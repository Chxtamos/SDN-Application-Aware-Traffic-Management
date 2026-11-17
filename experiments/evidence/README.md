# Controller Evidence (issues #7, #8, #10 follow-up)

Raw logs captured on mininet@100.68.254.36 (Mininet 2.3.1b4, OVS 2.13.8, Ryu 4.34)
after all review fixes (incl. _packet_out OFPP_CONTROLLER root cause).

## Connectivity / ACL (issue #7)
- connectivity_evidence.txt: ALLOWED 4 pairs OK (3/3 replies - first-packet forward
  works, no cold-start loss), DENIED 2 pairs dropped (0 replies).
- adaptive/acl_test.txt: ALL pairs PASS -
  student->web:80/443 ALLOW PASS, web:22/8080 DENY PASS, staff->iot:5000 ALLOW PASS,
  student->iot:5000 DENY PASS.
- adaptive/flows_acl_s1.txt: priority-300 L4 drop flows with tp_dst match.

## Stats (issue #8)
- adaptive/stats.csv: per-port raw counters (rx/tx bytes, packets) + utilisation +
  active path, 1 s samples. adaptive/flows.csv: flow-stats with cookie + match.

## Adaptive routing (issue #10)
- adaptive/controller_adaptive.txt:
  [..] ADAPTIVE: switch A -> B (util A=96% B=0%)
  [..] ADAPTIVE: now on path B, re-installed 1 active flow(s) (cooldown 10.0s)
  [..] ADAPTIVE: switch B -> A (util A=0% B=96%)     <- hysteresis both directions
  [..] ADAPTIVE: now on path A, re-installed 1 active flow(s)
- stats.csv s2 max 96.2%, s3 max 96.9% (traffic moves to Path B then back).
- ACL DENY ... on s4 count = 0 (race fixed).
- iperf_client.txt: sustained throughput, "iperf Done" (not stuck at 0).

## Topology graph analysis (NetworkX)
- topology_graph.json/.graphml: diamond is a cycle (has_cycle=true), s1->s4 has
  2 edge-disjoint paths, Path A (s1-s2-s4) and Path B (s1-s3-s4) share no edges
  (A_B_edge_disjoint=true) - the property adaptive routing depends on.
