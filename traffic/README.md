# Traffic generators

Run these workloads inside the existing Mininet host namespaces from the repository
root. They use Python 3.8+ and its standard library. The TCP bulk workload also
requires the VM's existing `iperf3` executable on `PATH`. Use the same client
arguments, receiver setup, and launch offsets for baseline and adaptive trials.

| Class | Client role/address | Service | Configurable load |
|---|---|---|---|
| Interactive | student1 `10.0.10.1`, staff1 `10.0.20.1`, mgmt1 `10.0.30.1` | web1 `10.0.40.10`, TCP 80 or 443 | GET interval and duration |
| Periodic | staff1 `10.0.20.1` or mgmt1 `10.0.30.1` | iot1 `10.0.40.30`, UDP 5000–5100 | Payload bytes, packets per second, duration |
| Bulk | staff1 `10.0.20.1` or mgmt1 `10.0.30.1` | file1 `10.0.40.20`, TCP 5201 | Duration and optional TCP target bitrate |

The CLI binds to the configured host IP and restricts endpoints to the
forwarding rules in [the current controller](../controller/ryu_controller.py).
The ACL and classifiers enforce policy independently.

## Run a trial

Start the services in their respective host namespaces before starting clients.
Use a unique run directory for each trial and put the output of each command in
its own file. The examples use paths relative to the repository root for
readability; redirect into your run directory in practice.

| Namespace | Service command |
|---|---|
| web1 | `python3 -B -m traffic http-server --duration 25 > web-server.json` |
| iot1 | `python3 -B -m traffic udp-receive --stream-id 18d20a90-81fa-41ed-bec8-915b7454d225 --source 10.0.20.1 --port 5000 --duration 25 > udp-receiver.json` |
| file1 | `python3 -B -m traffic bulk-server --timeout 30 > bulk-server.json` |

The HTTP and UDP services write a JSON `ready` event to stderr once bound.
Check the file1 listener with `ss -ltn 'sport = :5201'` inside its namespace.
Start the clients soon after readiness, with time for all clients to finish
within the server windows. For mixed traffic, start these commands concurrently
in their respective namespaces and record the actual offsets. In the Mininet CLI
prefix each command with the host name and append `&` for concurrent execution.

| Namespace | Client command |
|---|---|
| student1 | `python3 -B -m traffic http --source 10.0.10.1 --interval 0.25 --duration 10 --timeout 2 > http.json` |
| staff1 | `python3 -B -m traffic udp-send --stream-id 18d20a90-81fa-41ed-bec8-915b7454d225 --source 10.0.20.1 --port 5000 --payload-size 1200 --rate-pps 800 --duration 10 > udp-sender.json` |
| mgmt1 | `python3 -B -m traffic bulk --source 10.0.30.1 --duration 10 --target-bps 0 > bulk.json` |

Finish the UDP report after both endpoint commands exit. This command only
reads the files and can run outside Mininet.

```sh
python3 -B -m traffic udp-report --sender udp-sender.json --receiver udp-receiver.json > udp.json
python3 -B -m traffic --help
python3 -B -m unittest discover -s tests -p 'test_traffic.py'
```

The UDP pair must share a stream UUID, the sender IP, and the destination port.
Assign a different UUID to each trial. Concurrent UDP pairs need distinct iot1
ports. Use one HTTP fixture for concurrent HTTP clients. The bundled one-off
iperf3 listener serves one bulk client per launch on file1:5201. The three traffic
classes can run together. Schedule additional bulk trials as separate launches
with the available listener. Use the built-in HTTP fixture on port 80, or use
`http --port 443` with an existing HTTPS service whose certificate validates
for web1's IP. The HTTP fixture serves a fixed body.

## Output contract

Each command writes one JSON object on stdout with `schema_version` (currently 3),
`workload`, `command` (replay arguments), `config` (effective options), wall-clock
`started_at_unix_ns` and `ended_at_unix_ns`, monotonic `elapsed_s`, and `status`.
`observed` holds endpoint observations and raw tool output. `reported` holds
normalized iperf3 metrics. `calculated` holds rates and loss derived by this CLI.
Exit codes are 0 for `ok`, 1 for `partial` or `error`, 2 for invalid CLI arguments,
and 130 for `interrupted`. Use exit codes, readiness events, and separate output
files when integrating with the experiment harness. The final result is written
at process exit, and an `interrupted` run may contain incomplete observations.

### HTTP

The CLI records request counts, response codes, failures, body lengths, and
timestamps. `response_time_ms` is client-observed time from the start of a
fresh GET through completion or failure, including connection setup. Each
request is budgeted to the smaller of `--timeout` and the remaining `--duration`.
`body_bytes` counts a fully read, validated body. A timed-out, truncated, or
oversized body has `body_complete: false` and `body_bytes: 0`, even if part
of the body reached the client. A valid empty body has `body_complete: true`.
On the Linux CLI main thread, an interval timer enforces this deadline during
blocking connect and response reads. The HTTP fixture also bounds its serve
window on that path. When the interval timer is unavailable or already in use,
the socket timeout and stage checks provide best-effort enforcement. A peer
that keeps producing data can overrun the budget in that fallback. The harness
should use an external process deadline with time to exit and flush JSON.
Operating system scheduling and cleanup can delay process exit.

### UDP

The 24-byte payload header contains the stream UUID and unsigned 64-bit
sequence. In schemas 2 and 3 the sequence is the zero-based nominal slot index.
The sender targets absolute slot deadlines from the monotonic workload start.
Expired slots are skipped. For ordinary wake and recording overhead the next
send can start up to 30% of an interval earlier than a full post-send wait,
with a maximum correction of 0.5 ms. After a missed slot or a long send pause,
the sender waits at least one full interval. It sends at most once per slot.

The normal Mininet scenario targets 800 packets/s with a proposed tolerance
of ±5%, or 760–840 packets/s. Six seconds at 800 packets/s gives 4,800 slots,
so packet-count granularity is small. This tolerance accommodates routine host
jitter while keeping the offered load comparable across routing and QoS runs.
The CLI exports the target, achieved rate and offered-load status for each UDP
sender. A nonzero sender completing outside the tolerance is `partial`
with exit 1. A sender with no successful sends is `error`. Short windows
with few nominal slots can leave the tolerance due to packet granularity.
The schema 3 report retains the sender's partial status while calculating
loss and throughput for successful sends.

| Field | Definition |
|---|---|
| `scheduled_packets` | One nominal packet per slot. Computed as the ceiling of the configured window divided by the rounded interval. |
| `attempted_packets` | Number of send calls, including local failures. |
| `skipped_intervals` | Nominal slots that elapsed without a send attempt. Equals `scheduled_packets` minus `attempted_packets`. |
| `sent_packets` | Calls accepted by the local socket with the configured datagram length. |
| `send_failure_count` | Local send failures. Equals `attempted_packets` minus `sent_packets`. |
| `received_packets` | Receiver records that matched the configured stream and source address, including duplicate arrivals. |
| `received_unique_packets` | Unique successful sender sequences matched by source port, sequence, and payload length. |
| `lost_packets`, `loss_percent` | Unmatched successful sends and their percentage of successful sends. Skipped slots and local send failures are tracked separately. |

Payload size includes the UUID and sequence header. Throughput uses payload
bytes × 8 divided by the measured monotonic sender window, with matched
unique bytes on the receive side. Compare configured and achieved rates
using both endpoints. Start capture before sending and leave a tail for late
packets. Mininet endpoints share a system clock for the capture coverage
check.

`udp-report` validates the sender and receiver counters against the packet
records and rejects inconsistent windows, lengths, sender sequences, timestamps
and duplicate JSON fields. A matching arrival timestamp earlier than its
send timestamp is rejected, including duplicate arrivals.

Schema 3 uses shared Linux monotonic time when the endpoints have the same boot
and time namespace, represented by a clock ID in each result. A different
known clock ID or a schema 3 capture paired with a legacy capture is rejected.
If both schema 3 endpoints lack a clock ID, the report records
`utc_same_host_fallback` and assumes comparable UTC timestamps on the same host.
Legacy schemas 1 and 2 use UTC on the same host and permit no negative timing
offset. Schema 1 has attempt sequence numbers and returns `null` for slots
and skipped intervals. Resynchronize the clock and repeat if wall-clock
adjustments invalidate a legacy or fallback capture.

### Bulk

iperf3 provides sender and receiver throughput in bits/s and byte counts.
The client reports sender TCP retransmissions when the tool supplies the
field. A missing retransmission field is exported as `null`. Both roles
validate the TCP result summary. Required throughput values must be finite
and nonnegative, while byte counts and supplied retransmission counts must
be nonnegative integers. Each endpoint's bytes, duration and bitrate must
be mathematically consistent within iperf3's output rounding. A nonempty
transfer requires positive duration and rate. Zero bytes and zero rate are
valid, including a zero-duration summary. Malformed tool output yields a structured status
of `error` and retains diagnostic text or the parsed tool object. The wrapper
bounds the invocation and reaps the child it launched. `target_bps=0` requests
uncapped TCP. See the [iperf3 manual][iperf].

## Live verification

The [schema 2 Mininet regression](../experiments/evidence/issue12/schema2/README.md)
passed 38 Linux tests before and after the standalone and concurrent trials.
The live probes verified HTTP deadlines, UDP missed slots and duplicate/loss
accounting, both iperf3 roles, traffic overlap, ACLs, and QoS queue assignment.
Use the measured offered and achieved rates when comparing workloads.
The [schema 1 validation](../experiments/evidence/issue12/README.md) documents
the earlier implementation and its original results.

[iperf]: https://software.es.net/iperf/invoking.html
