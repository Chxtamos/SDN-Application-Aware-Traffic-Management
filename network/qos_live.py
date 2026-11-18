
#!/usr/bin/env python3

import uuid

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

from network.qos_lab_topology import QoSLabTopo
from network.qos_runtime import ovs, apply, cleanup


def main():
    if ovs("list-br"):
        raise RuntimeError("Existing OVS bridges detected. Stop test.")

    run_id = "issue11-" + uuid.uuid4().hex[:12]

    setLogLevel("info")

    net = Mininet(
        topo=QoSLabTopo(),
        controller=lambda name: RemoteController(
            name, ip="127.0.0.1", port=16653
        ),
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        build=False
    )

    attempted = False

    try:
        net.build()
        net.start()

        if not net.waitConnected(timeout=15):
            raise RuntimeError("Controller connection failed")

        attempted = True
        apply(run_id)

        print("\n=== QoS installed successfully ===")
        print("Run ID:", run_id)

        CLI(net)

    finally:
        try:
            if attempted:
                cleanup(run_id)
        finally:
            net.stop()


if __name__ == "__main__":
    main()
