#!/usr/bin/env bash
set -euo pipefail
port=17654
echo "--- sessions ---"
who
echo "--- experiment-related processes ---"
matches=$(ps -eo pid=,ppid=,user=,comm=,args= | awk '$4 != "awk" && $4 != "ps" && $0 ~ /(^|[ \/])(ryu-manager|mnexec|iperf3|ofprotocol|ovs-testcontroller|mininet:[[:space:]]|mn)([[:space:]]|$)/ {print $1,$2,$3,$4}')
printf '%s\n' "$matches"
echo "--- listening ports ---"
ss -H -lntu
echo "--- bridges ---"
bridges=$(sudo -n ovs-vsctl --timeout=5 list-br)
printf '%s\n' "$bridges"
echo "--- QoS records ---"
qos=$(sudo -n ovs-vsctl --timeout=5 --bare --no-headings --columns=_uuid,type list QoS)
printf '%s\n' "$qos"
echo "--- queues ---"
queues=$(sudo -n ovs-vsctl --timeout=5 --bare --no-headings --columns=_uuid list Queue)
printf '%s\n' "$queues"
echo "--- Mininet-named interfaces ---"
links=$(ip -o link show | awk '$2 ~ /^(s[1-4]|student1|staff1|mgmt1|web1|file1|iot1)(-eth[0-9]+)?[@:]/')
printf '%s\n' "$links"
echo "--- network namespaces ---"
sudo -n lsns -t net -o NS,PID,USER
conflict=$(ss -H -lnt "sport = :$port")
if [[ -n "$matches$bridges$qos$queues$links$conflict" ]]; then
    echo 'Conflict detected. Stopping before live testing.'
    exit 2
fi
printf 'PASS: no conflicting resources found; controller port %s clear.\n' "$port"
