
#!/usr/bin/env python3

import shlex

LINK_CAPACITY = 100_000_000

QOS_PORTS = (
    "s1-eth4",
    "s1-eth5",
    "s4-eth4",
    "s4-eth5",
)

QUEUES = {
    0: {"name": "default",     "min": 1_000_000,  "priority": 3},
    1: {"name": "interactive", "min": 30_000_000, "priority": 0},
    2: {"name": "periodic",    "min": 5_000_000,  "priority": 1},
    3: {"name": "bulk",        "min": 30_000_000, "priority": 2},
}


def build_command(port):
    command = [
        "sudo", "ovs-vsctl", "--timeout=5",
        "--", "set", "Port", port, "qos=@qos",
        "--", "--id=@qos", "create", "QoS",
        "type=linux-htb",
        f"other-config:max-rate={LINK_CAPACITY}",
    ]

    for queue_id in QUEUES:
        command.append(f"queues:{queue_id}=@q{queue_id}")

    for queue_id, config in QUEUES.items():
        command.extend([
            "--", f"--id=@q{queue_id}", "create", "Queue",
            f"other-config:min-rate={config['min']}",
            f"other-config:max-rate={LINK_CAPACITY}",
            f"other-config:priority={config['priority']}",
        ])

    return command


def main():
    assert sum(q["min"] for q in QUEUES.values()) <= LINK_CAPACITY

    print("=== SDN QoS Configuration ===")
    print("Mode: DRY RUN")
    print("Link capacity: 100 Mbps")

    for port in QOS_PORTS:
        print(f"\n=== Port: {port} ===")
        print(shlex.join(build_command(port)))

    print("\nDry run completed. No OVS changes made.")


if __name__ == "__main__":
    main()
