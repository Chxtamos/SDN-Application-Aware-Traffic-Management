
#!/usr/bin/env python3

from network.topology import SDNDiamondTopo, LINK_BW


class QoSLabTopo(SDNDiamondTopo):

    def addLink(self, node1, node2, *args, **opts):

        edges = {"s1", "s4"}
        transit = {"s2", "s3"}

        if node1 in edges and node2 in transit:
            opts.pop("bw", None)
            opts.pop("delay", None)

            opts["params1"] = {}
            opts["params2"] = {
                "bw": LINK_BW,
                "delay": "2ms"
            }

        elif node1 in transit and node2 in edges:
            opts.pop("bw", None)
            opts.pop("delay", None)

            opts["params1"] = {
                "bw": LINK_BW,
                "delay": "2ms"
            }
            opts["params2"] = {}

        return super().addLink(
            node1, node2, *args, **opts
        )


if __name__ == "__main__":
    topo = QoSLabTopo()

    print("=== QoS Lab Topology ===")
    print("Hosts:", topo.hosts())
    print("Switches:", topo.switches())

    for a, b in topo.links():
        print(a, "<->", b, topo.linkInfo(a, b))

    assert len(topo.hosts()) == 6
    assert len(topo.switches()) == 4
    assert len(topo.links()) == 10

    print("PASS: Topology validation")
    print("No live network was created.")
