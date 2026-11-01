# Project plan — CPE494 SDN Topic 1

**Deadline:** 11 Dec 2026 (submit ZIP to LEB2). **Team:** 3 members. **Status:** Planning; do not mark work done until tested.

## Roles
- Member 1: MiniEdit/Mininet topology, IP/port mapping, OVS, access-policy enforcement.
- Member 2: Ryu/OpenFlow controller, classification, monitoring and adaptive decisions.
- Member 3: traffic generation, experiment automation, results, integration support.
- All: architecture, design justification, integration, report, slides and video.

## Timeline
| Dates | Phase | Exit criteria |
|---|---|---|
| 1–3 Nov | Requirements & design | Three classes, policy, IP/ports, architecture approved |
| 4–12 Nov | Network & tooling | MiniEdit export, OVS–Ryu connection, connectivity |
| 4–20 Nov | Controller and traffic (parallel) | Stats, classification, baseline, adaptation |
| 21–25 Nov | Integration | Modules run together; access restrictions preserved |
| 26 Nov–2 Dec | Evaluation | Baseline vs adaptive, repeated tests and plots |
| 3–10 Dec | Delivery | Report, PDF slides, video, code/config and ZIP ready |

## First three actions
1. Freeze requirements and class/security policy.
2. Draw architecture/topology; agree IP, port mapping and bandwidth.
3. Verify MiniEdit/Mininet/OVS/Ryu compatibility and run connectivity smoke test.

## Milestones
M1 Design (3 Nov); M2 Network (12 Nov); M3 Controller (20 Nov); M4 Integration (25 Nov); M5 Evaluation (2 Dec); M6 Submission-ready (10 Dec).

## Working agreement
Move issues through Todo → In Progress → In Review → Done. Link commits/PRs to issues. Put test evidence in issue comments. Record blockers and owners. Never commit secrets or other groups' work.
