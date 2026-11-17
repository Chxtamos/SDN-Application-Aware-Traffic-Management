#!/usr/bin/env python3
"""
Frozen OpenFlow 1.3 diamond topology (Issue #3/#5).

    Path A: s1 - s2 - s4
    Path B: s1 - s3 - s4

Sources on s1 (Student/Staff/Management subnets), services on s4.
Runnable with Mininet. Controllers are started before hosts so OVS port
numbers stay deterministic across runs (Issue #5 requirement).

Port numbers are pinned explicitly (port1=/port2=) to match docs/topology.md.

Usage (script style):
    sudo python3 network/topology.py       # starts with a RemoteController on c0

Usage (mn --custom style):
    sudo mn --custom network/topology.py --topo sdndiamond \
        --controller remote --link tc,delay=1ms
"""
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.link import TCLink

# --- frozen addresses / port map (see docs/topology.md) ----------------------
LINK_BW = 100          # Mbps on all links
LINK_DELAY = "1ms"     # constant small delay so latency diff comes from load
CTRL_IP = "127.0.0.1"  # remote controller host (Ryu on the same machine)
CTRL_PORT = 6653       # standard OpenFlow 1.3 control port


class SDNDiamondTopo(Topo):
    """s1-s2-s4 (path A) and s1-s3-s4 (path B), 100 Mbps links.

    Port map (matches docs/topology.md):
      s1: 1=student1, 2=staff1, 3=mgmt1, 4=s2 (A), 5=s3 (B)
      s2: 1=s1 (A-in), 2=s4 (A-out)
      s3: 1=s1 (B-in), 2=s4 (B-out)
      s4: 1=web1, 2=file1, 3=iot1, 4=s2 (A), 5=s3 (B)
    """

    def build(self):
        # switches, with explicit datapath ids and secure fail mode
        s1 = self.addSwitch("s1", cls=OVSSwitch, protocols="OpenFlow13",
                            dpid="0000000000000001", failMode="secure")
        s2 = self.addSwitch("s2", cls=OVSSwitch, protocols="OpenFlow13",
                            dpid="0000000000000002", failMode="secure")
        s3 = self.addSwitch("s3", cls=OVSSwitch, protocols="OpenFlow13",
                            dpid="0000000000000003", failMode="secure")
        s4 = self.addSwitch("s4", cls=OVSSwitch, protocols="OpenFlow13",
                            dpid="0000000000000004", failMode="secure")

        # --- hosts on s1: the three roles (sources) ---------------------
        # defaultRoute so traffic crossing /24 uses the controller-as-gateway .254
        student1 = self.addHost("student1", ip="10.0.10.1/24",
                                defaultRoute="via 10.0.10.254")
        staff1 = self.addHost("staff1", ip="10.0.20.1/24",
                              defaultRoute="via 10.0.20.254")
        mgmt1 = self.addHost("mgmt1", ip="10.0.30.1/24",
                             defaultRoute="via 10.0.30.254")

        # --- hosts on s4: the three services (endpoints) ----------------
        web1 = self.addHost("web1", ip="10.0.40.10/24",
                            defaultRoute="via 10.0.40.254")
        file1 = self.addHost("file1", ip="10.0.40.20/24",
                             defaultRoute="via 10.0.40.254")
        iot1 = self.addHost("iot1", ip="10.0.40.30/24",
                            defaultRoute="via 10.0.40.254")

        # --- inter-switch links: both paths, pinned ports ---------------
        # Path A routes
        self.addLink(s1, s2, port1=4, port2=1, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s2, s4, port1=2, port2=4, bw=LINK_BW, delay=LINK_DELAY)
        # Path B routes
        self.addLink(s1, s3, port1=5, port2=1, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s3, s4, port1=2, port2=5, bw=LINK_BW, delay=LINK_DELAY)

        # --- host links: pinned to the frozen port map ------------------
        # s1 sources
        self.addLink(s1, student1, port1=1, port2=1,
                     bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s1, staff1, port1=2, port2=1,
                     bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s1, mgmt1, port1=3, port2=1,
                     bw=LINK_BW, delay=LINK_DELAY)
        # s4 services
        self.addLink(s4, web1, port1=1, port2=1,
                     bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s4, file1, port1=2, port2=1,
                     bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s4, iot1, port1=3, port2=1,
                     bw=LINK_BW, delay=LINK_DELAY)


# expose the topology class for 'mn --custom topology.py --topo sdndiamond'
topos = {"sdndiamond": SDNDiamondTopo}


def run():
    net = Mininet(
        topo=SDNDiamondTopo(),
        controller=RemoteController("c0", ip=CTRL_IP, port=CTRL_PORT),
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )
    net.start()
    print(
        "Topology up. Path A = s1-s2-s4, Path B = s1-s3-s4. "
        "Sources: student1/staff1/mgmt1 on s1. Services: web1/file1/iot1 on s4."
    )
    print(
        "NOTE: this diamond has an L2 loop. Use the team's ARP-proxy/baseline "
        "controller, NOT ryu.app.simple_switch_13 (it floods -> broadcast storm)."
    )
    CLI(net)  # interactive mininet CLI
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()
