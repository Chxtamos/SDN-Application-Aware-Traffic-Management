#!/usr/bin/env python3
"""Non-interactive Mininet driver to verify issues #6-#9 on the diamond.

Boots the SDNDiamondTopo against the Ryu controller (controller/ryu_controller.py,
already started on port 6653), then:
  - waits for the controller to come up,
  - pings the ALLOWED pairs (expect success) and the DENY pairs (expect timeout),
  - dumps OVS flows and port stats for evidence to /tmp/sdn_evid/.

Run as root ON the mininet box (after starting the controller):
    sudo python3 network/verify_connectivity.py
"""
import os
import sys
import time
import subprocess

# make sure the repo root + topology are importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info

from topology import SDNDiamondTopo, CTRL_IP, CTRL_PORT

EVID = '/tmp/sdn_evid'
ALLOWED = [('10.0.10.1', '10.0.40.10'), ('10.0.20.1', '10.0.40.20'),
           ('10.0.30.1', '10.0.40.30'), ('10.0.20.1', '10.0.40.30')]
DENIED = [('10.0.10.1', '10.0.40.20'), ('10.0.10.1', '10.0.40.30')]


def main():
    setLogLevel('info')
    os.makedirs(EVID, exist_ok=True)
    net = Mininet(topo=SDNDiamondTopo(),
                  controller=lambda name: RemoteController(name, ip=CTRL_IP,
                                                           port=CTRL_PORT),
                  switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.start()
    print('=== topology started, waiting for controller ===')
    time.sleep(6)  # give OVS/Ryu a few seconds to handshake + install baseline

    hosts = {h.name: h for h in net.hosts}
    lines = []
    print('\n=== ALLOWED pairs (expect reply) ===')
    for sip, dip in ALLOWED:
        src = next(h for h in net.hosts if h.IP() == sip)
        out = src.cmd('ping -c 3 -W 1 %s' % dip)
        # cold-start: first ICMP triggers L3-flow install; require >=1 echo reply
        got = int(out.split('received')[0].split()[-1]) if 'received' in out else 0
        ok = got >= 1
        lines.append('ALLOWED %s -> %s: %s (replies=%d)\n%s' % (sip, dip, 'OK' if ok else 'FAIL', got, out))
        print('%s -> %s : %s (replies=%d)' % (sip, dip, 'OK' if ok else 'FAIL', got))

    print('\n=== DENIED pairs (expect timeout) ===')
    for sip, dip in DENIED:
        src = next(h for h in net.hosts if h.IP() == sip)
        out = src.cmd('ping -c 2 -W 1 %s' % dip)
        got = int(out.split('received')[0].split()[-1]) if 'received' in out else 0
        timeout = got == 0 or '100% packet loss' in out or 'Destination Host Unreachable' in out
        lines.append('DENIED %s -> %s: %s (replies=%d, expected drop)\n%s' % (sip, dip, 'OK' if timeout else 'UNEXPECTED', got, out))
        print('%s -> %s : %s' % (sip, dip, 'OK' if timeout else 'UNEXPECTED'))

    # dump flows + port stats
    print('\n=== dump flows (each switch) ===')
    for swdpid in (1, 2, 3, 4):
        out = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows',
                              's%d' % swdpid], capture_output=True, text=True)
        lines.append('\n=== flows s%d ===\n%s' % (swdpid, out.stdout))
        print('s%d: %s' % (swdpid, out.stdout.splitlines()[-1] if out.stdout else ''))

    with open(os.path.join(EVID, 'connectivity_evidence.txt'), 'w') as f:
        f.write('\n'.join(lines))
    net.stop()
    print('\n=== evidence written to %s ===' % EVID)


if __name__ == '__main__':
    main()
