# Topology Launch & Verification (Issue #5)

**File:** `network/topology.py` (runnable), `network/topology.mn` (MiniEdit editable)
**Owner:** Member 1

## Reproducibility requirement (from Issue #5)
"Verify that port numbers are stable when the topology is run repeatedly."

To make this deterministic:
- **Port numbers are pinned explicitly** in `network/topology.py` with `port1=` /
  `port2=` on every `addLink(...)`, so Mininet cannot reassign them.
- The controller is started before the hosts so OVS reports ports in a stable order.
- Each switch has an explicit `dpid` and `failMode="secure"`.

The frozen port map is in `docs/topology.md`.

## ⚠️ Important — this diamond has a physical L2 loop
`s1-s2-s4-s3-s1` is a loop. Do **not** run `ryu.app.simple_switch_13` or any pure-L2
flooding controller on it: it would create an **ARP/broadcast storm**. Use the team's
**ARP-proxy / baseline controller** (see `docs/topology.md §3.1`, Issue #6) which
answers ARP for the gateway `.254`, does edge MAC rewrite, and installs loop-free
single-path flow rules.

## Launch the Python topology

```bash
# 1. Start the team's controller (member 2). Example entrypoint on OF1.3 port 6653:
ryu-manager --ofp-tcp-listen-port 6653 <team_controller>.py

# 2. Run the topology from a separate terminal (requires Mininet + root):
sudo python3 network/topology.py
#    or via the CLI:
sudo mn --custom network/topology.py --topo sdndiamond --controller remote \
    --link tc,delay=1ms
```

> `--topo sdndiamond` works because `topology.py` registers `topos = {"sdndiamond": SDNDiamondTopo}`.

## Launch the MiniEdit file

1. Start MiniEdit: `sudo miniedit`
2. **File → Open** `network/topology.mn`.
3. Verify nodes: s1–s4 (OVS, OVS switch type, OpenFlow 1.3), hosts
   student1/staff1/mgmt1 on s1 and web1/file1/iot1 on s4, controller c0 as a
   **remote** controller at 127.0.0.1:6653.
4. **Edit → Preferences**: *Start CLI* = on, *Switch type* = OVS, *OpenFlow 1.3* = on.
5. **Run.** Use the controller from step 1 and check connectivity between ALLOWED hosts
   only (see `docs/security-policy.md`). `pingall` will **not** all succeed on its
   own: cross-subnet traffic requires the ARP-proxy controller, and DENY pairs are
   intentional.

> After editing in MiniEdit, **File → Export Level 2 Script** can regenerate an
> equivalent `topology.py`. Because MiniEdit does not persist the `port1/port2`
> pinning or the `defaultRoute` for hosts, keep those in `network/topology.py` as
> the source of truth and re-apply them after any MiniEdit export.

## Verify stable port numbers

After the network is up, from the Mininet CLI:

```bash
sh ovs-vsctl show
sh ovs-ofctl -O OpenFlow13 dump-ports s1
sh ovs-ofctl -O OpenFlow13 dump-ports s4
```

**Expected (matches `docs/topology.md §4`):**

| Switch | Key ports | Connects to |
|--------|-----------|-------------|
| s1     | 1,2,3,4,5 | student1, staff1, mgmt1, s2(A), s3(B) |
| s2     | 1,2       | s1(A-in), s4(A-out)  (internal bridge port is OVS LOCAL=4294967294) |
| s3     | 1,2       | s1(B-in), s4(B-out)  (internal bridge port is OVS LOCAL=4294967294) |
| s4     | 1,2,3,4,5 | web1, file1, iot1, s2(A), s3(B) |

Re-run the topology twice and diff the `dump-ports` output — they must be identical.

## Evidence to attach to Issue #5
- `sh ovs-ofctl -O OpenFlow13 dump-ports s1` output (screenshot/log).
- `sh ovs-ofctl -O OpenFlow13 dump-ports s4` output.
- A two-run port-dump diff proving stability.
- Connectivity evidence for the ALLOWED pairs only, using the team's ARP-proxy
  controller (see `docs/security-policy.md`), since `pingall` across all 6 hosts is
  not a valid L2 smoke test on this L3-routed, looped diamond.
