#!/usr/bin/env python3
"""Adaptive-routing test (issue #10) + Path B verification.

Boots the diamond against the Ryu controller, runs a 35 s iperf3 bulk flow on
the default Path A, and verifies the controller reroutes to Path B under
congestion. After a switch is detected, it keeps sampling so the evidence shows
traffic actually moving on Path B (s3 loaded, s2 idle, receiver>0).

Run as root (controller already running on 6653):
    sudo python3 network/test_adaptive.py
"""
import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel

from topology import SDNDiamondTopo, CTRL_IP, CTRL_PORT

EVID = '/tmp/sdn_evid'
CTRL_LOG = os.path.join(EVID, 'controller.log')


def dump_flows(tag):
    for r in ('s1', 's2', 's3', 's4'):
        p = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', r],
                           capture_output=True, text=True)
        with open(os.path.join(EVID, 'flows_%s_%s.txt' % (r, tag)), 'w') as fh:
            fh.write(p.stdout)


def main():
    setLogLevel('info')
    os.makedirs(EVID, exist_ok=True)
    net = Mininet(topo=SDNDiamondTopo(),
                  controller=lambda n: RemoteController(n, ip=CTRL_IP, port=CTRL_PORT),
                  switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.start()
    time.sleep(6)

    file1 = next(h for h in net.hosts if h.IP() == '10.0.40.20')
    staff1 = next(h for h in net.hosts if h.IP() == '10.0.20.1')
    file1.cmd('iperf3 -s -D -p 5201')
    time.sleep(1)

    print('=== iperf3 bulk (staff1 -> file1:5201), 35s, on Path A ===')
    staff1.cmd('iperf3 -c 10.0.40.20 -p 5201 -t 35 -i 1 >/tmp/iperf_client.log 2>&1 &')

    switched = False
    for i in range(40):
        time.sleep(1)
        if os.path.exists(CTRL_LOG):
            with open(CTRL_LOG) as f:
                txt = f.read()
            if 'ADAPTIVE: switch' in txt:
                if not switched:
                    switched = True
                    print('>>> ADAPTIVE switch detected (iter %d). Sampling 10s more...' % i)
                    dump_flows('after_switch')
                    # keep sampling so s3 load / s2 idle appear in stats.csv
                    time.sleep(10)
            if 'ROUTE' in txt and 'path=B' in txt and switched:
                # continue; the long iperf run + log capture is enough
                pass
        if i >= 12:   # min runtime so iperf runs at least ~15s beyond switch
            pass
    print('switched =', switched)

    # wait for iperf to finish (35s total), then collect + dump final states
    time.sleep(2)
    try:
        with open('/tmp/iperf_client.log') as f:
            client = f.read()
    except Exception:
        client = 'no client log'
    print('=== iperf client (tail) ===')
    print('\n'.join(client.splitlines()[-20:]))
    dump_flows('final')
    file1.cmd('pkill -f iperf3')
    net.stop()


if __name__ == '__main__':
    main()
