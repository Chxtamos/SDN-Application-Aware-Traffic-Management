#!/usr/bin/env python3
"""Ryu controller for the SDN diamond topology (issues #6, #7, #8, #9, #10).

Single self-contained OpenFlow 1.3 app implementing Decision A
(docs/architecture.md, docs/topology.md sec. 3.1): hosts sit in four /24
subnets behind gateway .254, and the controller acts as that gateway:

  #6  ARP proxy for .254 gateways + per-flow L3 routing for connectivity
  #7  packet classification (Interactive/Periodic/Bulk) + high-priority ACL
  #8  port/flow stats collector + link-utilisation (logged to /tmp/sdn_stats.log)
  #9  fixed baseline: static single-path (Path A) L3 flow rules, loop-free,
      forward + return, with edge MAC rewrite
  #10 adaptive rerouting between Path A and B with hysteresis + cooldown

Run on the mininet box (as the mininet user):
    PATH=$PATH:$HOME/.local/bin ryu-manager --ofp-tcp-listen-port 6653 \
        controller/ryu_controller.py
"""
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
STATS_FILE = '/tmp/sdn_stats.log'

# ------------------------- frozen topology (docs/topology.md) --------------
SWITCH = {1: 's1', 2: 's2', 3: 's3', 4: 's4'}
# host ip -> (edge dpid, edge port, mac)
HOST = {
    '10.0.10.1': (1, 1, '00:00:00:00:0a:01'),
    '10.0.20.1': (1, 2, '00:00:00:00:14:01'),
    '10.0.30.1': (1, 3, '00:00:00:00:1e:01'),
    '10.0.40.10': (4, 1, '00:00:00:00:28:0a'),
    '10.0.40.20': (4, 2, '00:00:00:00:28:14'),
    '10.0.40.30': (4, 3, '00:00:00:00:28:1e'),
}
GATEWAYS = {'10.0.10.254', '10.0.20.254', '10.0.30.254', '10.0.40.254'}
EDGE_MAC = {1: 'aa:00:00:00:00:01', 4: 'aa:00:00:00:00:04'}
ROLE = {'10.0.10.0': 'student', '10.0.20.0': 'staff', '10.0.30.0': 'mgmt'}
SERVICE_SUB = '10.0.40.0'
DENY_DST = {('10.0.10.0', '10.0.40.20'), ('10.0.10.0', '10.0.40.30')}

CAPACITY = {  # dpid -> {port: bps}
    1: {1: 100e6, 2: 100e6, 3: 100e6, 4: 100e6, 5: 100e6},
    2: {1: 100e6, 2: 100e6},
    3: {1: 100e6, 2: 100e6},
    4: {1: 100e6, 2: 100e6, 3: 100e6, 4: 100e6, 5: 100e6},
}
# Path A transit ports: (dpid, port) pairs that carry Path A / Path B traffic
PATH_A_PORTS = {(1, 4), (2, 1), (2, 2), (4, 4)}
PATH_B_PORTS = {(1, 5), (3, 1), (3, 2), (4, 5)}


def classify(proto, dport):
    if proto == 'tcp' and dport in (80, 443):
        return 'interactive'
    if proto == 'udp' and (dport in (161, 162) or 5000 <= dport <= 5100):
        return 'periodic'
    if proto == 'tcp' and dport in (20, 21, 22):
        return 'bulk'
    if proto == 'tcp' and 8000 <= dport <= 9000:
        return 'bulk'
    return 'interactive'


class RyuController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(RyuController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.current_path = 'A'
        self.installed = set()          # (src,dst) flow pairs already installed
        # stats (#8)
        self.raw = {}
        self.link_util = {}
        self.path_util = {'A': 0.0, 'B': 0.0}
        self.sample_dt = 2.0
        # adaptive (#10)
        self.congest_hi = 80.0
        self.congest_lo = 45.0
        self.cool_down = 10.0
        self.busy_persist = 3
        self.congest_count = 0
        self.cooldown_until = 0.0
        self.adaptive_enabled = False
        self._ensure_log()
        self._log('controller started: gateway=Decision-A, path=%s, adaptive=%s' % (
            self.current_path, self.adaptive_enabled))
        hub.spawn(self._monitor_loop)

    def _log(self, msg):
        LOG.info('[%s] %s' % (time.strftime('%H:%M:%S'), msg))

    def _ensure_log(self):
        if STATS_FILE and not os.path.exists(STATS_FILE):
            try:
                open(STATS_FILE, 'w').close()
            except OSError:
                pass

    def _log_raw(self, line):
        LOG.info(line)
        try:
            with open(STATS_FILE, 'a') as f:
                f.write(line + '\n')
        except OSError:
            pass

    # ---------------- helpers ----------------
    def _add_flow(self, dp, priority, match, actions, idle=0):
        inst = [dp.ofproto_parser.OFPInstructionActions(
            dp.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = dp.ofproto_parser.OFPFlowMod(
            datapath=dp, priority=priority, match=match, instructions=inst,
            buffer_id=dp.ofproto.OFP_NO_BUFFER, idle_timeout=idle,
            flags=dp.ofproto.OFPFF_SEND_FLOW_REM)
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
            self._log('ARP proxy: %s -> gw %s on %s' % (
                a.src_ip, a.dst_ip, SWITCH.get(dp.id)))
            return
        # route ARP to a real destination host on the same edge
        if a.dst_ip in HOST and dp.id in (1, 4):
            hdpid, hport, _ = HOST[a.dst_ip]
            if dp.id == hdpid:
                self._packet_out(dp, in_port, pkt.data,
                                 [dp.ofproto_parser.OFPActionOutput(hport)])

    # ---------------- #7 ACL ----------------
    def _acl_allowed(self, sip, dip):
        """Default-deny: only source-role subnets may reach the service subnet."""
        ssub = '.'.join(sip.split('.')[:3]) + '.0'
        role = ROLE.get(ssub)
        if role is None:
            return False   # non-role source (incl. 10.0.40.x) -> deny (return traffic
                           # is handled by the reverse L3 flows, not via _acl_allowed)
        if (ssub, dip) in DENY_DST:
            return False
        dsub = '.'.join(dip.split('.')[:3]) + '.0'
        return dsub == SERVICE_SUB

    # ---------------- #9 L3 route install (loop-free, edge MAC rewrite) ----
    def _route_ip(self, dp, in_port, eth, p4, pkt):
        sip, dip = p4.src, p4.dst
        # ACL drop (default-deny) (#7)
        if not self._acl_allowed(sip, dip):
            self._log('ACL DENY %s -> %s on %s' % (sip, dip, SWITCH.get(dp.id)))
            # install a high-priority DROP rule so denied pairs stop hitting
            # PacketIn (docs/security-policy.md 3): drop locally, don't flood
            if dp.id in (1, 4):
                dm = dp.ofproto_parser.OFPMatch(in_port=in_port, eth_type=0x0800,
                                                ipv4_dst=dip)
                self._add_flow(dp, 300, dm, [])   # empty actions = drop
            return
        if dip not in HOST:
            self._log('unknown dst %s -> drop' % dip)
            return
        if (sip, dip) in self.installed:
            return   # flows already installed
        # classify for the log (#7)
        flow = pkt.get_protocol(tcp.tcp) or pkt.get_protocol(udp.udp)
        dport = flow.dst_port if flow else 0
        proto = ('tcp' if pkt.get_protocol(tcp.tcp)
                 else 'udp' if pkt.get_protocol(udp.udp) else 'icmp')
        self._log('ROUTE %s->%s (%s:%s) class=%s path=%s' % (
            sip, dip, proto, dport, classify(proto, dport), self.current_path))
        self._install_l3_flows(sip, dip, p4.src, p4.dst)
        self.installed.add((sip, dip))

    def _install_l3_flows(self, sip, dip, smac, dmac):
        """Install bidirectional L3 flows along the active path with edge MAC
        rewrite (Decision A). Path is chosen by self.current_path."""
        src_e, src_p, src_mac = HOST[sip]
        dst_e, dst_p, dst_mac = HOST[dip]
        parser = self.datapaths[src_e].ofproto_parser if src_e in self.datapaths else None
        if not parser:
            return
        if self.current_path == 'A':
            trans = 2          # transit switch s2
            s1_out, s4_in = 4, 4   # s1->s2 port, s4 from-s2 port
        else:
            trans = 3
            s1_out, s4_in = 5, 5
        # --- forward: src host -> dst host (downstream) ---
        # ingress edge (s1)
        self._l3_fwd(self._sw(src_e), in_port=src_p, dst_ip=dip,
                     dst_mac=dst_mac, next_port=s1_out)
        # transit (s2/s3): match on dst mac -> to s4
        self._l3_fwd(self._sw(trans), match_eth=dst_mac,
                     out_port=self._edge_port(trans, dst_e, 'fwd'))
        # egress edge (s4): match dst ip -> rewire mac to dst host + output
        self._l3_fwd(self._sw(dst_e), in_port=s4_in, dst_ip=dip,
                     dst_mac=dst_mac, out_port=dst_p)
        # --- reverse: dst host -> src host ---
        self._l3_fwd(self._sw(dst_e), in_port=dst_p, dst_ip=sip,
                     dst_mac=src_mac, next_port=s4_in)
        self._l3_fwd(self._sw(trans), match_eth=src_mac,
                     out_port=self._edge_port(trans, src_e, 'rev'))
        self._l3_fwd(self._sw(src_e), in_port=s1_out, dst_ip=sip,
                     dst_mac=src_mac, out_port=src_p)

    def _sw(self, dpid):
        return self.datapaths.get(dpid)

    def _edge_port(self, transit, edge, direction):
        # port on transit switch (s2/s3) toward an edge
        if edge == 1:
            return 1
        if edge == 4:
            return 2

    def _l3_fwd(self, dp, in_port=None, dst_ip=None, dst_mac=None,
                match_eth=None, next_port=None, out_port=None):
        if not dp:
            return
        kw = {}
        if in_port is not None: kw['in_port'] = in_port
        if dst_ip is not None:
            kw['eth_type'] = 0x0800   # IPv4 (required by OVS when matching ipv4)
            kw['ipv4_dst'] = dst_ip
        if match_eth is not None: kw['eth_dst'] = match_eth
        match = dp.ofproto_parser.OFPMatch(**kw)
        acts = []
        if dst_mac:
            acts.append(dp.ofproto_parser.OFPActionSetField(eth_dst=dst_mac))
        out = next_port if next_port is not None else out_port
        if out is not None:
            acts.append(dp.ofproto_parser.OFPActionOutput(out))
        self._add_flow(dp, 200, match, acts)

    # ---------------- #8 stats + #10 adaptive ----------------
    def _monitor_loop(self):
        while True:
            hub.sleep(self.sample_dt)
            self._poll_stats()
            if self.adaptive_enabled:
                self._decide_reroute()

    def _poll_stats(self):
        for dp in self.datapaths.values():
            dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(
                dp, 0, dp.ofproto.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        dp = ev.msg.datapath
        now = time.time()
        for st in ev.msg.body:
            key = (dp.id, st.port_no)
            rx_b, tx_b = st.rx_bytes, st.tx_bytes
            old = self.raw.get(key)
            if old and now > old[0]:
                dt = now - old[0]
                rx_rate = (rx_b - old[1]) * 8.0 / dt if rx_b >= old[1] else 0.0
                cap = CAPACITY.get(dp.id, {}).get(st.port_no)
                util = min(100.0, rx_rate / cap * 100.0) if cap else 0.0
                self.link_util[key] = util
                if key in PATH_A_PORTS:
                    self.path_util['A'] = max(self.path_util.get('A', 0.0), util)
                if key in PATH_B_PORTS:
                    self.path_util['B'] = max(self.path_util.get('B', 0.0), util)
                self._log_raw(
                    'PORT s%s p%s dt=%.1f rx=%d tx=%d rate=%.0f util=%.2f%%' %
                    (dp.id, st.port_no, dt, rx_b, tx_b, rx_rate, util))
            self.raw[key] = (now, rx_b, tx_b, st.rx_packets)

    def _decide_reroute(self):
        now = time.time()
        util = self.path_util.get(self.current_path, 0.0)
        if now < self.cooldown_until:
            return
        if util >= self.congest_hi:
            self.congest_count += 1
            if self.congest_count >= self.busy_persist:
                self._switch_path('B' if self.current_path == 'A' else 'A')
                self.congest_count = 0
        elif util < self.congest_lo:
            self.congest_count = 0

    def _switch_path(self, new):
        if new == self.current_path:
            return
        self.current_path = new
        self.cooldown_until = time.time() + self.cool_down
        self.installed = set()
        self._log('ADAPTIVE: switch -> path %s (cooldown %ss)' % (new, self.cool_down))
