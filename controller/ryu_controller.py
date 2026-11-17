#!/usr/bin/env python3
"""Ryu controller for the SDN diamond topology (issues #6-#10).

Single OpenFlow 1.3 app:
  #6  ARP proxy + per-flow L3 routing (connectivity)
  #7  L4-aware ACL/classification (default-deny, port-limited) + drop flows
  #8  port/flow stats collector + link utilisation (raw .csv / .json out)
  #9  fixed baseline L3 rules (loop-free, bidirectional, edge MAC rewrite)
  #10 adaptive rerouting A<->B with hysteresis + cooldown + real OVS flow swap

Run (mininet box, repo root):
    PATH=$PATH:$HOME/.local/bin ryu-manager --ofp-tcp-listen-port 6653 \
        controller/ryu_controller.py
"""
import json
import logging
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp, udp
from ryu.lib import hub

LOG = logging.getLogger('sdn')
STATS_CSV = '/tmp/sdn_stats.csv'      # raw port stats lines
FLOW_CSV = '/tmp/sdn_flows.csv'
FLOW_HEADER = 'ts,dpid,table,priority,cookie,packets,bytes,match'       # raw flow stats lines
CSV_HEADER = 'ts,dpid,port,rx_bytes,tx_bytes,rx_packets,util_pct,path'

# ------------------------- frozen topology (docs/topology.md) --------------
SWITCH = {1: 's1', 2: 's2', 3: 's3', 4: 's4'}
HOST = {  # ip -> (edge dpid, edge port, mac)
    '10.0.10.1': (1, 1, '00:00:00:00:0a:01'),
    '10.0.20.1': (1, 2, '00:00:00:00:14:01'),
    '10.0.30.1': (1, 3, '00:00:00:00:1e:01'),
    '10.0.40.10': (4, 1, '00:00:00:00:28:0a'),
    '10.0.40.20': (4, 2, '00:00:00:00:28:14'),
    '10.0.40.30': (4, 3, '00:00:00:00:28:1e'),
}
GATEWAYS = {'10.0.10.254', '10.0.20.254', '10.0.30.254', '10.0.40.254'}
EDGE_MAC = {1: 'aa:00:00:00:00:01', 4: 'aa:00:00:00:00:04'}
SERVICE_SUB = '10.0.40.0'
# source subnet -> role
ROLE = {'10.0.10.0': 'student', '10.0.20.0': 'staff', '10.0.30.0': 'mgmt'}
# fine-grained deny pairs (student cannot reach file/iot service)
DENY_DST = {('10.0.10.0', '10.0.40.20'), ('10.0.10.0', '10.0.40.30')}

# ACL matrix (docs/security-policy.md). role -> list of (dst ip, (lo,hi) ports, proto)
ACL = {
    'student': [
        ('10.0.40.10', (80, 80), 'tcp'), ('10.0.40.10', (443, 443), 'tcp'),
    ],
    'staff': [
        ('10.0.40.10', (80, 80), 'tcp'), ('10.0.40.10', (443, 443), 'tcp'),
        ('10.0.40.20', (20, 22), 'tcp'),
        ('10.0.40.20', (5201, 5201), 'tcp'),   # iperf3 bulk benchmark
        ('10.0.40.30', (161, 162), 'udp'),
        ('10.0.40.30', (5000, 5100), 'udp'),
    ],
    'mgmt': [
        ('10.0.40.10', (80, 80), 'tcp'), ('10.0.40.10', (443, 443), 'tcp'),
        ('10.0.40.20', (20, 22), 'tcp'),
        ('10.0.40.20', (5201, 5201), 'tcp'),   # iperf3 bulk benchmark
        ('10.0.40.30', (161, 162), 'udp'),
        ('10.0.40.30', (5000, 5100), 'udp'),
    ],
}

CAPACITY = {  # dpid -> {port: bps}
    1: {1: 100e6, 2: 100e6, 3: 100e6, 4: 100e6, 5: 100e6},
    2: {1: 100e6, 2: 100e6},
    3: {1: 100e6, 2: 100e6},
    4: {1: 100e6, 2: 100e6, 3: 100e6, 4: 100e6, 5: 100e6},
}
PATH_A_PORTS = {(1, 4), (2, 1), (2, 2), (4, 4)}
PATH_B_PORTS = {(1, 5), (3, 1), (3, 2), (4, 5)}


def classify(proto, dport):
    if proto == 'tcp' and dport in (80, 443):
        return 'interactive'
    if proto == 'udp' and (dport in (161, 162) or 5000 <= dport <= 5100):
        return 'periodic'
    if proto == 'tcp' and dport in (20, 21, 22):
        return 'bulk'
    if proto == 'tcp' and dport == 5201:
        return 'bulk'   # iperf3 bulk benchmark
    if proto == 'tcp' and 8000 <= dport <= 9000:
        return 'bulk'
    return 'interactive'   # web-like default; ACL still gates by port


class RyuController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RyuController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.current_path = 'A'
        self.installed = set()              # (sip,dip,proto,dport)
        # stats (#8)
        self.raw = {}                       # (dpid,port)->(ts,rx_b,tx_b,rx_pkt)
        self.link_util = {}
        self.path_util = {'A': 0.0, 'B': 0.0}
        self.sample_dt = 1.0
        # adaptive (#10)
        self.congest_hi = 80.0
        self.congest_lo = 45.0
        self.cool_down = 10.0
        self.busy_persist = 3
        self.congest_count = 0
        self.cooldown_until = 0.0
        self.adaptive_enabled = True
        self._init_logs()
        self._log('controller started: adaptive=%s path=%s' % (
            self.adaptive_enabled, self.current_path))
        hub.spawn(self._monitor_loop)

    def _log(self, msg):
        LOG.info('[%s] %s' % (time.strftime('%H:%M:%S'), msg))

    def _init_logs(self):
        for path, header in ((STATS_CSV, CSV_HEADER), (FLOW_CSV, FLOW_HEADER)):
            if not os.path.exists(path):
                try:
                    with open(path, 'w') as f:
                        f.write(header + '\n')
                except OSError:
                    pass

    def _csv(self, path, row):
        try:
            with open(path, 'a') as f:
                f.write(','.join(str(x) for x in row) + '\n')
        except OSError:
            pass

    # ---------------- helpers ----------------
    def _add_flow(self, dp, priority, match, actions, idle=0, cookie=0):
        inst = [dp.ofproto_parser.OFPInstructionActions(
            dp.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = dp.ofproto_parser.OFPFlowMod(
            datapath=dp, priority=priority, match=match, instructions=inst,
            buffer_id=dp.ofproto.OFP_NO_BUFFER, idle_timeout=idle,
            cookie=cookie, flags=dp.ofproto.OFPFF_SEND_FLOW_REM)
        dp.send_msg(mod)

    def _del_l3_flows(self, dp):
        """Delete ONLY the L3 routing flows (cookie 0x200). OFPFC_DELETE with a
        wildcard match deletes EVERYTHING, so we scope by cookie to leave the
        table-miss (priority 0) and ACL DROP (priority 300) rules untouched."""
        ofp = dp.ofproto
        mod = dp.ofproto_parser.OFPFlowMod(
            datapath=dp, command=ofp.OFPFC_DELETE,
            match=dp.ofproto_parser.OFPMatch(),
            table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
            cookie=0x200, cookie_mask=0xffffffffffffffff)
        dp.send_msg(mod)

    def _packet_out(self, dp, port, data, actions):
        dp.send_msg(dp.ofproto_parser.OFPPacketOut(
            datapath=dp, buffer_id=dp.ofproto.OFP_NO_BUFFER,
            in_port=dp.ofproto.OFPP_CONTROLLER, data=data, actions=actions))

    # ---------------- events ----------------
    @set_ev_cls(ofp_event.EventOFPStateChange, MAIN_DISPATCHER)
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self._log('switch connected: %s (dpid=%s)' % (SWITCH.get(dp.id), dp.id))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        match = dp.ofproto_parser.OFPMatch()
        act = [dp.ofproto_parser.OFPActionOutput(
            dp.ofproto.OFPP_CONTROLLER, dp.ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, act)   # table-miss -> controller

    # ---------------- packet-in ----------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        dp = ev.msg.datapath
        in_port = ev.msg.match['in_port']
        pkt = packet.Packet(ev.msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if not eth:
            return
        a = pkt.get_protocol(arp.arp)
        if a:
            self._handle_arp(dp, in_port, eth, a, pkt)
            return
        p4 = pkt.get_protocol(ipv4.ipv4)
        if p4:
            self._route_ip(dp, in_port, eth, p4, pkt)

    # ---------------- #6 ARP proxy ----------------
    def _handle_arp(self, dp, in_port, eth, a, pkt):
        if a.opcode != arp.ARP_REQUEST:
            return
        if a.dst_ip in GATEWAYS and dp.id in (1, 4):
            mac = EDGE_MAC[dp.id]
            e = ethernet.ethernet(ethertype=eth.ethertype, dst=eth.src, src=mac)
            reply = arp.arp(hwtype=1, proto=0x0800, hlen=6, plen=4,
                            opcode=arp.ARP_REPLY, src_mac=mac, src_ip=a.dst_ip,
                            dst_mac=eth.src, dst_ip=a.src_ip)
            rep = packet.Packet(); rep.add_protocol(e); rep.add_protocol(reply)
            rep.serialize()
            self._packet_out(dp, in_port, rep.data,
                             [dp.ofproto_parser.OFPActionOutput(in_port)])
            return
        if a.dst_ip in HOST and dp.id in (1, 4):
            hdpid, hport, _ = HOST[a.dst_ip]
            if dp.id == hdpid:
                self._packet_out(dp, in_port, pkt.data,
                                 [dp.ofproto_parser.OFPActionOutput(hport)])

    # ---------------- #7 L4-aware ACL ----------------
    def _ip_pair_allowed(self, sip, dip):
        """Role->service-subnet reachability (used for ICMP / reachability)."""
        ssub = '.'.join(sip.split('.')[:3]) + '.0'
        role = ROLE.get(ssub)
        if role is None:
            return False
        dsub = '.'.join(dip.split('.')[:3]) + '.0'
        return dsub == SERVICE_SUB and (ssub, dip) not in DENY_DST

    def _flow_allowed(self, sip, dip, proto, dport):
        # ICMP/ping: reachability check between an allowed IP pair
        # (docs/security-policy.md - ICMP allowed for allowed pairs).
        if proto == 'icmp':
            return self._ip_pair_allowed(sip, dip)
        ssub = '.'.join(sip.split('.')[:3]) + '.0'
        role = ROLE.get(ssub)
        if role is None:
            return False
        for (dst, prange, pproto) in ACL.get(role, []):
            if dst != dip:
                continue
            if pproto != proto:
                continue
            if prange is None:
                return True
            if prange[0] <= (dport or 0) <= prange[1]:
                return True
        return False

    def _route_ip(self, dp, in_port, eth, p4, pkt):
        sip, dip = p4.src, p4.dst
        flow = pkt.get_protocol(tcp.tcp) or pkt.get_protocol(udp.udp)
        dport = flow.dst_port if flow else 0
        proto = ('tcp' if pkt.get_protocol(tcp.tcp)
                 else 'udp' if pkt.get_protocol(udp.udp) else 'icmp')
        # ACL (#7): default-deny, port-limited
        if not self._flow_allowed(sip, dip, proto, dport):
            self._log('ACL DENY %s[%s:%s] -> %s on %s' % (
                sip, proto, dport, dip, SWITCH.get(dp.id)))
            if dp.id in (1, 4):
                kw = {'in_port': in_port, 'eth_type': 0x0800, 'ipv4_src': sip,
                      'ipv4_dst': dip}
                if proto == 'tcp':
                    kw['ip_proto'] = 6; kw['tcp_dst'] = dport
                elif proto == 'udp':
                    kw['ip_proto'] = 17; kw['udp_dst'] = dport
                self._add_flow(dp, 300, dp.ofproto_parser.OFPMatch(**kw), [])
            return
        if dip not in HOST:
            return
        key = (sip, dip, proto, dport)
        if key in self.installed:
            return
        self._log('ROUTE %s->%s (%s:%s) class=%s path=%s' % (
            sip, dip, proto, dport, classify(proto, dport), self.current_path))
        self._install_l3_flows(sip, dip, proto, dport, p4.src, p4.dst)
        self.installed.add(key)

    # ---------------- #9 L3 route install ----------------
    def _install_l3_flows(self, sip, dip, proto, dport, smac, dmac):
        src_e, src_p, src_mac = HOST[sip]
        dst_e, dst_p, dst_mac = HOST[dip]
        if self.current_path == 'A':
            trans, s1_out, s4_in = 2, 4, 4
        else:
            trans, s1_out, s4_in = 3, 5, 5
        # per-flow L4 match fields to disambiguate flows (#7)
        l4 = {}
        if proto == 'tcp':
            l4['ip_proto'] = 6; l4['tcp_dst'] = dport
        elif proto == 'udp':
            l4['ip_proto'] = 17; l4['udp_dst'] = dport
        # forward
        self._l3_fwd(self._sw(src_e), in_port=src_p, src_ip=sip, dst_ip=dip,
                     dst_mac=dst_mac, next_port=s1_out, l4=l4)
        self._l3_fwd(self._sw(trans), match_eth=dst_mac, l4=l4,
                     out_port=self._edge_port(trans, dst_e, 'fwd'))
        self._l3_fwd(self._sw(dst_e), in_port=s4_in, src_ip=sip, dst_ip=dip,
                     dst_mac=dst_mac, out_port=dst_p, l4=l4)
        # reverse (5-tuple reversed)
        rl4 = {}
        if proto == 'tcp':
            rl4['ip_proto'] = 6; rl4['tcp_src'] = dport
        elif proto == 'udp':
            rl4['ip_proto'] = 17; rl4['udp_src'] = dport
        self._l3_fwd(self._sw(dst_e), in_port=dst_p, src_ip=dip, dst_ip=sip,
                     dst_mac=src_mac, next_port=s4_in, l4=rl4)
        self._l3_fwd(self._sw(trans), match_eth=src_mac, l4=rl4,
                     out_port=self._edge_port(trans, src_e, 'rev'))
        self._l3_fwd(self._sw(src_e), in_port=s1_out, src_ip=dip, dst_ip=sip,
                     dst_mac=src_mac, out_port=src_p, l4=rl4)

    def _sw(self, dpid):
        return self.datapaths.get(dpid)

    def _edge_port(self, transit, edge, direction):
        return 1 if edge == 1 else 2

    def _l3_fwd(self, dp, in_port=None, src_ip=None, dst_ip=None, dst_mac=None,
                match_eth=None, next_port=None, out_port=None, l4=None):
        if not dp:
            return
        kw = {'eth_type': 0x0800}
        if in_port is not None: kw['in_port'] = in_port
        if src_ip is not None: kw['ipv4_src'] = src_ip
        if dst_ip is not None: kw['ipv4_dst'] = dst_ip
        if match_eth is not None: kw['eth_dst'] = match_eth
        if l4:
            kw.update(l4)
        match = dp.ofproto_parser.OFPMatch(**kw)
        acts = []
        if dst_mac:
            acts.append(dp.ofproto_parser.OFPActionSetField(eth_dst=dst_mac))
        out = next_port if next_port is not None else out_port
        if out is not None:
            acts.append(dp.ofproto_parser.OFPActionOutput(out))
        self._add_flow(dp, 200, match, acts, cookie=0x200)

    # ---------------- #8 stats ----------------
    def _monitor_loop(self):
        while True:
            hub.sleep(self.sample_dt)
            # decide on the PREVIOUS cycle's accumulated path_util (before the
            # next poll resets it), otherwise util reads 0 right after reset.
            if self.adaptive_enabled:
                self._decide_reroute()
            self._poll_stats()

    def _poll_stats(self):
        # reset per-path utilisation once per sampling round (#10 fix:
        # _port_stats fires per-switch, so resetting there would wipe
        # contributions from switches that reply later)
        self.path_util = {'A': 0.0, 'B': 0.0}
        self.path_seen = False
        for dp in self.datapaths.values():
            dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(
                dp, 0, dp.ofproto.OFPP_ANY))
            dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(
                dp, 0, dp.ofproto.OFPTT_ALL, dp.ofproto.OFPP_ANY,
                dp.ofproto.OFPG_ANY, 0, 0, None))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        dp = ev.msg.datapath
        now = time.time()
        for st in ev.msg.body:
            key = (dp.id, st.port_no)
            rx_b, tx_b, rx_pkt = st.rx_bytes, st.tx_bytes, st.rx_packets
            old = self.raw.get(key)
            util = 0.0
            if old and now > old[0]:
                dt = now - old[0]
                rrate = (rx_b - old[1]) * 8.0 / dt if rx_b >= old[1] else 0.0
                trate = (tx_b - old[2]) * 8.0 / dt if tx_b >= old[2] else 0.0
                cap = CAPACITY.get(dp.id, {}).get(st.port_no)
                util = min(100.0, max(rrate, trate) / cap * 100.0) if cap else 0.0
                self.link_util[key] = util
                if key in PATH_A_PORTS:
                    self.path_util['A'] = max(self.path_util['A'], util)
                if key in PATH_B_PORTS:
                    self.path_util['B'] = max(self.path_util['B'], util)
                self._csv(STATS_CSV, [int(now), dp.id, st.port_no, rx_b, tx_b,
                                      rx_pkt, round(util, 2), self.current_path])
            self.raw[key] = (now, rx_b, tx_b, rx_pkt)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats(self, ev):
        dp = ev.msg.datapath
        now = int(time.time())
        for st in ev.msg.body:
            if st.priority in (200, 300):
                m = st.match.to_jsondict() if hasattr(st.match, 'to_jsondict') else str(st.match)
                self._csv(FLOW_CSV, [now, dp.id, st.table_id, st.priority,
                                     hex(st.cookie or 0), st.packet_count,
                                     st.byte_count, m])

    # ---------------- #10 adaptive ----------------
    def _decide_reroute(self):
        """Hysteresis with back-off: only reroute to a less-congested path, and
        only back after congestion clears."""
        now = time.time()
        util = self.path_util.get(self.current_path, 0.0)
        other = 'B' if self.current_path == 'A' else 'A'
        util_other = self.path_util.get(other, 0.0)
        if now < self.cooldown_until:
            return
        if util >= self.congest_hi and util_other < util:
            self.congest_count += 1
            if self.congest_count >= self.busy_persist:
                self._switch_path(other)
                self.congest_count = 0
        elif util < self.congest_lo:
            self.congest_count = 0

    def _switch_path(self, new):
        if new == self.current_path:
            return
        self._log('ADAPTIVE: switch %s -> %s (util A=%.0f%% B=%.0f%%)' % (
            self.current_path, new, self.path_util['A'], self.path_util['B']))
        # remove old L3 flows + clear installed so flows re-install on new path
        for dp in self.datapaths.values():
            self._del_l3_flows(dp)
        self.current_path = new
        self.installed = set()
        self.congest_count = 0
        self.cooldown_until = time.time() + self.cool_down
        self._log('ADAPTIVE: now on path %s (cooldown %ss)' % (
            new, self.cool_down))
