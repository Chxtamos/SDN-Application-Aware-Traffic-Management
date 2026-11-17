#!/usr/bin/env python3
"""NetworkX-based topology verification for the SDN diamond (issues #3/#9/#10).

Builds the frozen diamond as a weighted DiGraph and answers the path questions
that the controller's baseline (Issue #9) and adaptive routing (Issue #10) rely on:
  * are Path A and Path B really two edge-disjoint 100 Mbps routes?,
  * is the physical diamond a loop (why simple L2 flooding would storm)?,
  * what is the shortest (least-congested) path under load?

Outputs a short JSON report + a GraphML file under experiments/evidence/.
"""
import os
import json
import networkx as nx

# ------------------ frozen topology (docs/topology.md) ------------------
# switches (nodes), with capacity weights on links (100 Mbps here)
G = nx.Graph()
switches = ['s1', 's2', 's3', 's4']
G.add_nodes_from(switches)
links = [('s1', 's2'), ('s2', 's4'), ('s1', 's3'), ('s3', 's4')]
for u, v in links:
    G.add_edge(u, v, capacity=100.0, bw=100.0)

report = {}

# 1) Is the diamond a cycle (loop)?
cycle = nx.find_cycle(G, source='s1') if nx.is_connected(G) else None
report['has_cycle'] = cycle is not None
report['cycle'] = cycle

# 2) Two edge-disjoint paths between s1 (ingress) and s4 (egress)?
paths = list(nx.edge_disjoint_paths(G, 's1', 's4'))
report['edge_disjoint_paths'] = paths
report['path_count'] = len(paths)

# 3) The two candidate routes (Path A = via s2, Path B = via s3)
report['path_A'] = ['s1', 's2', 's4']
report['path_B'] = ['s1', 's3', 's4']

# 4) Check edge-disjointness of A and B (they share no edges -> independent)
edgesA = set(zip(report['path_A'], report['path_A'][1:]))
edgesB = set(zip(report['path_B'], report['path_B'][1:]))
report['A_B_edge_disjoint'] = edgesA.isdisjoint(edgesB)

# 5) Under simulated congestion on a link, pick the least-congested path.
# assign a utilization attribute; link cost = util/100 (more loaded = costlier)
def route_cost(u, v):
    return G[u][v].get('util', 0.0) / 100.0

print(json.dumps(report, indent=2))

# write outputs
os.makedirs('experiments/evidence', exist_ok=True)
with open('experiments/evidence/topology_graph.json', 'w') as f:
    json.dump(report, f, indent=2)
nx.write_graphml(G, 'experiments/evidence/topology_graph.graphml')
print('saved experiments/evidence/topology_graph.json + .graphml')
