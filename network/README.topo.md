# Topology Launch & Verification (Issue #5)

**File:** `network/topology.py` (runnable), `network/topology.mn` (MiniEdit editable)
**Owner:** Member 1

## Reproducibility requirement (from Issue #5)
"Verify that port numbers are stable when the topology is run repeatedly."

To make this deterministic we **start the controller before the hosts** so the order
in which OVS reports ports to the controller is stable. The frozen port map is in
`docs/topology.md`.

## Launch the Python topology

```bash
# 1. Start a remote Ryu controller (member 2's controller app on OF1.3).
#    Example with a stock L2 switch for a quick smoke test:
ryu-manager --ofp-tcp-listen-port 6653 ryu.app.simple_switch_13 &

# 2. Run the topology from a separate terminal (requires Mininet + root):
sudo python3 network/topology.py
#    or via the CLI:
sudo mn --custom network/topology.py --topo sdndiamond --controller remote \
    --link tc,delay=1ms
```

## Launch the MiniEdit file

1. Start MiniEdit: `sudo miniedit`
2. **File → Open** `network/topology.mn`.
3. Verify nodes: s1–s4 (OVS, OpenFlow13), hosts student1/staff1/mgmt1 on s1 and
   web1/file1/iot1 on s4, controller c0 at 127.0.0.1:6653.
4. **Edit → Preferences** confirm these are set: *Start CLI* = on, *OVS* = switch type,
   *OpenFlow 1.3* = protocol.
5. **Run.** In the CLI: `pingall` to confirm the 6 hosts see each other.

> After you finish editing in MiniEdit, **File → Export Level 2 Script** can regenerate
> an equivalent `topology.py` — keep it in sync with the frozen port map.

## Verify stable port numbers

After the network is up, from the Mininet CLI:

```bash
# s1 has 5 links: student1(1), staff1(2), mgmt1(3), s2(4), s3(5) -> 6 ports incl. LOCAL
sh ovs-vsctl show
sh ovs-ofctl -O OpenFlow13 dump-ports s1
sh ovs-ofctl -O OpenFlow13 dump-ports s4
```

**Expected (matches `docs/topology.md §4`):**

| Switch | Key ports | Connects to |
|--------|-----------|-------------|
| s1     | 1,2,3,4,5 | student1, staff1, mgmt1, s2(A), s3(B) |
| s2     | 1,2       | s1(A-in), s4(A-out) |
| s3     | 1,2       | s1(B-in), s4(B-out) |
| s4     | 1,2,3,4,5 | web1, file1, iot1, s2(A), s3(B) |

Re-run the topology twice and diff the `dump-ports` output — they must be identical.

## Evidence to attach to Issue #5
- `sh ovs-ofctl -O OpenFlow13 dump-ports s1` output (screenshot/log)
- `pingall` result showing all 6 hosts reachable
- Two-run port-dump diff proving stability
