# Requirements & Traffic-Class SLAs — FROZEN (Issue #1)

**Milestone:** M1 Design Complete
**Owner:** All members (Member 1 coordinates)
**Status:** ✅ Also approved via Issue #1 — Hybrid approach chosen
**Decision recorded:** **Hybrid = Adaptive Routing + QoS** (not QoS-only).

---

## 1. Background / goal

Provide application-aware traffic management on an OpenFlow 1.3 OVS topology so that
three service classes meet their performance targets under competing load, while an
access policy (Student/Staff/Management) is always respected.

## 2. Approach decision — Hybrid (Adaptive Routing + QoS)

| Option | Description | Verdict |
|--------|-------------|---------|
| QoS-only | Only queueing / per-class rate shaping on fixed paths | Rejected |
| Hybrid | Adaptive rerouting between Path A/B **plus** per-class QoS queues | **Chosen** |

Rationale: with two identical-capacity paths (A/B), adaptive steering adds a second
degree of freedom (divide interactive/periodic from bulk) that QoS alone cannot provide,
and it directly demonstrates "application-aware traffic management" — the course topic.

## 3. Traffic classes and requirements

### Class 1 — Interactive (voice / video / web)
- Example flows: HTTPS web browsing, WebRTC/RTP voice+video.
- Ports: TCP 80, 443; UDP/RTP 16384–32767.
- Requirements: **latency-critical**, jitter-sensitive, low loss.
- QoS: highest priority queue; adaptive routing keeps it on the least-loaded path.

| SLA metric | Target |
|-----------|--------|
| One-way delay | ≤ 50 ms |
| Jitter | ≤ 30 ms |
| Packet loss | ≤ 0.1 % |

### Class 2 — Periodic (monitoring / telemetry)
- Example flows: SNMP polls, MQTT sensor/telemetry, IoT keepalives.
- Ports: UDP 161/162 (SNMP), UDP 5000–5100 (MQTT/telemetry).
- Requirements: **reliable small payloads** on a regular cadence; must not be starved
  by bulk; small guaranteed bandwidth.

| SLA metric | Target |
|-----------|--------|
| Guaranteed minimum bandwidth | ≥ 5 Mbps |
| Packet loss | ≤ 1 % |
| One-way delay | ≤ 200 ms |

### Class 3 — Bulk (transfer / backup)
- Example flows: FTP/SFTP large file transfers, backups, big HTTP downloads.
- Ports: TCP 20, 21, 22; TCP 8000–9000 (HTTP download).
- Requirements: **elastic / best-effort**, bandwidth-hungry, tolerant of delay.

| SLA metric | Target |
|-----------|--------|
| Throughput (headroom permitting) | ≥ 30 Mbps |
| Delay / jitter | best-effort (no hard target) |
| Loss | acceptable under congestion (lowest priority) |

> The three classes map to the three service endpoints in `docs/topology.md`.

## 4. Acceptance criteria for experiments

A class meets its SLA if, in a repeated, identical baseline-vs-adaptive test:

1. **Interactive**: one-way delay ≤ 50 ms and loss ≤ 0.1 % measured end-to-end even
   while Bulk saturates a path.
2. **Periodic**: ≥ 5 Mbps sustained and loss ≤ 1 % throughout the run.
3. **Bulk**: keeps utilising remaining capacity and never starves Interactive/Periodic.
4. **Access policy**: no Student/Staff/Management ACL violation occurs on any reroute
   (verify both Path A and Path B and at controller-reload boundaries).
5. **Repeatability**: running the same scenario twice yields results within a defined
   tolerance (loss/threshold/utilisation within ±5 % or documented).

## 5. Out of scope (non-goals)

- Inter-domain / WAN routing, firewalling beyond the student/staff/management ACL,
  DPI-based deep packet inspection beyond port/IP classification, and physical hardware
  deployment (Mininet/OVS emulation only).

## 6. Cross-references

- Topology & endpoints: `docs/topology.md` (Issue #3)
- Architecture: `docs/architecture.md` (Issue #2)
- ACL matrix: `docs/security-policy.md` (Issue #4)
- Evaluation plan: `experiments/README.md`
