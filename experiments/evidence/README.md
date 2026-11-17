# Controller Evidence (issues #6-#10)

Raw logs captured on the mininet box (`mininet@100.68.254.36`), Mininet 2.3.1b4 +
OVS 2.13.8 + Ryu 4.34.

| File | Source | Proves |
|------|--------|--------|
| `connectivity_evidence.txt` | `verify_connectivity.py` ping results | #6, #7 |
| `controller_run.log` | Ryu controller stdout | #6 ARP proxy, #7 ACL/classify, #8 stats |
| `stats.log` | /tmp/sdn_stats.log | #8 port stats + link utilisation |

## Result summary
- **#6 Connectivity:** all 4 ALLOWED pairs replied (2/3, first ICMP = flow-install cold
  start); both DENIED pairs got 0 replies (dropped).
- **#7 ACL/classify:** 4 ROUTE messages + 4 ACL-DENY messages in the run log.
- **#8 Stats:** port rates/util logged on every sample (2 s interval) across s1-s4.
- **#9 Baseline:** L3 forward+return flow rules installed with edge MAC rewrite,
  loop-free.
- **#10 Adaptive:** hysteresis + cooldown logic present; enable `adaptive_enabled`
  for a congestion-driven run.
