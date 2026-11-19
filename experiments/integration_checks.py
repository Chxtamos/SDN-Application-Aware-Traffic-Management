"""Validate Issue #14 evidence from controller, OVS and endpoint observations."""

from collections import namedtuple
import json
import re

from network.qos_plan import QUEUES
from traffic import workloads

Service = namedtuple("Service", "source destination protocol port queue")
APPLICATIONS = (
    Service(workloads.STUDENT, workloads.WEB, "tcp", 80, 1),
    Service(workloads.STAFF, workloads.IOT, "udp", 5000, 2),
    Service(workloads.MANAGEMENT, workloads.FILE, "tcp", 5201, 3),
)
ACL_SERVICES = (
    Service(workloads.STAFF, workloads.FILE, "tcp", 22, 3),
    Service(workloads.STAFF, workloads.IOT, "udp", 5001, 2),
)
EDGE_PORTS = {workloads.STUDENT: 1, workloads.STAFF: 2, workloads.MANAGEMENT: 3,
              workloads.WEB: 1, workloads.FILE: 2, workloads.IOT: 3}
PATHS = {"A": (4, "s2", "s3"), "B": (5, "s3", "s2")}
EVENT = re.compile(r"ADAPTIVE: switch ([AB]) -> ([AB]) \(util A=(\d+)% B=(\d+)%\)")
QUEUE = re.compile(r"(?m)^\s*port (\d+) queue (\d+): [^\r\n]*?\bpkts=(\d+)\b")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def path_events(text):
    return [{"from": start, "to": end, "util_a_pct_rounded": int(a), "util_b_pct_rounded": int(b)}
            for start, end, a, b in EVENT.findall(text)]


def path_history(events, adaptive, policy):
    expected = [("A", "B"), ("B", "A")] if adaptive else []
    observed = [(event["from"], event["to"]) for event in events]
    require(observed == expected, "unexpected path history: {}".format(observed))
    if adaptive:
        offload, recovery = events
        # Ryu logs whole percentages. Allow half a percentage point of rounding.
        a, b = offload["util_a_pct_rounded"], offload["util_b_pct_rounded"]
        require(a + .5 >= policy["congest_hi"] and b <= a, "offload event does not match policy")
        require(recovery["util_b_pct_rounded"] - .5 < policy["congest_lo"]
                and recovery["util_a_pct_rounded"] - .5 < policy["congest_hi"],
                "recovery event does not match policy")


def flow_rows(text):
    for line in text.splitlines():
        head, separator, actions = line.partition(" actions=")
        if not separator:
            continue
        attrs = dict(re.findall(r"\b(\w+)=([^ ,]+)", head))
        if "n_packets" in attrs and "priority" in attrs:
            fields = set(re.split(r"[, ]+", head))
            yield attrs, fields, actions.split(",")


def active_flows(rows):
    return [(attrs, fields, actions) for attrs, fields, actions in rows
            if attrs.get("cookie") == "0x200" and attrs["priority"] == "200"]


def has_rule(rows, service, reverse, port, queue=None, edge=False, traffic=True):
    source, destination = ((service.destination, service.source) if reverse
                           else (service.source, service.destination))
    transport = "tp_src" if reverse else "tp_dst"
    for attrs, fields, actions in rows:
        if service.protocol not in fields or attrs.get(transport) != str(service.port):
            continue
        if edge and (attrs.get("nw_src"), attrs.get("nw_dst"), attrs.get("in_port")) != (
                source, destination, str(EDGE_PORTS[source])):
            continue
        if "output:" + str(port) not in actions:
            continue
        if queue is not None and "set_queue:" + str(queue) not in actions:
            continue
        if not traffic or int(attrs["n_packets"]) > 0:
            return True
    return False


def queue_counts(text):
    counts = {}
    for port, queue, count in QUEUE.findall(text):
        key = (int(port), int(queue))
        require(key not in counts, "duplicate OVS queue counter {}".format(key))
        counts[key] = int(count)
    return counts


def verify_path(snapshots, baseline, path, services, udp_replies=False):
    port, transit, unused = PATHS[path]
    flows = {switch: active_flows(flow_rows(snapshots[switch, "dump-flows"]))
             for switch in ("s1", "s2", "s3", "s4")}
    require(not flows[unused], "inactive transit {} still has forwarding rules".format(unused))
    for switch in ("s1", "s4"):
        before = queue_counts(baseline[switch, "queue-stats"])
        after = queue_counts(snapshots[switch, "queue-stats"])
        required = {(p, q) for p in (4, 5) for q in QUEUES}
        require(required <= before.keys() and required <= after.keys(),
                "{} is missing configured queue counters".format(switch))
        for service in services:
            reverse = switch == "s4"
            traffic = not reverse or service.protocol == "tcp" or udp_replies
            require(has_rule(flows[switch], service, reverse, port, service.queue, True, traffic),
                    "Path {} {} missing active class {} edge rule or queue action".format(
                        path, switch, service.queue))
            require(has_rule(flows[transit], service, reverse, 1 if reverse else 2, traffic=traffic),
                    "Path {} {} missing class {} transit forwarding".format(path, transit, service.queue))
            key = (port, service.queue)
            if traffic:
                require(after[key] > before[key],
                        "Path {} {} queue {} did not advance".format(path, switch, service.queue))


def drop_counts(text):
    counts = {}
    for service in ACL_SERVICES:
        matching = [int(attrs["n_packets"]) for attrs, fields, actions in flow_rows(text)
                    if (attrs["priority"] == "300" and attrs.get("nw_src") == workloads.STUDENT
                        and attrs.get("in_port") == str(EDGE_PORTS[workloads.STUDENT])
                        and attrs.get("nw_dst") == service.destination and service.protocol in fields
                        and attrs.get("tp_dst") == str(service.port) and actions == ["drop"])]
        if matching:
            counts[service.protocol] = sum(matching)
    return counts


def verify_denies(previous, current):
    before, after = drop_counts(previous), drop_counts(current)
    for protocol in ("tcp", "udp"):
        require(after.get(protocol, 0) > before.get(protocol, 0),
                "student {} deny counter did not advance".format(protocol))


def acl_records(directory, role):
    data = (directory / ("acl-" + role + ".stdout.txt")).read_text(encoding="utf-8")
    # A probe can be terminated between writes. Only complete JSONL records
    # are counted. Any earlier malformed line remains a verification failure.
    return [json.loads(line) for line in data.rpartition("\n")[0].splitlines()]


def verify_acl(records, start, end):
    """Require both transports to be exercised inside this observation window."""
    for role, accepted in (("student", "timeout"), ("staff", "reply")):
        for protocol in ("tcp", "udp"):
            require(any(row["protocol"] == protocol and row["status"] == accepted
                        and start <= row["started_ns"] <= row["finished_ns"] <= end
                        for row in records[role]),
                    "{} {} probe did not demonstrate {} in this interval".format(role, protocol, accepted))


def verify_acl_run(records):
    for row in records["student"]:
        require(row["status"] == "timeout", "student {} probe unexpectedly returned {}".format(
            row["protocol"], row["status"]))
    for row in records["staff"]:
        status = row["status"]
        # Retain a completed TCP handshake with a missing echo as a
        # transient for Staff. Window validation still requires real echoes.
        transient = row["protocol"] == "tcp" and status == "connected_without_reply"
        require(status in {"reply", "timeout"} or transient,
                "staff {} probe error: {}".format(row["protocol"], status))


def workload_overlap(http, udp, bulk):
    """Intersect observed activity with positive iperf intervals, inset for its 1 s clock precision."""
    http_times = [row["ended_at_unix_ns"] for row in http["observed"]["requests"]
                  if row["error"] is None and row["body_complete"]]
    udp_times = [row["at_unix_ns"] for row in udp["observed"]["packets"]]
    require(len(http_times) > 1 and len(udp_times) > 1, "HTTP or UDP activity samples are missing")
    iperf = bulk["observed"]["iperf3_json"]
    anchor = iperf["start"]["timestamp"]["timesecs"]
    spans = []
    for interval in (row["sum"] for row in iperf["intervals"] if row["sum"]["bytes"] > 0):
        require(interval["end"] > interval["start"], "invalid iperf interval")
        if spans and spans[-1][1] == interval["start"]:
            spans[-1][1] = interval["end"]
        else:
            spans.append([interval["start"], interval["end"]])
    for start, end in spans:
        left = max(min(http_times), min(udp_times), int((anchor + start + 1) * 1e9))
        right = min(max(http_times), max(udp_times), int((anchor + end) * 1e9))
        if left < right and all(any(left <= sample <= right for sample in rows)
                                for rows in (http_times, udp_times)):
            return {"started_ns": left, "ended_ns": right, "duration_ns": right - left}
    raise RuntimeError("no common HTTP, UDP and positive TCP observation window")
