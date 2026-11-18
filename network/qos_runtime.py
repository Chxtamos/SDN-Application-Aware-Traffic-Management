
#!/usr/bin/env python3

import subprocess
import shlex

from network.qos_plan import QOS_PORTS, QUEUES, LINK_CAPACITY


def ovs(*args):
    return subprocess.check_output(
        ["ovs-vsctl", "--timeout=5", *args],
        text=True
    ).strip()


def build_apply(run_id):
    cmd = ["ovs-vsctl", "--timeout=5"]

    for i, port in enumerate(QOS_PORTS):
        qos = f"@qos{i}"

        cmd.extend([
            "--", "wait-until", "Port", port, "qos=[]",
            "--", "set", "Port", port, f"qos={qos}",
            "--", f"--id={qos}", "create", "QoS",
            "type=linux-htb",
            f"other-config:max-rate={LINK_CAPACITY}",
            f"external_ids:sdn-run={run_id}",
            f"external_ids:sdn-port={port}",
        ])

        for qid in QUEUES:
            cmd.append(f"queues:{qid}=@q{i}_{qid}")

        for qid, cfg in QUEUES.items():
            cmd.extend([
                "--", f"--id=@q{i}_{qid}", "create", "Queue",
                f"other-config:min-rate={cfg['min']}",
                f"other-config:max-rate={LINK_CAPACITY}",
                f"other-config:priority={cfg['priority']}",
                f"external_ids:sdn-run={run_id}",
            ])

    return cmd


def apply(run_id):
    for port in QOS_PORTS:
        if ovs("get", "Port", port, "qos") != "[]":
            raise RuntimeError(f"Existing QoS on {port}")

    subprocess.run(build_apply(run_id), check=True)


def cleanup(run_id):
    ids = ["--bare", "--no-headings", "--columns=_uuid"]
    tag = f"external_ids:sdn-run={run_id}"

    qos_ids = ovs(*ids, "find", "QoS", tag).split()
    queue_ids = ovs(*ids, "find", "Queue", tag).split()

    cmd = ["ovs-vsctl", "--timeout=5"]

    for port in QOS_PORTS:
        current = ovs("--if-exists", "get", "Port", port, "qos")

        if current in qos_ids:
            cmd.extend([
                "--", "wait-until", "Port", port,
                f"qos={current}",
                "--", "clear", "Port", port, "qos"
            ])

    for uid in qos_ids:
        cmd.extend(["--", "destroy", "QoS", uid])

    for uid in queue_ids:
        cmd.extend(["--", "destroy", "Queue", uid])

    if qos_ids or queue_ids:
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    print("=== QoS Runtime: DRY RUN ===")
    print(shlex.join(build_apply("issue11-preview")))
    print("PASS: Configuration generated")
    print("No OVS changes made.")
