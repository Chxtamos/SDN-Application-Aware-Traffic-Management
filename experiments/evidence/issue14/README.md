# Issue #14 live system integration evidence

## Verified revision

- Branch feat/issue-14-system-integration, with base
  39576bc541034502d551e6d086183cfc36d9a892. The runtime and tests include the
  uncommitted Issue #14 patch. The input manifest identifies the exact revision.
- Ubuntu 20.04.6 LTS, Python 3.8.10, Ryu 4.34, Mininet 2.3.1b4,
  Open vSwitch 2.13.8 with OpenFlow 1.3, and iperf3 3.7.
- Final Linux regression passed 89/89 tests with zero failures, errors or
  skips. The final Windows check passed 88/89, with one Linux-only skip.
- The successful VM attempt verified all 81 input files before and after
  running. The final integration guide was updated after the test. The
  Python sources still match the tested inputs byte for byte.

## Fresh results

| Scenario | Concurrent overlap (s) | HTTP successes / mean (ms) | UDP send (pkt/s) | UDP lost/sent | TCP receive (Mbps) | Student timeouts | Staff replies |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| fixed-a | 10.819 | 45 / 63.95 | 787.07 | 0/9445 | 87.169 | 60 | 185 |
| adaptive | 22.008 | 93 / 58.88 | 790.87 | 11/18981 | 86.863 | 86 | 224 |
| fixed-a-repeat | 11.000 | 48 / 40.72 | 790.90 | 1/9491 | 87.078 | 60 | 210 |

All three scenarios passed. The adaptive controller logged A to B with
A at 96% and B at 0%, then B to A with both paths at 0%. The figures were
rounded by the controller. The observer recorded both events about
24.09 seconds apart. OpenFlow and queue snapshots separately confirmed
the forwarding path. On B, s1 queues 1, 2 and 3 advanced by 50, 2,074 and
18,041 packets at the active checkpoint. The recovered A checkpoint
confirmed both directions for the Staff TCP and UDP echo services.

Student probes timed out in all 206 attempts. Matching priority-300 TCP
and UDP drop counters advanced at all checkpoints. Staff TCP and UDP had
successful echoes in all required windows, including both sides of the
two adaptive events. The final adaptive run retained one Staff TCP
handshake whose echo timed out and one other Staff timeout. Those
observations did not count as successful echoes.

The fixed runs used equal configurations and different run/stream IDs.
The same port and topology names were reused after verified teardown.
Ryu port telemetry covered all four switches in every run. Flow telemetry
covered the exercised switches, including both transit switches during the
adaptive scenario.

## First attempt and correction

Attempt 1 passed Linux 88/88 and the first fixed run. The controller moved
A to B and B to A in the adaptive run. The verifier stopped the suite
because it treated any Staff TCP handshake without an echo as a fixture
error. The saved probe records contained two such observations. Replaying
the records reproduced the verifier error. Each of the seven observation
windows independently contained real Staff echoes and Student timeouts.

A focused regression test reproduced the error before the fix. The fix
retains a Staff TCP handshake with no echo as a transient observation.
The window checker still requires genuine Staff echoes, and a successful
Student handshake still fails ACL verification. The controller, topology,
QoS settings, ACLs and workload generators kept their original behavior.
The same full suite passed on attempt 2. Both attempts remain available.

## Integrity and review

The two VM evidence transfers passed SHA-256 verification before and after
extraction. The protected VM manifest matched for 6,593 entries. The fixed
repeat and the final resource checks found no reserved controller port,
experimental processes, interfaces, bridges, QoS records or queues. The
final VM network namespace IDs matched the starting set. The final SSH
session closed cleanly. The original Windows worktree comparison matched
2,439 files, including 537 Issue #12 evidence files.

- [Final Linux regression log](live/attempt-2/linux-regression.txt)
- [Final environment versions](live/attempt-2/environment.json) and [input manifest](live/attempt-2/SOURCE-SHA256.txt)
- [Suite result with scenario directories](live/attempt-2/runs/issue14-suite-20261119T043505Z-91dkzty4/suite-result.json)
- [Verification from raw sources](fresh-verification.json) and [offline verifier](verify-evidence.py)
- [First-attempt status](live/attempt-1/attempt.json), [RED](local/acl-probe-regression-red.txt), and [GREEN](local/acl-probe-regression-green.txt)
- [Transfer verification](live/transfer-verification.json), [VM integrity](live/attempt-2/teammate-integrity.json), and [Windows integrity](windows-worktree-integrity.json)
- [Startup and shutdown procedure](../../INTEGRATION.md)

Local and historical Issue #12 and #13 regression fixtures are distinct
from fresh Issue #14 VM evidence. The scenarios are integration smoke
tests. They do not demonstrate full SLA compliance, exhaustively cover
the ACL matrix, or measure worst-case transient interruption. The bare UDP
workload is one-way. The separate echo fixture proves bidirectionality.
HTTP application time and post-workload ICMP RTT do not measure one-way
path delay. Issue #15 owns the repeated baseline/adaptive comparison.
