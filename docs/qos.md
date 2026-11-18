
# OVS QoS and Traffic Shaping — Issue #11

## Objective

Implement application-aware bandwidth allocation using
Open vSwitch linux-htb queues and OpenFlow 1.3.

## Queue Configuration

| Queue | Traffic | Min Rate | Max Rate | Priority |
|---|---|---|---|---|
| 0 | Default | 1 Mbps | 100 Mbps | 3 |
| 1 | Interactive | 30 Mbps | 100 Mbps | 0 |
| 2 | Periodic | 5 Mbps | 100 Mbps | 1 |
| 3 | Bulk | 30 Mbps | 100 Mbps | 2 |

The total reserved bandwidth is 66 Mbps.
Unused bandwidth can be borrowed by active queues.

## Implementation

- OVSDB-based queue configuration.
- OpenFlow OFPActionSetQueue.
- Bidirectional traffic classification.
- QoS configuration on both Path A and Path B.
- QoS disabled by default.
- Configurable adaptive routing.
- Run-specific experimental log directories.
- Tagged QoS records for targeted cleanup.
- Separate QoS-compatible Mininet topology.

## Automated Testing

10 out of 10 unit tests passed.

## Experimental Environment

- Ubuntu 20.04.6 LTS
- Python 3.8.10
- Open vSwitch 2.13.8
- OpenFlow 1.3
- Ryu Controller
- Mininet
- Link capacity: 100 Mbps

## Experimental Results

| Metric | Baseline | QoS |
|---|---|---|
| UDP packets sent | 24000 | 24000 |
| UDP packets received | 23951 | 23986 |
| UDP packets lost | 49 | 14 |
| UDP packet loss | 0.204% | 0.058% |
| UDP payload rate | 7.664 Mbps | 7.676 Mbps |
| Bulk throughput | 90.2 Mbps | 90.3 Mbps |
| TCP retransmissions | 531 | 14 |

## Observations

UDP packet loss decreased by 71.43%.
TCP retransmissions decreased by 97.36%.

Bulk throughput remained approximately unchanged.
The periodic traffic met the minimum bandwidth
and packet-loss targets in both conditions.

With QoS enabled, all three application classes
were observed using their assigned queues.

## Security

The tested allowed ICMP pairs remained reachable.
Student-to-file ICMP traffic remained blocked.

## Limitations

These results represent preliminary experiments.
Adaptive routing was disabled in both conditions.

The baseline and QoS measurements used different
UDP ports within the same traffic class.

Interrupted or failed baseline attempts were excluded
from the comparison. Baseline telemetry and cumulative
queue counters may include those earlier attempts.

Repeated controlled experiments, comprehensive ACL
testing, and adaptive QoS integration remain necessary.

## Evidence

See experiments/evidence/issue11/.

Raw telemetry is stored in compressed CSV files.
The inherited flow-log format may contain unescaped
commas in match fields.

## Cleanup

All experimental bridges, QoS records and queues
were removed after testing.

## Conclusion

The implementation demonstrates application-aware
queue assignment and bandwidth allocation.
The preliminary results suggest improved traffic
protection under competing workloads.

## Running the QoS Experiment

Use a dedicated working directory and obtain exclusive
access to the shared Mininet VM before live testing.
Do not overwrite existing OVS configurations.

Terminal 1 (QoS controller):

```bash
RUN_DIR=$(mktemp -d /tmp/issue11-qos.XXXXXX)
SDN_RUN_DIR="$RUN_DIR" SDN_QOS=1 SDN_ADAPTIVE=0 \
~/ryu-env/bin/ryu-manager \
--ofp-listen-host 127.0.0.1 \
--ofp-tcp-listen-port 16653 \
controller/ryu_controller.py
```

Terminal 2 (Mininet):

```bash
sudo python3 -B -m network.qos_live
```

For the baseline, use a separate run directory and
set SDN_QOS=0 and SDN_ADAPTIVE=0.

Both conditions retain HTB and the same topology.
The baseline sends application traffic to Queue 0.

The dry-run scripts are for inspection only.
Do not execute the untagged commands printed by
network/qos_plan.py on a shared network.

Exit the Mininet CLI before stopping the controller.
Verify that experimental OVS resources are removed.

## Additional Evaluation Limitations

HTTP response time is not equivalent to one-way delay.
The UDP measurement represents average received
payload rate over a nominal 30-second window.

One-way delay, jitter, sustained bandwidth per interval,
and repeated SLA verification remain future work.

The successful baseline retry reused a running topology,
so its initial flow state differed from the QoS trial.
The comparison is preliminary, not a controlled proof
of causal performance improvement.

