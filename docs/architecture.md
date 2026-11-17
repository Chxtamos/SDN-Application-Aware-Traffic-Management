# Architecture & Data/Control Flow (Issue #2)

**Milestone:** M1 Design Complete
**Owner:** All members
**Status:** ✅ Design approved

This document describes the software architecture: the modules, how monitoring data
flows into policy decisions, and how those decisions are enforced. It is the blueprint
referenced by the controller (Issue #7–#10), monitoring (Issue #8) and QoS (Issue #11)
implementation issues.

---

## 1. Components

| Component | Owner | Where | Responsibility |
|-----------|-------|-------|----------------|
| Ryu controller (OpenFlow 1.3) | Member 2 | controller/ | Negotiate with OVS, install/remove flow rules, adaptive routing, ACL enforcement |
| Classification module | Member 2 | controller/ | Map flows to Interactive/Periodic/Bulk (IP/port/protocol) |
| Monitoring / stats collector | Member 2 | controller/ | Poll OVS port & flow counters, compute link utilization |
| Policy / decision engine | Member 2 | controller/ | Hysteresis-based adaptive reroute decision; consult ACL |
| QoS / queue manager | Member 1 | controller/ | Create OVS QoS queues, map classes to queues |
| Network / topology (OVS, MiniEdit) | Member 1 | network/ | Diamond topology, IP/port map, link capacities |
| Traffic generators | Member 3 | traffic/ | Repeatable interactive/periodic/bulk workloads |
| Experiment harness | Member 3 | experiments/ | Drive scenarios, collect raw logs, produce plots |

---

## 2. Data flow — monitoring

```
OVS ports/flows
   │  (OFPT_MULTIPART_REQUEST: PORT_STATS / FLOW_STATS)
   ▼
Ryu OFPStateChangeHandler → stats queue
   │
   ▼
Monitoring module (periodic poll, e.g. every 1 s)
   │  raw counters: rx_bytes, tx_bytes, rx_packets, flow byte_count
   ▼
Link-utilization computation:
   util(link) = (Δbytes × 8) / (Δt × capacity_bits)
   │
   ▼
Policy/decision engine (thresholds + hysteresis)
   ▼
Adaptive-routing action → controller installs/removes flow rules
```

**Rules of thumb (per `monitoring/README.md`):**
- Always keep **raw counters** separate from **computed utilization**.
- Sample **Δt** is fixed (1 s) and recorded alongside counters for reproducibility.
- Hysteresis (Issue #10) prevents flapping: enter congestion at high threshold, exit at
  a lower (different) threshold.

---

## 2.1 L3 reachability (ARP proxy + edge MAC rewrite)

This is a Layer-2 OVS fabric with four `/24` subnets and a gateway `.254` per subnet
(decision A in `docs/topology.md §3.1`). The controller is the **gateway** for every
subnet:

- **ARP Proxy Responder** — answers ARP for `10.0.10/20/30/40.254` with the edge
  switch's own MAC (s1 for sources, s4 for services). ARP is never flooded across the
  diamond loop.
- **Edge MAC rewrite** — when a packet crosses subnets, the controller rewrites
  `eth_src`/`eth_dst` at the ingress edge (s1) to the destination MAC and installs
  `set_field` actions, so hosts see their gateway MAC and services see the sender MAC
  without L2 flooding.
- **Loop-free install** — flow rules follow a single chosen path (A or B); no broadcast
  frame crosses `s1-s2-s4-s3-s1`, so the physical loop is never used as a forwarding
  loop.

Consequence: **do not run `ryu.app.simple_switch_13`** on this diamond (pure-L2
flooding cannot route across `/24` and would cause an ARP/broadcast storm). The team's
baseline / ARP-proxy controller implements this behaviour (Issue #6).

---

## 3. Data flow — policy decision & enforcement

```
Packet-in / new flow
   │
   ▼
CLASSIFICATION (IP/port/protocol → class)
   │
   ▼
ACL CHECK (src/dst subnet per security-policy.md)  ──deny──▶ drop rule
   │ allow
   ▼
ROUTING DECISION (Path A default; switch to B when congested, hysteresis)
   │
   ▼
QoS QUEUE ASSIGNMENT (class → queue id, per-traffic-class SLAs)
   │
   ▼
Install flow rule(s): priority / match / actions(goto queue, output port)
```

**Flow-table layering (shared ownership across controller modules):**
- **High priority** — ACL deny/allow rules (top of table; admin traffic never blocked).
- **Medium priority** — class/queue + steering rules (classification, QoS).
- **Low priority** — default/fallback forwarding.
Ownership of each table slice is written in `controller/README.md` so modules do not
overwrite each other.

---

## 4. Interfaces

| Interface | Protocol | Direction | Purpose |
|-----------|----------|-----------|---------|
| Ryu ↔ OVS | OpenFlow 1.3 (TCP 6633/6653) | control | flow install, stats, packet-in |
| Ryu ↔ Ryu modules | internal Python API | app | stats sharing, queue mgmt |
| Monitoring → decision engine | in-process queue | app | utilisation inputs |
| Decision engine → tables | in-process calls | app | install/modify/delete rules |
| Traffic generators → hosts | socket-level (iperf/ping/custom) | data-plane | generate classes |
| Experiment harness ↔ controller | REST/RESTConf (Ryu `ofctl_rest`) or log files | side | trigger/measure scenarios |

---

## 5. Key design decisions / trade-offs

1. **Hybrid over QoS-only** — see `docs/requirements-slacls.md` (Issue #1).
2. **Source-subnet ACL** — ACLs keyed on source subnet (role) at ingress (s1), so policy
   is enforced exactly once before rerouting, and adaptive steering can never bypass it.
3. **Hysteresis for adaptation** — avoids route flapping under borderline load.
4. **Fixed sample interval** — deterministic monitoring for reproducible experiments.
5. **Deterministic port numbering** — start controller before hosts so ports are stable.

---

## 6. Cross-references

- `docs/topology.md` (Issue #3) for the exact network.
- `docs/security-policy.md` (Issue #4) for the ACL matrix.
- `controller/README.md`, `monitoring/README.md`, `traffic/README.md` for module
  responsibilities.
