# Security Policy — Student/Staff/Management ACL Matrix (Issue #4)

**Milestone:** M1 Design Complete
**Owner:** Members 1 and 2
**Status:** ✅ Frozen 2026-11-03

This matrix defines which traffic each role (Student / Staff / Management) may send
toward the service endpoints. It is enforced at the ingress switch **s1** with
**high-priority** OpenFlow flow rules, and **must never be bypassed** by adaptive
routing (Issue #4 acceptance: verify on both Path A and Path B and after
controller reload).

---

## 1. Roles and source subnets

| Role | Source subnet | Example host |
|------|---------------|--------------|
| Student | 10.0.10.0/24 | student1 |
| Staff | 10.0.20.0/24 | staff1 |
| Management | 10.0.30.0/24 | mgmt1 |

## 2. Service endpoints (from `docs/topology.md`)

| Service | Host IP | Ports | Class |
|---------|---------|-------|-------|
| web1 | 10.0.40.10 | TCP 80, 443 | Interactive |
| file1 | 10.0.40.20 | TCP 20, 21, 22 | Bulk |
| iot1 | 10.0.40.30 | UDP 5000–5100, UDP 161/162 | Periodic |

---

## 3. ACL matrix (allow / deny)

Access is **default-deny** for anything not listed.

| Source \ Target | web1<br>(80/443) | file1<br>(20/21/22) | iot1<br>(UDP 5000-5100,161/162) |
|------------------|------------------|---------------------|----------------------------------|
| **Student** (10.0.10.0/24) | ✅ **ALLOW** HTTP/HTTPS only | ⛔ **DENY** (no direct file/backup) | ⛔ **DENY** |
| **Staff** (10.0.20.0/24) | ✅ **ALLOW** HTTP/HTTPS | ✅ **ALLOW** SFTP/FTP (read+write) | ✅ **ALLOW** read-only telemetry |
| **Management** (10.0.30.0/24) | ✅ **ALLOW** | ✅ **ALLOW** full access | ✅ **ALLOW** full read/write/config |

### Legend
- **ALLOW** – install a steer/queue flow rule for the matching traffic.
- **DENY** – install a **high-priority drop** rule; matching traffic is dropped at s1.
- Anything not in the matrix is dropped (default-deny).

---

## 4. Enforcement notes

1. **Default-deny base rule** is always present (lowest-priority catch-all drop) so no
   unlisted flow slips through on any path.
2. **High-priority allow rules** for the listed Student/Staff/Management-to-service flows
   carry the class + QoS queue + steering actions.
3. **Priority ordering on s1:** adminController/LLDP/ARP are top; then ACL allow vs deny;
   then default-deny. Deny/allow ACL entries are **above** any steering rules so adaptive
   rerouting cannot accidentally bypass a policy decision.
4. Adaptive routing (Issue #10) only changes the **egress port** of already-ALLOWED flows;
   ACL classification happens first and is path-independent. Verified in Issue #4 by
   pinging allowed pairs (success) and denied pairs (timeout) on both Path A and Path B.

## 5. Control-plane traffic policy (ARP, ICMP, return traffic)

The controller acts as the gateway for each subnet (see `docs/topology.md §3.1`), so
forwarding policy also covers control-plane frames:

### ARP
- The controller is the **ARP Proxy Responder** for the four gateway addresses
  (`10.0.10.254 / .20.254 / .30.254 / .40.254`). It answers any ARP request for a
  gateway from a host on that subnet with the edge switch's own MAC.
- ARP requests **between** subnets are never flooded across the diamond. They are
  answered locally (proxy) or dropped, so ARP is not a broadcast vector on the L2 loop.
- Policy: **allow** ARP only between a host and its own subnet gateway; **drop** ARP
  replies/cross-subnet ARP that would traverse the transit segment.

### ICMP (ping / measurement)
Used for reachability and RTT/continuity checks.
- **ALLOW** ping between an allowed source role and an allowed service, in **both**
  directions (echo request `s1→s4` and echo reply `s4→s1`), consistent with the matrix
  below.
- **DENY** ping from any role to a service they are not allowed to reach (e.g.
  `student1 → file1`), and any ping between peer hosts in the same edge unless already
  allowed.
- When using ping to measure latency, only fire it at **allowed** pairs so the results
  are not polluted by ACL drops.

### Return traffic rules (`s4 → s1`)
Every ALLOWED forward flow must have a matching **return flow** installed (reverse
5-tuple), so replies from a service reach the source host. Keyed on the SAME matrix:
- Return for `Student→web1` (TCP 443): allow `web1→student1` on the reverse path.
- Return for `Staff→file1/sftp` and `Staff→iot1`: allow reverse as well.
- No return rule exists for DENY pairs (student1↔file1 etc.) — both directions dropped.
- Adaptive rerouting must steer forward and return flows on the **same** Path (A or B)
  so the reverse direction is never left without a rule.

---

## 7. Example flow (enforcement intent)

- Student → web1 (TCP 443): ALLOW → class Interactive → high QoS queue → egress to Path A
  (or Path B if congested). Policy preserved on both paths.
- Staff → iot1: ALLOW read-only.
- Student → file1 (TCP 21): DENY → high-priority drop at s1.

---

## 8. Cross-references
- Topology/subnets: `docs/topology.md` (Issue #3)
- Classification/QoS: `docs/requirements-slacls.md` (Issue #1), `docs/architecture.md` (Issue #2)
