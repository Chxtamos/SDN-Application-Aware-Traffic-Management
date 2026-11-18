"""Issue #12 validation for the Issue #11 diamond in an empty exclusive VM."""

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from mininet.link import TCLink
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

lab = Path(sys.argv[1]).resolve()
evidence = lab / "evidence"
repo = lab / "repo"
sys.path.insert(0, str(repo))

from network.qos_lab_topology import QoSLabTopo
from network.qos_runtime import apply, cleanup, ovs

PORT = 17653
DURATION = 6
RUN_ID = "issue12-" + uuid.uuid4().hex[:12]
QIDS = {"http": 1, "udp": 2, "bulk": 3}
EXPECTED_FLOWS = {
    "http": ("10.0.10.1", "10.0.40.10", 80),
    "udp": ("10.0.20.1", "10.0.40.30", 5000),
    "bulk": ("10.0.30.1", "10.0.40.20", 5201),
}
QUEUE_RE = re.compile(r"port (\d+) queue (\d+): bytes=(\d+), pkts=(\d+), errors=(\d+)")
PORTS = (("s1", "4"), ("s1", "5"), ("s4", "4"), ("s4", "5"))
JOBS = []
os.umask(0o022)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def load(path):
    return json.loads(path.read_text())


def note(trial, host, command):
    with (trial / "commands.jsonl").open("a") as stream:
        stream.write(json.dumps({"host": host, "argv": command,
                                 "launch_at_unix_ns": time.time_ns()}) + "\n")


def process(trial, label, command, host=None):
    note(trial, host.name if host else "root", command)
    with (trial / (label + ".json")).open("w") as out, (trial / (label + ".stderr.txt")).open("w") as err:
        launch = host.popen if host else subprocess.Popen
        child = launch(command, cwd=str(repo), stdout=out, stderr=err)
    JOBS.append(child)
    return child


def traffic(trial, label, args, host=None):
    return process(trial, label, ["/usr/bin/python3", "-B", "-m", "traffic", *args], host)


def check(trial, label, child, allowed=(0,)):
    code = child.wait(timeout=22)
    require(code in allowed, "{} {} exited {}: {}".format(
        trial.name, label, code, (trial / (label + ".stderr.txt")).read_text()))
    document = load(trial / (label + ".json"))
    require(document["status"] == "ok", "{} {}: {}".format(trial.name, label, document.get("error")))
    return document


def wait_ready(trial, label, child):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and child.poll() is None:
        if '"event": "ready"' in (trial / (label + ".stderr.txt")).read_text():
            return
        time.sleep(0.04)
    raise RuntimeError("{} {} did not become ready".format(trial.name, label))


def execute(trial, command, host=None):
    if host:
        note(trial, host.name, command)
        child = host.popen(command, cwd=str(repo), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        JOBS.append(child)
        out, err = child.communicate(timeout=5)
        require(child.returncode == 0, "{} failed: {}".format(command, err))
        return out
    note(trial, "root", command)
    completed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=8)
    return completed.stdout


def await_bulk(trial, child, host):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and child.poll() is None:
        ports = execute(trial, ["ss", "-H", "-lnt", "sport", "=", ":5201"], host)
        if "10.0.40.20:5201" in ports:
            (trial / "bulk-listener.txt").write_text(ports)
            return
        time.sleep(0.05)
    raise RuntimeError("{} bulk did not become ready".format(trial.name))


def counters(trial, phase):
    snapshot = {}
    for switch, port in PORTS:
        raw = execute(trial, ["ovs-ofctl", "-O", "OpenFlow13", "queue-stats", switch, port])
        (trial / ("queues-{}-{}-{}.txt".format(phase, switch, port))).write_text(raw)
        for actual_port, qid, b, packets, errors in QUEUE_RE.findall(raw):
            require(actual_port == port, "unexpected port")
            snapshot["{}:{}:{}".format(switch, port, qid)] = {
                "bytes": int(b), "packets": int(packets), "errors": int(errors)}
    require(len(snapshot) == 16, "expected 16 queue counters")
    save(trial / ("queues-" + phase + ".json"), snapshot)
    return snapshot


def snapshot_flows(trial):
    raw = execute(trial, ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"])
    (trial / "s1-flows.txt").write_text(raw)
    return raw.splitlines()


def verify_queues(trial, before, after, classes):
    delta = {}
    for key, end in after.items():
        delta[key] = {metric: count - before[key][metric] for metric, count in end.items()}
        require(min(delta[key].values()) >= 0, "queue counter decreased: " + key)
    save(trial / "queues-delta.json", delta)
    flows = snapshot_flows(trial)
    for name in classes:
        queue = QIDS[name]
        key = "s1:4:{}".format(queue)
        require(delta[key]["packets"] > 0 and delta[key]["bytes"] > 0,
                "no observed packets for {} queue {}".format(name, key))
        src, dst, port = EXPECTED_FLOWS[name]
        match = "nw_src={},nw_dst={},tp_dst={} actions=".format(src, dst, port)
        require(any(match in line and "set_queue:{},output:4".format(queue) in line
                    and re.search(r"n_packets=[1-9]\d*", line) for line in flows),
                "missing {} class flow with packet hits".format(name))
    return delta


def tcp_probe(trial, host):
    start = time.time_ns()
    raw = execute(trial, ["ss", "-H", "-tin", "dst", "10.0.40.20:5201"], host)
    return {"started_at_unix_ns": start, "ended_at_unix_ns": time.time_ns(), "ss_stdout": raw}


def tcp_bytes(raw):
    counts = {}
    port = None
    for line in raw.splitlines():
        endpoint = re.search(r"10\.0\.30\.1:(\d+)\s+10\.0\.40\.20:5201", line)
        if endpoint:
            port = endpoint.group(1)
        acked = re.search(r"\bbytes_acked:(\d+)", line)
        if port and acked:
            counts[port] = int(acked.group(1))
    return counts


def overlap(first, last, http, udp_send, udp_recv):
    bounds = (first["ended_at_unix_ns"], last["started_at_unix_ns"])
    lo, hi = bounds
    initial, final = (tcp_bytes(probe["ss_stdout"]) for probe in (first, last))
    advances = {key: final[key] - initial[key] for key in final.keys() & initial.keys()}
    request_count = sum(lo <= row["started_at_unix_ns"] <= row["ended_at_unix_ns"] <= hi
                        and row["error"] is None for row in http["observed"]["requests"])
    udp_packets = [
        sum(lo <= row["at_unix_ns"] <= hi for row in run["observed"]["packets"])
        for run in (udp_send, udp_recv)
    ]
    require(max(advances.values(), default=0) >= 1_000_000 and request_count > 0
            and min(udp_packets) > 0, "TCP byte progress and HTTP/UDP events did not overlap")
    return {"interval_start_unix_ns": lo, "interval_end_unix_ns": hi,
            "tcp_bytes_acked_delta_by_source_port": advances, "http_completed": request_count,
            "udp_sends": udp_packets[0], "udp_receives": udp_packets[1]}


def verify_metrics(documents):
    summary = {}
    if "http" in documents:
        obs = documents["http"]["observed"]
        requests = obs["requests"]
        require(obs["request_count"] == len(requests) and len(requests) > 0
                and obs["failure_count"] == 0
                and all(row["status_code"] == 200 and row["response_time_ms"] > 0
                        and row["ended_at_unix_ns"] >= row["started_at_unix_ns"] for row in requests),
                "invalid HTTP results")
        times = sorted(row["response_time_ms"] for row in requests)
        summary["http"] = {"status_200": len(times), "min_response_ms": times[0],
                           "mean_response_ms": sum(times) / len(times), "max_response_ms": times[-1]}
    if "udp-report" in documents:
        tx = documents["udp-send"]["observed"]
        rx = documents["udp-receiver"]["observed"]
        m = documents["udp-report"]["calculated"]
        sent = {row["sequence"]: row["payload_bytes"] for row in tx["packets"]}
        received = {row["sequence"] for row in rx["packets"] if
                    row["source_port"] == tx["source_port"]
                    and sent.get(row["sequence"]) == row["payload_bytes"]}
        lost = len(sent) - len(received)
        elapsed = tx["window_elapsed_s"]
        expected_rate = 8 * sum(sent[seq] for seq in received) / elapsed
        actual_tx = documents["udp-send"]["calculated"]["sent_payload_bps"]
        require(len(sent) == tx["sent_packets"] and len(rx["packets"]) == rx["received_packets"]
                and len(received) == m["received_unique_packets"] > 0 and lost == m["lost_packets"]
                and math.isclose(m["loss_percent"], 100 * lost / len(sent))
                and math.isclose(m["received_payload_bps"], expected_rate)
                and math.isclose(actual_tx, 8 * sum(sent.values()) / elapsed), "invalid UDP metrics")
        summary["udp"] = {**m, "sent_packets": len(sent), "sent_payload_bps": actual_tx,
                          "sent_packets_per_s": len(sent) / elapsed}
    if "bulk" in documents:
        bulk = documents["bulk"]
        m = bulk["reported"]
        raw = bulk["observed"]["iperf3_json"]["end"]
        require(m["sent_bytes"] == raw["sum_sent"]["bytes"] > 0
                and m["received_bytes"] == raw["sum_received"]["bytes"] > 0
                and m["receiver_bps"] == raw["sum_received"]["bits_per_second"] > 0
                and m["sender_bps"] == raw["sum_sent"]["bits_per_second"] > 0
                and m["tcp_retransmissions"] == raw["sum_sent"]["retransmits"] >= 0,
                "invalid bulk metrics")
        summary["bulk"] = m
    return summary

def run_trial(net, name, classes):
    print("Running", name, flush=True)
    trial = evidence / name
    trial.mkdir()
    before = counters(trial, "before")
    servers = {}
    if "http" in classes:
        servers["http-server"] = traffic(trial, "http-server", ["http-server", "--duration", "11"],
                                         net["web1"])
        wait_ready(trial, "http-server", servers["http-server"])
    if "udp" in classes:
        stream = str(uuid.uuid4())
        servers["udp-receiver"] = traffic(trial, "udp-receiver",
            ["udp-receive", "--stream-id", stream, "--source", "10.0.20.1",
             "--port", "5000", "--duration", "11"], net["iot1"])
        wait_ready(trial, "udp-receiver", servers["udp-receiver"])
    if "bulk" in classes:
        servers["bulk-server"] = traffic(trial, "bulk-server", ["bulk-server", "--timeout", "17"],
                                         net["file1"])
        await_bulk(trial, servers["bulk-server"], net["file1"])

    clients = {}
    if "http" in classes:
        clients["http"] = traffic(trial, "http", ["http", "--source", "10.0.10.1", "--interval",
                                  "0.25", "--duration", str(DURATION), "--timeout", "2"],
                                  net["student1"])
    if "udp" in classes:
        clients["udp-send"] = traffic(trial, "udp-send", ["udp-send", "--stream-id", stream,
            "--source", "10.0.20.1", "--port", "5000", "--payload-size", "1200",
            "--rate-pps", "800", "--duration", str(DURATION)], net["staff1"])
    if "bulk" in classes:
        clients["bulk"] = traffic(trial, "bulk", ["bulk", "--source", "10.0.30.1",
                                  "--duration", str(DURATION), "--target-bps", "0"], net["mgmt1"])
    if len(classes) == 3:
        time.sleep(1.1)
        initial = tcp_probe(trial, net["mgmt1"])
        time.sleep(2.4)
        final = tcp_probe(trial, net["mgmt1"])
        save(trial / "tcp-progress.json", [initial, final])

    documents = {name: check(trial, name, child) for name, child in {**clients, **servers}.items()}
    if "udp" in classes:
        files = ["--sender", str(trial / "udp-send.json"),
                 "--receiver", str(trial / "udp-receiver.json")]
        documents["udp-report"] = check(trial, "udp-report",
            traffic(trial, "udp-report", ["udp-report", *files]))
    summary = verify_metrics(documents)
    after = counters(trial, "after")
    delta = verify_queues(trial, before, after, classes)
    summary["s1_path_a_queue_deltas"] = {name: delta["s1:4:{}".format(QIDS[name])] for name in classes}
    if len(classes) == 3:
        summary["overlap"] = overlap(initial, final, documents["http"], documents["udp-send"],
                                     documents["udp-receiver"])
    save(trial / "summary.json", summary)
    print("PASS", name, flush=True)
    return summary


UDP_LISTENER = """import json, socket, sys, time
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.bind(("10.0.40.30", 5000))
    print('{"event": "ready"}', file=sys.stderr, flush=True)
    deadline = time.monotonic() + 2.8
    packets = []
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            data, (source, port) = sock.recvfrom(256)
        except socket.timeout:
            break
        packets.append({"source": source, "payload": data.decode("ascii")})
    print(json.dumps(packets))
"""
UDP_PROBE = """import json, socket, sys, time
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.bind((sys.argv[1], 0))
    for index in range(2):
        sock.sendto(sys.argv[2].encode("ascii"), ("10.0.40.30", 5000))
        time.sleep(0.15)
    print(json.dumps({"attempts": 2, "source": sys.argv[1]}))
"""
TCP_PROBE = """import json, socket
with socket.socket() as sock:
    sock.bind(("10.0.10.1", 0))
    sock.settimeout(2.2)
    try:
        sock.connect(("10.0.40.20", 5201))
    except socket.timeout:
        print(json.dumps({"outcome": "timeout"}))
    except OSError as exc:
        print(json.dumps({"outcome": "socket_error", "error": str(exc)}))
    else:
        print(json.dumps({"outcome": "connected"}))
"""


def verify_acl(net):
    print("Running ACL checks", flush=True)
    trial = evidence / "acl"
    trial.mkdir()
    receiver = process(trial, "udp-capture", ["/usr/bin/python3", "-B", "-c", UDP_LISTENER],
                       net["iot1"])
    wait_ready(trial, "udp-capture", receiver)
    for role, address in (("student", "10.0.10.1"), ("staff", "10.0.20.1")):
        label = "udp-probe-" + role
        sender = process(trial, label, ["/usr/bin/python3", "-B", "-c", UDP_PROBE, address, role],
                         net[role + "1"])
        require(sender.wait(timeout=5) == 0, label + " failed")
    require(receiver.wait(timeout=5) == 0, "UDP ACL receiver exited with error")
    packets = load(trial / "udp-capture.json")
    require(packets and all(row == {"source": "10.0.20.1", "payload": "staff"} for row in packets),
            "UDP ACL failed: {}".format(packets))

    server = traffic(trial, "bulk-server", ["bulk-server", "--timeout", "12"], net["file1"])
    await_bulk(trial, server, net["file1"])
    probe = process(trial, "tcp-probe-student", ["/usr/bin/python3", "-B", "-c", TCP_PROBE],
                    net["student1"])
    require(probe.wait(timeout=5) == 0, "TCP ACL probe exited with error")
    require(load(trial / "tcp-probe-student.json")["outcome"] == "timeout",
            "Student-to-file TCP did not time out")
    client = traffic(trial, "bulk-staff", ["bulk", "--source", "10.0.20.1", "--duration", "1"],
                     net["staff1"])
    check(trial, "bulk-staff", client)
    check(trial, "bulk-server", server)

    rows = snapshot_flows(trial)
    for destination, port in (("10.0.40.20", 5201), ("10.0.40.30", 5000)):
        match = "nw_src=10.0.10.1,nw_dst={},tp_dst={} actions=drop".format(destination, port)
        require(any("priority=300" in row and match in row
                    and re.search(r"n_packets=[1-9]\d*", row) for row in rows),
                "missing ACL drop flow with packet hits: " + match)
    result = {
        "student_to_web_tcp_80": "allowed, verified in HTTP workload",
        "staff_to_iot_udp_5000": "allowed, datagrams arrived",
        "student_to_iot_udp_5000": "denied, no datagrams arrived and s1 drop counter increased",
        "staff_to_file_tcp_5201": "allowed, iperf3 completed",
        "mgmt_to_file_tcp_5201": "allowed, verified in bulk workload",
        "student_to_file_tcp_5201": "denied, connect timed out and s1 drop counter increased",
        "capture": packets,
    }
    save(trial / "summary.json", result)
    print("PASS ACL checks", flush=True)
    return result


def stop_processes():
    live = [child for child in JOBS if child.poll() is None]
    for child in live:
        child.terminate()
    for child in live:
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
    require(all(child.poll() is not None for child in JOBS), "experiment process left running")


def main():
    require(not ovs("list-br"), "unexpected bridge before startup")
    require(not ovs("--bare", "--no-headings", "--columns=_uuid", "list", "QoS"),
            "unexpected QoS before startup")
    require(not ovs("--bare", "--no-headings", "--columns=_uuid", "list", "Queue"),
            "unexpected queue before startup")
    save(evidence / "configuration.json", {
        "run_id": RUN_ID, "controller_host": "127.0.0.1", "controller_port": PORT,
        "qos_enabled": True, "adaptive_enabled": False,
        "topology": "network.qos_lab_topology.QoSLabTopo", "fabric_bps": 100000000,
        "client_duration_s": DURATION, "bulk_target_bps": 0,
        "http_interval_s": 0.25, "udp_payload_bytes": 1200, "udp_target_pps": 800,
        "http_timeout_s": 2, "udp_destination_port": 5000,
        "repeat_difference": "Fresh receiver process and stream UUID; same workload parameters.",
    })
    net = Mininet(topo=QoSLabTopo(), controller=lambda name: RemoteController(
        name, ip="127.0.0.1", port=PORT), switch=OVSSwitch, link=TCLink,
        autoSetMacs=True, build=False)
    attempted_qos = False
    try:
        net.build()
        net.start()
        require(net.waitConnected(timeout=15), "controller connection failed")
        attempted_qos = True
        apply(RUN_ID)
        for table in ("QoS", "Queue"):
            raw = ovs("--format=json", "find", table, "external_ids:sdn-run=" + RUN_ID)
            document = json.loads(raw)
            save(evidence / ("installed-" + table.lower() + ".json"), document)
            require(len(document["data"]) == (4 if table == "QoS" else 16), "unexpected install count")
        runs = {}
        for name, classes in (
            ("http-only", ("http",)), ("udp-only", ("udp",)), ("bulk-only", ("bulk",)),
            ("concurrent-1", ("http", "udp", "bulk")), ("concurrent-2", ("http", "udp", "bulk")),
        ):
            runs[name] = run_trial(net, name, classes)
        runs["acl"] = verify_acl(net)
        save(evidence / "summary.json", runs)
    finally:
        try:
            stop_processes()
        finally:
            try:
                if attempted_qos:
                    cleanup(RUN_ID)
            finally:
                net.stop()


if __name__ == "__main__":
    def interrupted(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    main()
