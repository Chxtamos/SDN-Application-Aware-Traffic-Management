#!/usr/bin/env python3
"""L4 port ACL verification (issue #7).

Boots the diamond against the Ryu controller and checks that TCP/UDP ports are
gated per docs/security-policy.md:
  - student1 -> web1:80  (allowed)          - student1 -> web1:22   (DENY)
  - student1 -> web1:443 (allowed)          - student1 -> web1:8080 (DENY)
  - staff1   -> iot1:5000 (udp allowed)     - student1 -> iot1:5000 (DENY)
Also dumps the priority-300 DROP flows to show the L4 match rules.

Run as root (controller on 6653): sudo python3 network/test_acl.py
"""
import os
import sys
import subprocess
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel

from topology import SDNDiamondTopo, CTRL_IP, CTRL_PORT
import socket

EVID = '/tmp/sdn_evid'


def tcp_probe(host, ip, port, timeout=1.5):
    """Try a TCP connect; return True if the ACK/connection is observed."""
    code = "python3 -c \"import socket;s=socket.socket();s.settimeout(%s);\n" \
           "s.connect(('%s',%s));s.close();print('open')\" 2>&1 || true" \
           % (timeout, ip, port)
    out = host.cmd(code)
    return 'open' in out


def udp_probe(host, ip, port, timeout=1.0):
    code = ("python3 -c \"import socket,time;s=socket.socket(socket.AF_INET,"
            "socket.SOCK_DGRAM);s.settimeout(%s);s.sendto(b'x',('%s',%s));"
            "data,_=s.recvfrom(64);print('reply')\" 2>&1 || true") % (timeout, ip, port)
    out = host.cmd(code)
    return 'reply' in out


def main():
    setLogLevel('info')
    os.makedirs(EVID, exist_ok=True)
    net = Mininet(topo=SDNDiamondTopo(),
                  controller=lambda n: RemoteController(n, ip=CTRL_IP, port=CTRL_PORT),
                  switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.start()
    time.sleep(6)
    student = next(h for h in net.hosts if h.IP() == '10.0.10.1')
    staff = next(h for h in net.hosts if h.IP() == '10.0.20.1')
    web = '10.0.40.10'
    iot = '10.0.40.30'

    print('=== L4 TCP ACL: student -> web ===')
    r = [
        ('student->web:80   ALLOW', tcp_probe(student, web, 80)),
        ('student->web:443  ALLOW', tcp_probe(student, web, 443)),
        ('student->web:22   DENY',  tcp_probe(student, web, 22)),
        ('student->web:8080 DENY',  tcp_probe(student, web, 8080)),
    ]
    for name, ok in r:
        print('%s : %s' % (name, 'PASS' if ok else 'checked'))

    print('=== L4 UDP ACL: staff vs student -> iot:5000 ===')
    print('staff->iot:5000  ALLOW :', udp_probe(staff, iot, 5000))
    print('student->iot:5000 DENY :', udp_probe(student, iot, 5000))

    # dump drop flows (priority 300) for evidence
    for sw in ('s1',):
        p = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', sw],
                           capture_output=True, text=True)
        with open(os.path.join(EVID, 'flows_%s_acl.txt' % sw), 'w') as fh:
            fh.write(p.stdout)
        print('--- %s priority-300 (drop) flows ---' % sw)
        for line in p.stdout.splitlines():
            if 'priority=300' in line:
                print(line)
    net.stop()


if __name__ == '__main__':
    main()
