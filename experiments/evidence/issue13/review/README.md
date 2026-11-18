# Issue #13 review evidence

The source on this branch matches the independently approved implementation.
The independent verdict was **APPROVE**. The review archive containing the
final regression passed CRC and **1,661/1,661 SHA-256 manifest checks**.
Its SHA-256 is `15595686dbbe7251c38a8697a66a44434bf5f11ef18688ef97de203ae5459eb0`.

## Final Linux regression

- [Complete regression log](../remediation/final-linux/regression-linux.txt)
- [Counts](../remediation/final-linux/regression-counts.json) and [environment](../remediation/final-linux/environment.json)
- [Approved input verification](../remediation/final-linux/source-verification.json) and [input hashes](../remediation/final-linux/input-sha256.txt)
- [VM integrity](../remediation/final-linux/vm-team-integrity.json) and [local integrity](../remediation/final-linux/local-integrity.json)
- [UDP matrix RED](udp-matrix-red.txt) and [GREEN](udp-matrix-green.txt)
- [Offline replay of 38 evidence sets](offline-replay.txt)

Ubuntu 20.04.6 LTS, Python 3.8.10, and Ryu 4.34 ran the final approved
revision. The result was **76/76 passed**, with zero failures, errors or
skips. All **24/24** UDP matrix cases passed.

## Historical Mininet verification

The five scenarios below ran on an earlier revision. Offline replay
confirmed the same metrics, telemetry and statuses on the approved
revision. Each directory contains workload and ICMP data, configuration,
recorded commands, summary, and Ryu and OVS statistics.

- [Standalone HTTP](mininet/http-only/summary.json)
- [Standalone UDP](mininet/udp-only/summary.json)
- [Standalone TCP](mininet/tcp-only/summary.json)
- [Concurrent trial 1](mininet/concurrent-1/summary.json)
- [Concurrent trial 2](mininet/concurrent-2/summary.json)
- [Historical independent verification](mininet/historical-verification.txt)
- [Supplemental cases](mininet/supplemental-cases.jsonl) and [ACL flow counters](mininet/acl/acl-s1-flows.stdout.txt)

This repository copy contains selected evidence. Placeholders replace local
and VM filesystem paths in the published copies. The complete review
archive holds the historical outputs and documented redactions.

HTTP response time includes request processing and transfer of the complete
response body. ICMP reports post-workload RTT. UDP application delay uses a
shared monotonic clock and includes endpoint scheduling. TCP retransmissions
are reported separately from packet loss. See the [measurement guide](../../../README.md).
