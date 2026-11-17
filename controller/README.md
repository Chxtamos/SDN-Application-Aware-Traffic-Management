# Ryu Controller (issues #6, #7, #8, #9, #10)

Owner: Member 2 (with Member 1 for network integration).

Single self-contained OpenFlow 1.3 app: `controller/ryu_controller.py`.

## What it does
| Issue | Feature | Where |
|-------|---------|-------|
| #6 | ARP proxy for the four gateways (.254) + routed connectivity | `_handle_arp` |
| #7 | Traffic classification (Interactive/Periodic/Bulk) + high-priority ACL (default-deny) | `classify`, `_acl_allowed`, `_route_ip` |
| #8 | Port/flow stats + link utilisation (logged to /tmp/sdn_stats.log) | `_port_stats`, `_monitor_loop` |
| #9 | Fixed baseline: L3 flow rules on Path A (loop-free, bidirectional, edge MAC rewrite) | `_install_baseline`, `_install_l3_flows` |
| #10 | Adaptive rerouting Path A<->B with hysteresis + cooldown | `_decide_reroute`, `_switch_path` |

## Design (Decision A)
Hosts sit in four /24 subnets behind gateway .254. The controller is the gateway:
- answers ARP for .254 (ARP proxy),
- installs bidirectional L3 flows **keyed on dst-IP** with **edge MAC rewrite**
  (`set_field(eth_dst=…)`), and
- routes on a single chosen path (Path A by default) so the physical L2 loop is never
  flooded (loop-free, Issue #9).

> Matching IPv4 requires `eth_type=0x0800` in the OFPMatch — OVS drops ipv4 rules without it.

## Run (on the mininet box, as the mininet user)
```bash
PATH=$PATH:$HOME/.local/bin \
ryu-manager --ofp-tcp-listen-port 6653 controller/ryu_controller.py
```
Then boot the topology: `sudo python3 network/topology.py`.

## Verify (issues #6-#9 evidence)
```bash
cd /home/mininet/sdn-network
sudo python3 verify_connectivity.py
```
Expected: ALLOWED pairs reply, DENIED pairs dropped; flows + port stats recorded in
`/tmp/sdn_evid/*`. Evidence is stored under `experiments/evidence/`.

## Adaptive routing (#10)
`adaptive_enabled` defaults to `False` so the baseline test is stable. Set it to
`True` (and wire `path_util` sampling) to exercise congestion-driven reroute with
hysteresis (enter ≥ 80% util, clear ≤ 45%) and a 10 s cooldown.
