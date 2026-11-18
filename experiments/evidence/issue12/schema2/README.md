# Issue #12 schema 2 Mininet regression

The 2026-11-18 run used Ubuntu 20.04.6, Python 3.8.10, Mininet 2.3.1b4,
Open vSwitch 2.13.8 and iperf3 3.7. All 38 unit tests passed before and after
the live tests. Path A carried the workloads with QoS enabled and adaptive
routing disabled. The three standalone runs and two equivalent mixed runs
used six-second client workloads. The concurrent runs used different UDP
stream UUIDs and the same workload and service settings.

| Scenario | HTTP 200 / mean response | UDP sent / received unique / lost | UDP skipped | UDP RX Mbps | TCP RX Mbps / retransmissions |
|---|---:|---:|---:|---:|---:|
| HTTP standalone | 24 / 20.67 ms | — | — | — | — |
| UDP standalone | — | 3,999 / 3,995 / 4 (0.100%) | 801 | 6.392 | — |
| TCP standalone | — | — | — | — | 94.32 / 4 |
| Concurrent 1 | 24 / 31.12 ms | 3,868 / 3,868 / 0 | 932 | 6.189 | 88.55 / 18 |
| Concurrent 2 | 24 / 29.69 ms | 3,864 / 3,864 / 0 | 936 | 6.182 | 88.25 / 16 |

- HTTP received headers and continuing body bytes in both deadline probes.
  The per-request deadline fired at 450.93 ms and the remaining-workload
  deadline at 451.00 ms, against a 450 ms budget.
- The scoped UDP pause skipped 14 of 80 slots with zero local socket failures
  and zero network loss. The smallest recorded spacing met the 24 ms test
  threshold for a 25 ms configured interval.
- The duplicate/loss fixture sent an additional copy of sequence 0 and applied
  an egress drop for sequence 1 on the isolated sender interface. The receiver
  recorded 10 arrivals, nine unique sequences, one duplicate and one lost
  sequence. The egress filter recorded one drop.
- Each mixed run had nine complete HTTP requests with UDP traffic and
  increasing TCP acknowledgements in the same roughly 2.4-second window.
  The TCP data socket gained 26.87 MB and 26.91 MB of acknowledgements in
  those intervals. Queue counters and matching OpenFlow actions verified
  queues 1, 2 and 3 for interactive, periodic and bulk traffic.
- The Student-to-file TCP and Student-to-IoT UDP probes were denied. Student
  HTTP, Staff UDP, and Staff and Management TCP were permitted.
- The Linux suite tested rejection of malformed iperf3 results. The live
  iperf3 client and server produced valid schema 2 envelopes in the bulk runs.

The configured UDP rate was 800 packets/s. Achieved sender rates were
643.96–666.48 packets/s. The skipped slots quantify the scheduling shortfall
independently of network loss. HTTP reports client-observed response time.
These runs verify the fixed QoS scenario. Baseline comparisons and adaptive
route transitions require separate evidence.

- [Aggregate raw-derived results](summary.json)
- [Linux regression before tests](regression-tests.txt) and [after tests](regression-after-live.txt)
- [Runtime versions](environment.txt) and [scenario configuration](configuration.json)
- [HTTP deadline results](http-deadlines/summary.json), [UDP pause results](udp-paused/summary.json), and [duplicate/loss filter counters](udp-duplicate-and-loss/egress-filter.txt)
- [ACL verdicts](acl/summary.json) and [OpenFlow ACL evidence](acl/s1-flows.txt)
- [Resource cleanup and teammate workspace comparison](cleanup-and-integrity.txt)
- [Source transfer SHA-256](transfer-sha256.txt), [raw VM evidence SHA-256](evidence-sha256.txt), and [Windows transfer verification](transfer-back.txt)

Each trial directory includes launch commands, raw JSON, timing, and the
applicable before/after queue snapshots. The evidence archive was transferred
with all VM hashes intact. The review ZIP has an additional manifest for
the packaged copies, including privacy redactions to three preflight logs.
The previous schema 1 experiment remains in the parent evidence directory.
