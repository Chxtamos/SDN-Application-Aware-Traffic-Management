# Issue #14 system integration

This check exercises the merged diamond, OpenFlow 1.3 controller, ACLs,
tagged QoS, Issue #12 application workloads, and Issue #13 evidence harness.
It covers the working prototype and policy preservation required by the
instructor. [Issue #14](https://github.com/Chxtamos/SDN-Application-Aware-Traffic-Management/issues/14)
requires the network, controller, and workloads to run together, ACL
checks during policy changes, and reproducible startup and shutdown.

## Scenarios

| Order | Scenario | Workload duration | Required observations |
| --- | --- | --- | --- |
| 1 | Fixed Path A | 12 s | Concurrent HTTP, UDP and TCP. No adaptive event. Correct rules, bidirectional TCP and ACL-probe traffic, and advancing application queues on A. |
| 2 | Adaptive | 24 s | Continuous ACL probes. Sustained offered load moves the active data plane A to B. Releasing the load returns it B to A. Verify events, bidirectional edge and transit rules, and advancing counters at each checkpoint. |
| 3 | Fixed Path A repeat | 12 s | Same settings as the first run with a new directory and stream ID. The same controller port and topology resources must be available again. |

All scenarios enable the existing application-aware QoS. The adaptive run
uses the same uncapped bulk workload as fixed mode, with a longer interval
to exercise policy transitions. The controller's policy is read from its
source and left at its current values. It samples at 1 s, offloads when A
reaches 80% and B is lighter for three qualifying observations, then applies a
10 s cooldown. Recovery requires B strictly below 45% and A below 80% for
three qualifying observations. A threshold failure ends the integration check
and keeps the evidence.

Staff connects to the instrumented file service on TCP 22 and exchanges an
echo with the IoT service on UDP 5001. Student probes those same active
services. Probe outcomes and priority-300 drop deltas are required before
load, under load, and after load. In adaptive mode, each transport and both
roles must also have evidence within the short windows immediately before and
after each observed transition. The probes run continuously across both
transitions. A forbidden connection or reply anywhere in the observed run
fails the check. Temporary allowed-probe timeouts are retained as evidence,
and each observation window must contain both allowed successes. Controller
event observation times come from the harness polling loop.

Queue and bidirectional forwarding assertions use the selected transit
switch and both edge switches. The main UDP generator sends one-way, so its
reverse rule is verified without requiring return packets. The ACL UDP echo
independently exercises return traffic. HTTP and bulk TCP must carry packets
in both directions. Workload concurrency requires overlapping observed HTTP,
UDP, and positive TCP intervals. The overlap calculation accounts for
iperf3's one-second start timestamp precision.

## Prepare and start

The plan command is local and does not need Ryu, Mininet, or root.

```bash
python3 -B -m experiments.integration --plan
```

Obtain approval and exclusive access before SSH or any live run. Prepare a
new isolated workspace on the authorized VM, transfer the working revision
and test fixtures, and verify the transfer SHA-256 manifest. Record the
base revision and any uncommitted patch. The suite also records hashes of
the Python files it uses. Keep the team's existing Ryu Python environment.

Run from the transferred repository. Set RUN_BASE to a new evidence path in
the isolated workspace, and use the verified revision from the transfer
record. If the transfer is an authorized working-tree snapshot, the revision
is its Git base and the transferred file hashes identify its exact content.

```bash
set -euo pipefail
mkdir -p "$RUN_BASE"
"$HOME/ryu-env/bin/python" -B -m unittest discover -s tests -v 2>&1 |
  tee "$RUN_BASE/linux-regression.txt"

sudo -n /usr/bin/python3 -B -m experiments.integration \
  --revision "$TRANSFERRED_REVISION" \
  --controller-port 17656 \
  --ryu-manager "$HOME/ryu-env/bin/ryu-manager" \
  --output-root "$RUN_BASE" 2>&1 | tee "$RUN_BASE/integration-console.txt"
```

Verify there are no unrelated active sessions using Mininet. The runner
checks bridges, interfaces, processes, OVS QoS/Queue records, and the
controller port before starting. It binds Ryu to loopback, waits for the
four switches, and sets up owned, tagged QoS. Conflicting resources stop the
run. Use the documented port only if it is free.

## Shutdown and integrity

The foreground suite shuts down after each scenario. Use Ctrl+C for an
early stop. The harness stops its owned process groups, removes its tagged
queues and QoS, and stops only its Mininet instance. The postflight checks
repeat after the controller is reaped. If any resource check fails, save the
evidence and stop. The suite never uses global Mininet or OVS cleanup.

Before the run, take a read-only SHA-256 inventory of agreed teammate roots
and older Issue #12/#13 evidence. Repeat the same inventory after the run and
compare byte-for-byte. Use the account's existing read access, or the team's
approved read-only sudo approach for root-owned evidence. For example,
capture digests of regular files and the symlink targets without following
them. Scope the paths to the agreed directories and exclude credentials.

```bash
snapshot() {
  sudo -n /usr/bin/python3 -I -B - "$@" <<'PY'
import hashlib
from pathlib import Path
import sys

def visit(path):
    if path.is_symlink():
        print("link", str(path), path.readlink() if hasattr(path, "readlink") else readlink(path))
    elif path.is_dir():
        print("dir", str(path))
        for child in sorted(path.iterdir()):
            visit(child)
    elif path.is_file():
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        print("file", digest.hexdigest(), str(path))
    else:
        print("missing or special", str(path))

from os import readlink
for root in sorted(set(sys.argv[1:])):
    visit(Path(root))
PY
}
# Run before and after the suite against the same agreed path list.
snapshot "$HOME/sdn-controller" "$HOME/sdn-network" "$HOME/ryu-env" \
  "$APPROVED_PRIOR_EVIDENCE_ROOT" > "$RUN_BASE/team-before.txt"
# Repeat with the same arguments into team-after.txt after shutdown.
cmp "$RUN_BASE/team-before.txt" "$RUN_BASE/team-after.txt"
sha256sum "$RUN_BASE/team-before.txt" "$RUN_BASE/team-after.txt"
```

Keep the inventories private if they contain internal names. Preserve a
sanitized comparison result and hashes with the experiment. Verify source
and evidence SHA-256 again after transfer to the local workspace.

## Read the evidence

- suite-config.json holds the revision, source hashes, dependency versions,
  actual controller policy, and settings. suite-result.json identifies all
  scenarios executed and whether source files changed during the run.
- Each scenario has configuration.json, plan.json, commands.jsonl,
  summary.json, integration.json, controller stderr, path-events.json,
  acl-windows.json, ACL probe JSONL, workload JSON and ICMP output, Ryu port
  and flow CSV, and OVS port, flow, and queue snapshots.
- integration.json and postflight outputs identify setup, evidence, or
  cleanup failures. A suite stops after the first failed scenario.
- Local logs and dry-run settings are under
  [evidence/issue14/local](evidence/issue14/local/). Issue #13 captures used
  by the local parser tests are historical. They do not validate the merged
  revision on Mininet.

The integration check does not evaluate the frozen SLA targets under
repeated baseline and adaptive conditions. HTTP response time is an
application measurement, ICMP is post-workload RTT, and UDP delay includes
endpoint processing and scheduling. Retransmissions are a separate TCP
statistic. See [metric definitions](README.md#metric-definitions-and-sla-evaluation).

## Recorded Issue #14 verification

The fresh VM runs used base 39576bc and the working Issue #14 patch
identified in the final source manifest. Linux passed 89 of 89 tests and
the fixed, adaptive, and fixed-repeat scenarios all passed. The first
attempt exposed a Staff TCP echo-timeout classification defect in the
integration verifier. The second attempt included the focused regression
and repeated all three scenarios with equivalent settings.

See [the evidence index](evidence/issue14/README.md) for both attempts and
the offline check of workload, flow, queue and ACL evidence.
