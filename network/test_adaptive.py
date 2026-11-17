#!/usr/bin/env python3
"""Adaptive-routing test (issue #10): show Path A -> B switch under congestion.

Boots the diamond against the Ryu controller, starts iperf3 bulk traffic on the
default Path A, then reports when the controller's adaptive logic reroutes to
Path B. Evidence (controller log + port stats csv) is produced on the box.

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


def main():
    setLogLevel('info')
    os.makedirs(EVID, exist_ok=True)
    net = Mininet(topo=SDNDiamondTopo(),
                  controller=lambda n: RemoteController(n, ip=CTRL_IP, port=CTRL_PORT),
                  switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.start()
    time.sleep(6)  # controller handshake + baseline

    # start iperf3 server on file1 (bulk class, staff1 is ALLOWED to file1)
    file1 = next(h for h in net.hosts if h.IP() == '10.0.40.20')
    staff1 = next(h for h in net.hosts if h.IP() == '10.0.20.1')
    file1.cmd('iperf3 -s -D -p 5201')
    time.sleep(1)

    print('=== starting iperf3 bulk (staff1 -> file1) on Path A ===')
    # background iperf client for ~40s
    staff1.cmd('iperf3 -c 10.0.40.20 -p 5201 -t 40 -i 1 >/tmp/iperf_client.log 2>&1 &')

    # sample window: watch controller log for ADAPTIVE switch
    ctrl_log = '/tmp/sdn_evid/controller.log'
    switch_seen = False
    for i in range(30):          # up to 60s
        time.sleep(2)
        if os.path.exists(ctrl_log):
            with open(ctrl_log) as f:
                txt = f.read()
            if 'ADAPTIVE' in txt and 'switch' in txt:
                switch_seen = True
                print('>>> ADAPTIVE switch detected on iter %d' % i)
                # capture flows after switch
                for r in ('s1', 's2', 's3', 's4'):
                    p = subprocess.run(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', r],
                                       capture_output=True, text=True)
                    with open(os.path.join(EVID, 'flows_%s_after.txt' % r), 'w') as fh:
                        fh.write(p.stdout)
                break
    print('switch_seen =', switch_seen)
    # collect client result
    try:
        with open('/tmp/iperf_client.log') as f:
            client = f.read()
    except Exception:
        client = 'no client log'
    print('=== iperf client (tail) ===')
    print('\n'.join(client.splitlines()[-12:]))
    file1.cmd('pkill -f iperf3')
    net.stop()


if __name__ == '__main__':
    main()
