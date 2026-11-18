# Issue #12 schema 3 Mininet regression

The 2026-11-19 run used Ubuntu 20.04.6, Python 3.8.10, Mininet 2.3.1b4,
Open vSwitch 2.13.8, and iperf3 3.7. All 47 Linux tests passed before and after
the live tests. The runs used Path A with QoS enabled and adaptive routing
disabled. The standalone and mixed clients used the same six-second duration,
endpoints, ports, rates, payload sizes, timeouts, and other settings as the
schema 2 experiments. Each UDP run used a fresh stream UUID.

| Scenario | HTTP success / mean response | UDP actual packets/s | UDP sent / received unique / lost | UDP skipped | Minimum sender spacing | TCP RX Mbps / retransmissions |
|---|---:|---:|---:|---:|---:|---:|
| HTTP standalone | 21 / 68.73 ms | — | — | — | — | — |
| UDP standalone | — | 797.98 | 4,788 / 4,782 / 6 | 12 | 0.974 ms | — |
| TCP standalone | — | — | — | — | — | 94.21 / 7 |
| Concurrent 1 | 24 / 28.78 ms | 796.31 | 4,778 / 4,778 / 0 | 22 | 0.966 ms | 87.19 / 0 |
| Concurrent 2 | 24 / 28.66 ms | 794.14 | 4,765 / 4,765 / 0 | 35 | 0.969 ms | 87.07 / 13 |

- All three 800 packets/s scenarios passed the 760–840 packets/s band.
  All attempts belonged to unique nominal slots, with zero local send
  failures. The measured pre-send monotonic timestamp gaps exceeded the
  0.875 ms minimum for every normal UDP run. Standalone sequence loss was
  6/4,788 (0.125%). Both mixed runs had zero sequence loss.
- A 350.69 ms sender pause skipped 14 of 80 slots. The first gap after the
  sender resumed was 25.40 ms, with a 25 ms nominal interval. The sender and
  report emitted partial status for degraded offered load, with zero local
  failures and zero sequence loss.
- The scoped duplicate/drop fixture measured 10 arrivals, nine unique
  packets, one duplicate and one loss from 10 successful sends. The egress
  filter reported one drop. The Ubuntu endpoint captures shared one monotonic
  clock identity, and all matched timestamps satisfied receive-after-send.
- The slow HTTP fixture continued delivering body bytes until deadlines fired
  at 450.98 ms and 450.80 ms for the 450 ms request and workload budgets.
  Both results had body_complete false and body_bytes 0.
- The first standalone HTTP response took 1,037.03 ms and returned HTTP 200
  within the 2-second request budget. The controller recorded initial ARP
  and flow setup at that time. The available logs do not identify the
  precise cause of the delay. The pacing schedule emitted 21 of 24 nominal
  requests, all successful, and the fixture recorded 21 responses. The
  reported mean includes the first request.
- Over approximately 2.4 seconds of overlap per mixed run, HTTP completed
  10 and 9 requests, UDP recorded 1,914 and 1,909 receives, and the TCP data
  sockets advanced by 26.48 MB of acknowledged bytes in each interval.
- Linux unit tests verified rejection of numerically contradictory iperf3
  results. The live iperf3 3.7 client and server summaries passed the
  bytes–seconds–bitrate check.
- OpenFlow packet hits and queue deltas confirmed interactive, periodic,
  and bulk assignments at queues 1, 2, and 3. Tested permitted traffic ran
  successfully. Student traffic to file1 TCP 5201 and iot1 UDP 5000 hit
  the switch's explicit drop rules.
- The experiment controller, bridges, QoS entries, queues, and scoped
  filter were removed. Teammate file manifests agreed across 4,168 entries.

These observations establish behavior for the tested fixed-route QoS setup.
Sender spacing is measured in application timestamps. HTTP times are
client-observed request/response durations. Baseline comparisons, adaptive
routing changes, and SLA validation have separate scopes.

- [Workload metrics and overlap](summary.json)
- [Linux regression before](regression-tests.txt) and [after](regression-after-live.txt)
- [HTTP deadline results](http-deadlines/summary.json), [sender pause](udp-paused/summary.json), and [duplicate/drop results](udp-duplicate-and-loss/summary.json)
- [ACL flow counters](acl/s1-flows.txt) and [cleanup and integrity](cleanup-and-integrity.txt)
- [Runtime environment](environment.txt) and [configuration](configuration.json)
- [VM file hashes](evidence-sha256.txt) and [transfer verification](transfer-back.txt)

The trial folders contain raw application and tool output, command arguments,
clock samples, and applicable queue snapshots. The raw files retain the
original VM bytes. Review ZIP preflight snapshots mask infrastructure
addresses and have separate SHA-256 values.
