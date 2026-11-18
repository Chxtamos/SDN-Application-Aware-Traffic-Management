# Experiments

Issue #13 provides a Mininet harness and an offline reader for its evidence.
The harness uses the existing diamond, Ryu controller, ACLs, QoS runtime, and
Issue #12 traffic commands. One invocation runs one scenario. The comparison
of fixed routing and adaptive routing belongs to Issue #15.

## Linux and Mininet verification

Use the team's Ubuntu 20.04 and Python 3.8 environment. Run from a dedicated
checkout with exclusive access to the VM. Use the installed Ryu environment
for the controller and regression suite. The live harness uses the system
Python with Mininet, OVS, iperf3, iproute2, and iputils ping.

```sh
~/ryu-env/bin/python -B -m unittest discover -s tests -v
python3 -B -m experiments.harness summarize \
  experiments/evidence/issue12/schema3/concurrent-1 \
  --workloads http udp bulk --controller-dir experiments/evidence/issue12/schema3

VERIFY=$(mktemp -d "$PWD/issue13-verify.XXXXXX")
for workloads in '["http"]' '["udp"]' '["bulk"]' '["http","udp","bulk"]'; do
  printf '{"workloads":%s,"duration_s":6,"qos":true,"adaptive":false}\n' "$workloads" > "$VERIFY/config.json"
  sudo /usr/bin/python3 -B -m experiments.harness run \
    --config "$VERIFY/config.json" --output-root "$VERIFY/runs" \
    --ryu-manager "$HOME/ryu-env/bin/ryu-manager" || break
done
```

The first stdout line gives the run directory. Read each `summary.json`, verify the
controller port and flow rows and the before/after OVS snapshots, and inspect
the traffic JSON and the `commands.jsonl` launch times. The `run` command
requires root, so use `sudo` to read the generated files when necessary.
Repeat the mixed scenario to check that a fresh directory and stream UUID
are used. Compare the actual traffic start/end timestamps and per-event data
when demonstrating overlap. Each class starts after the listeners are ready.

## Configuration

Pass a JSON object. Unspecified entries take the following defaults.

| Key | Default | Meaning |
|---|---|---|
| `workloads` | `["http","udp","bulk"]` | Distinct classes to start together |
| `duration_s` | `6` | Whole seconds for each selected client, from 1 to 86400 |
| `http_interval_s`, `http_timeout_s` | `0.25`, `2` | Interactive interval and per-request deadline |
| `udp_rate_pps`, `udp_payload_bytes`, `udp_port` | `800`, `1200`, `5000` | Target rate, UDP payload including the sequence header, and service port (5000–5100) |
| `bulk_target_bps` | `0` | iperf3 sender target in bits/s. Zero selects uncapped TCP |
| `controller_port` | `17656` | Free, unprivileged loopback OpenFlow port |
| `qos`, `adaptive` | `true`, `false` | Existing Ryu settings. QoS also applies the existing tagged OVS configuration |

These scenarios use the approved pairs student1→web1 TCP 80, staff1→iot1 UDP,
and mgmt1→file1 TCP 5201. Each run uses a new Mininet topology and its own Ryu
process. Preflight checks the loopback port, bridges, interfaces, experiment
processes, and QoS records. The harness reaps its process groups, removes only
its tagged QoS records, and stops the Mininet instance it created.
HTTP and UDP readiness use the Issue #12 listener events. Bulk readiness
uses `ss` in file1 to preserve iperf3's one-off listener. All selected listeners
share a five-second readiness budget. A polling loop supervises independent monotonic deadlines for clients and
services. It sends termination signals without waiting through another
process's grace period. Completion first observed at or after its deadline
is a timeout. A readiness result must arrive within the readiness budget.
The receiver stays open past the sender window. Polling and signal delivery
depend on host scheduling. Linux cleanup signals the process group before
reaping its leader and therefore keeps the group ID reserved. Children that
deliberately detach into another session are outside that group.

## Evidence and status

- `configuration.json` gives the effective configuration. `plan.json` gives
  namespace, argv, and time budgets for all planned workload roles.
  `commands.jsonl` records executed commands, controller settings, readiness,
  timestamps and exit codes. A run-scoped stream ID appears in both UDP commands.
- Each workload leaves its Issue #12 JSON and stderr. Raw ICMP and OVS output
  and stderr use `.stdout.txt` and `.stderr.txt`. The harness captures OpenFlow
  1.3 port and flow dumps before and after, plus queue counters when QoS is on.
- Ryu writes `sdn_stats.csv` and `sdn_flows.csv` into the run directory. The
  summary inventories their rows and datapaths. The flow match field contains
  unescaped commas in the existing format. Split each line at the first seven
  commas to preserve the match. The files hold cumulative counters, and port
  `util_pct` is calculated by Ryu. Interpret counter deltas per switch and port,
  taking resets, sampling intervals and route changes into account.
- `summary.json` classifies the outcome as `ok` (exit 0), `degraded` (exit 1),
  `incomplete` (exit 2), or `failed` (exit 3). Degraded runs can retain valid
  partial workload measurements, including UDP below its offered-load band.
  A timeout or missing artifact leaves the result incomplete unless another
  failure supersedes it. Invalid CLI input exits 2 before starting a scenario.
  Unavailable measurements use `null` or have no field if the corresponding
  observation never occurred.
- Workload status is checked against raw HTTP and UDP records. When command
  observations are available, the CLI exit code must agree with the workload
  JSON. An executed ICMP probe requires a valid count and loss summary and an
  RTT summary when there are replies. A total loss has a null RTT. Historical
  replay accepts directories that contain no ICMP probe.

## Metric definitions and SLA evaluation

| Metric | Provenance and calculation |
|---|---|
| HTTP request and failure counts | Observed at the client. HTTP response time is client-measured request-to-completed-body time, including connection establishment. The harness calculates mean, min, max and nearest-rank p95 for successful complete responses. |
| UDP throughput and packet loss | Calculated from successful sender records and distinct matched receiver sequences. Goodput is received payload bytes × 8 / measured sender seconds. Loss % is 100 × (successful sends − distinct matched receives) / successful sends. Skipped send opportunities and socket failures have separate counts. |
| UDP one-way application delay | Calculated from first matched receive minus send monotonic timestamp, divided by one million for ms. Both endpoints need the same comparable clock. Delay variation uses absolute differences between consecutive received sequences in send order. These samples include host processing and scheduling. |
| TCP throughput and retransmissions | Reported by iperf3. Preserve sender and receiver directions and byte counts. Retransmissions are a TCP metric. Application packet loss remains unavailable for this workload. |
| ICMP probes | Three probes per selected source/service pair after the workload. Packet counts come from the ping output. RTT aggregates are reported by ping, and ICMP loss is calculated from probe and reply counts. These post-workload probes measure connectivity and RTT at probe time. |

The frozen interactive targets are 50 ms one-way delay, 30 ms jitter, and
0.1% packet loss. Evaluating them needs synchronized, per-packet interactive
measurements under load and an explicit jitter definition. HTTP request time
and ICMP RTT serve different measurements. For periodic traffic, compare
per-interval delivered payload rate and matched loss with the 5 Mbps and 1%
targets, then compare same-clock UDP delay samples with 200 ms. This harness
reports whole-window goodput and loss plus raw per-packet timestamps for
interval analysis. For bulk use iperf3 receiver throughput against 30 Mbps
when headroom permits and inspect service metrics under competition.
An SLA conclusion also requires repeated equivalent trials and ACL checks
across path transitions as specified in the frozen requirements.

The offline `summarize` command accepts Issue #12 schema 3 workload artifacts.
Provide `--controller-dir` when Ryu telemetry is available separately. It
recomputes packet matching and metrics from the workload artifacts. Review
`summary.json` and `commands.jsonl` from the live harness for execution and
cleanup outcomes.
