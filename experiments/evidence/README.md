# Controller Evidence (issues #7, #8, #10 follow-up)

Raw logs captured on mininet@100.68.254.36 (Mininet 2.3.1b4, OVS 2.13.8, Ryu 4.34).

## Connectivity / ACL (issues #7)
- connectivity_evidence.txt: ALLOWED pairs reply (2/3, first ICMP = flow-install cold
  start), DENIED pairs dropped (0 replies) with high-priority DROP flows installed.
- controller_run.txt: ARP-proxy / ROUTE / ACL-DENY / PORT-STATS lines.

## Stats (issue #8)
- stats.csv: per-port raw counters (rx/tx bytes, packets) + utilisation + active path,
  1 s samples. flows.csv: flow-stats (priority 200/300) packet/byte counts.

## Adaptive routing (issue #10)
- adaptive/adaptive_test.txt: iperf3 bulk on Path A -> "ADAPTIVE switch detected"
- adaptive/flows.csv, iperf_client.txt, stats.csv path column (A->B movement)
- controller log shows: "ADAPTIVE: switch A -> B (util A=96% B=0%)" then
  "ADAPTIVE: now on path B (cooldown 10.0s)"; iperf3 sender ~92 Mbit/s.
