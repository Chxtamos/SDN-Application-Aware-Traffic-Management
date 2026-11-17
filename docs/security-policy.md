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

## 5. Example flow (enforcement intent)

- Student → web1 (TCP 443): ALLOW → class Interactive → high QoS queue → egress to Path A
  (or Path B if congested). Policy preserved on both paths.
- Staff → iot1: ALLOW read-only.
- Student → file1 (TCP 21): DENY → high-priority drop at s1.

---

## 6. Cross-references
- Topology/subnets: `docs/topology.md` (Issue #3)
- Classification/QoS: `docs/requirements-slacls.md` (Issue #1), `docs/architecture.md` (Issue #2)
