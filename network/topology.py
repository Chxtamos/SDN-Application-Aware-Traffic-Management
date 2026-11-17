#!/usr/bin/env python3
"""
Frozen OpenFlow 1.3 diamond topology (Issue #3/#5).

    Path A: s1 - s2 - s4
    Path B: s1 - s3 - s4

Sources on s1 (Student/Staff/Management subnets), services on s4.
Runnable with Mininet. Controllers are started before hosts so OVS port
numbers stay deterministic across runs (Issue #5 requirement).

Usage:
    sudo mn --custom topology.py --topo criedge --controller remote \
        --link tc,delay=1ms   # with a remote Ryu controller on the host
    sudo python3 topology.py  # runs the same topology with a default controller
"""
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch, Controller, RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.link import TCLink

# --- frozen addresses / port map (see docs/topology.md) ----------------------
LINK_BW = 100        # Mbps on all links
LINK_DELAY = "1ms"   # constant small delay so latency differences come from load
CTRL_PORT = 6653      # standard OpenFlow 1.3 control port

HOSTS = {
    # name: (ip, subnet-gateway)
    "student1": "10.0.10.1/24",
    "staff1":   "10.0.20.1/24",
    "mgmt1":    "10.0.30.1/24",
    "web1":     "10.0.40.10/24",
    "file1":    "10.0.40.20/24",
    "iot1":     "10.0.40.30/24",
}


class SDNDiamondTopo(Topo):
    """s1-s2-s4 (path A) and s1-s3-s4 (path B), 100 Mbps links."""

    def build(self):
        # switches
        s1 = self.addSwitch("s1", cls=OVSSwitch, protocols="OpenFlow13")
        s2 = self.addSwitch("s2", cls=OVSSwitch, protocols="OpenFlow13")
        s3 = self.addSwitch("s3", cls=OVSSwitch, protocols="OpenFlow13")
        s4 = self.addSwitch("s4", cls=OVSSwitch, protocols="OpenFlow13")

        # hosts on s1: the three roles (sources)
        student1 = self.addHost("student1", ip="10.0.10.1/24")
        staff1 = self.addHost("staff1", ip="10.0.20.1/24")
        mgmt1 = self.addHost("mgmt1", ip="10.0.30.1/24")

        # hosts on s4: the three services (endpoints)
        web1 = self.addHost("web1", ip="10.0.40.10/24")
        file1 = self.addHost("file1", ip="10.0.40.20/24")
        iot1 = self.addHost("iot1", ip="10.0.40.30/24")

        # inter-switch links: both paths, 100 Mbps, delay 1ms
        self.addLink(s1, s2, bw=LINK_BW, delay=LINK_DELAY)   # path A hop 1
        self.addLink(s2, s4, bw=LINK_BW, delay=LINK_DELAY)   # path A hop 2
        self.addLink(s1, s3, bw=LINK_BW, delay=LINK_DELAY)   # path B hop 1
        self.addLink(s3, s4, bw=LINK_BW, delay=LINK_DELAY)   # path B hop 2

        # host links, 100 Mbps
        self.addLink(s1, student1, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s1, staff1, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s1, mgmt1, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s4, web1, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s4, file1, bw=LINK_BW, delay=LINK_DELAY)
        self.addLink(s4, iot1, bw=LINK_BW, delay=LINK_DELAY)


def run():
    net = Mininet(
        topo=SDNDiamondTopo(),
        controller=RemoteController("c0", port=CTRL_PORT),
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
    )
    net.start()
    print(
        "Topology up. Path A = s1-s2-s4, Path B = s1-s3-s4. "
        "Sources: student1/staff1/mgmt1 on s1. Services: web1/file1/iot1 on s4."
    )
    CLI(net)  # interactive mininet CLI
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()
