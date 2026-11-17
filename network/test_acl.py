#!/usr/bin/env python3
"""L4 port ACL verification (issue #7) with real destination servers.

Starts TCP listeners on web1 (80,443,22,8080) and a UDP echo on iot1 (5000), then
probes from student1/staff1 so that ALLOW ports get a real connect/echo and DENY
ports are dropped. Evidence: priority-300 DROP flows on s1 + per-pair verdict.

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

EVID = '/tmp/sdn_evid'

# start a TCP listener on a host
def tcp_srv(host, port):
    code = ("nohup python3 -c \"import socket,sys;\n"
            "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);\n"
            "s.bind(('0.0.0.0',%d));s.listen(1);\n"
            "c,_=s.accept();c.send(b'ok');c.close()\" >/tmp/srv_%d.log 2>&1 &") % (port, port)
    host.cmd(code)

def udp_srv(host, port):
    code = ("nohup python3 -c \"import socket;\n"
            "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);\n"
            "s.bind(('0.0.0.0',%d));\n"
            "data,a=s.recvfrom(64);s.sendto(b'reply',a)\" >/tmp/usrv_%d.log 2>&1 &") % (port, port)
    host.cmd(code)

def tcp_probe(host, ip, port, timeout=1.2):
    out = host.cmd("python3 -c \"import socket;s=socket.socket();"
                   "s.settimeout(%s);s.connect(('%s',%s));print(s.recv(4));"
                   "s.close()\" 2>&1 || true" % (timeout, ip, port))
    return 'ok' in out or ('b\'ok\'' in out)

def udp_probe(host, ip, port, timeout=1.0):
    out = host.cmd("python3 -c \"import socket;s=socket.socket(socket.AF_INET,"
                   "socket.SOCK_DGRAM);s.settimeout(%s);s.sendto(b'x',('%s',%s));"
                   "print(s.recvfrom(64)[0])\" 2>&1 || true" % (timeout, ip, port))
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
    web = next(h for h in net.hosts if h.IP() == '10.0.40.10')
    iot = next(h for h in net.hosts if h.IP() == '10.0.40.30')

    # start destination servers on web1 + iot1
    for p in (80, 443, 22, 8080):
        tcp_srv(web, p)
    udp_srv(iot, 5000)
    time.sleep(1)

    print('=== L4 TCP ACL: student -> web ===')
    rows = [
        ('student->web:80   ALLOW',   tcp_probe(student, '10.0.40.10', 80)),
        ('student->web:443  ALLOW',   tcp_probe(student, '10.0.40.10', 443)),
        ('student->web:22   DENY',    not tcp_probe(student, '10.0.40.10', 22)),
        ('student->web:8080 DENY',    not tcp_probe(student, '10.0.40.10', 8080)),
    ]
    for name, ok in rows:
        print('%s : %s' % (name, 'PASS' if ok else 'FAIL'))
    print('=== L4 UDP ACL: staff vs student -> iot:5000 ===')
    print('staff->iot:5000  ALLOW : %s' % ('PASS' if udp_probe(staff, '10.0.40.30', 5000) else 'FAIL'))
    print('student->iot:5000 DENY : %s' % ('PASS' if not udp_probe(student, '10.0.40.30', 5000) else 'FAIL'))

    # dump drop flows on s1 for evidence
    p = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', 's1'],
                       capture_output=True, text=True)
    with open(os.path.join(EVID, 'flows_acl_s1.txt'), 'w') as fh:
        fh.write(p.stdout)
    print('--- s1 priority-300 (drop) flows ---')
    for line in p.stdout.splitlines():
        if 'priority=300' in line:
            print(line)
    net.stop()


if __name__ == '__main__':
    main()
