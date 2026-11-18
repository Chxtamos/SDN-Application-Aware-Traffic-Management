# Issue #12 live validation evidence

Captured on 2026-11-18 in an isolated VM workspace with exclusive access. The
run reused the Issue #11 QoS topology and left adaptive routing disabled.
All five workloads used Path A. Each client ran for six seconds. The mixed
trials used equivalent workload settings with distinct UDP stream UUIDs.

| Trial | HTTP 200 | Mean HTTP response (ms) | UDP sent / received / lost | UDP loss | UDP TX / RX (Mbps) | TCP receiver (Mbps) | TCP retransmissions |
|---|---:|---:|---:|---:|---:|---:|---:|
| HTTP only | 24 | 20.24 | — | — | — | — | — |
| UDP only | — | — | 4,772 / 4,765 / 7 | 0.147% | 7.635 / 7.624 | — | — |
| Bulk only | — | — | — | — | — | 94.28 | 0 |
| Concurrent 1 | 24 | 27.65 | 4,787 / 4,787 / 0 | 0% | 7.659 / 7.659 | 86.92 | 13 |
| Concurrent 2 | 24 | 23.13 | 4,778 / 4,778 / 0 | 0% | 7.645 / 7.645 | 87.19 | 7 |

In each mixed trial, nine complete HTTP requests and more than 1,900 UDP
sends and receives fell between two TCP socket snapshots roughly 2.4 seconds
apart. Acknowledged data increased by about 26.5 MB on the iperf3 data
connection between those snapshots. The individual and concurrent trials
each showed an increase in the intended `s1` Path A queue: 1 for HTTP, 2 for
UDP, and 3 for TCP bulk. The ACL probes observed both Student drops at `s1`
and successful traffic on the permitted pairs.

- [Aggregate results and overlap windows](summary.json)
- [Trial configuration](configuration.json) and [validation entry point](run-live.sh)
- [Concurrent 1 queue deltas](concurrent-1/queues-delta.json) and [Concurrent 2 queue deltas](concurrent-2/queues-delta.json)
- [ACL verdicts](acl/summary.json) and [matching OpenFlow entries](acl/s1-flows.txt)
- [VM environment](environment.txt), [regression log before live testing](regression-tests.txt), and [regression log after live testing](regression-after-live.txt)
- [Connection and resource checks before live testing](preflight-before-live.txt), [resource checks after cleanup](post-cleanup.txt), and [teammate integrity comparison](cleanup-and-integrity.txt)
- [Source transfer hashes](transfer-sha256.txt), [source verification after live testing](source-after-live.txt), and [Windows transfer verification](transfer-back.txt)

Each trial directory contains the exact command argument vectors, host
namespace names, startup timestamps, raw generator JSON, and queue counters.
The raw iperf3 JSON records sender and receiver rates at their respective
observation points. The table uses the receiver rate for delivered throughput.
The HTTP timings are client-observed application response times. UDP TX and RX
are payload rates over the measured sender window. The fixed-route, short
runs verify workload behavior. Baseline comparisons, adaptive transitions,
one-way delay, and a formal SLA evaluation require separate experiments.

To reproduce the validation on an approved and exclusively available VM,
place the project checkout in an isolated workspace's `repo/` directory.
Create sibling `state/` and `evidence/` directories. Copy `preflight.sh` and
`teammate-manifest.py` to `state/`, and `validation-driver.py` to `evidence/`.
From this evidence directory, run `bash run-live.sh <absolute-workspace-path>`
as the Mininet user with its existing `ryu-env` and passwordless Mininet sudo.
The driver reuses the frozen Mininet switch names and exits if the preflight
finds conflicting resources or if controller port 17653 is occupied.

The transfer manifest covers the files used during the VM run. The traffic
usage guide was updated afterward to link to these results.
