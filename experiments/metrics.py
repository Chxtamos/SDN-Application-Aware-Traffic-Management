"""Extract labeled observations and derived metrics from experiment artifacts."""

import math
from pathlib import Path
import re
import statistics

from traffic.workloads import calculate_udp_report, decode_json, iperf_metrics

WORKLOAD_FILES = {"http": ("http",), "udp": ("udp-send", "udp-receiver", "udp-report"),
                  "bulk": ("bulk",)}
PROBE_LABELS = {kind + "-ping" for kind in WORKLOAD_FILES}
ARTIFACT_STATUS = {"ok": "ok", "partial": "degraded", "error": "failed",
                   "interrupted": "incomplete"}
STATUS_ORDER = {"ok": 0, "degraded": 1, "incomplete": 2, "failed": 3}
ARTIFACT_ERRORS = (OSError, KeyError, ValueError, TypeError, RecursionError, AttributeError, OverflowError)


def combined_status(statuses):
    return max(statuses, key=STATUS_ORDER.get, default="ok")


def distribution(values):
    if not values:
        return None
    values = sorted(values)
    return {"count": len(values), "min": values[0], "mean": statistics.mean(values),
            "p95": values[math.ceil(len(values) * .95) - 1], "max": values[-1]}


def number(value):
    if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
        raise ValueError("expected a finite nonnegative observation")
    return value


def udp_timing(sender, receiver):
    out = {"udp_app_delay_ms": None, "udp_app_delay_variation_ms": None}
    if not sender.get("clock_id") or sender["clock_id"] != receiver.get("clock_id"):
        return out
    sent = {row["sequence"]: row for row in sender["packets"]}
    arrivals = {}
    for record in receiver["packets"]:
        sequence = record["sequence"]
        match = sent.get(sequence)
        if (match is None or record["source_port"] != sender["source_port"]
                or match["payload_bytes"] != record["payload_bytes"]):
            continue
        delay = record["at_monotonic_ns"] - match["at_monotonic_ns"]
        if delay < 0:
            raise ValueError("UDP arrival timestamp is before its send timestamp")
        arrivals[sequence] = min(delay, arrivals.get(sequence, delay))
    delays = [arrivals[seq] / 1_000_000 for seq in sorted(arrivals)]
    out["udp_app_delay_ms"] = distribution(delays)
    out["udp_app_delay_variation_ms"] = distribution(
        [abs(second - first) for first, second in zip(delays, delays[1:])])
    return out


def ping_stats(text):
    counts = re.findall(r"(?m)^(\d+) packets transmitted, (\d+) (?:packets )?received,"
                       r"[^\n]*? (\d+(?:\.\d+)?)% packet loss(?:,|\s*$)", text)
    if len(counts) != 1:
        raise ValueError("missing or ambiguous ICMP packet summary")
    tx, rx, printed = counts[0]
    sent, received = int(tx), int(rx)
    if not 0 <= received <= sent or sent == 0:
        raise ValueError("invalid ICMP transmit and reply counts")
    loss = 100 * (sent - received) / sent
    rounding = 10 ** -len(printed.partition(".")[2])
    if not 0 <= float(printed) <= 100 or abs(float(printed) - loss) > rounding + 1e-9:
        raise ValueError("ICMP loss percentage disagrees with packet counts")
    rtt = re.findall(r"(?m)^(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
                     r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms\s*$", text)
    has_rtt_line = any(line.startswith(("rtt ", "round-trip ")) for line in text.splitlines())
    if (received and len(rtt) != 1) or (not received and has_rtt_line):
        raise ValueError("ICMP RTT availability disagrees with reply count")
    samples = None
    if received:
        minimum, mean, maximum, spread = (number(float(token)) for token in rtt[0])
        if not minimum <= mean <= maximum:
            raise ValueError("invalid ICMP RTT range")
        samples = {"min": minimum, "mean": mean, "max": maximum, "mdev": spread}
    return {
        "observed": {"icmp_probes_sent": sent, "icmp_echo_replies": received},
        "reported": {"icmp_rtt_ms": samples},
        "calculated": {"icmp_packet_loss_pct": loss},
    }


def artifact(directory, name):
    path = directory / (name + ".json")
    try:
        doc = decode_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    expected = "udp-receive" if name == "udp-receiver" else name
    if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int
            or doc["schema_version"] != 3 or doc.get("workload") != expected
            or doc.get("status") not in ARTIFACT_STATUS):
        raise ValueError("invalid Issue #12 schema 3 envelope: " + path.name)
    return doc


def load_workload_artifacts(directory, names):
    docs, issues = {}, {}
    for name in names:
        try:
            document = artifact(directory, name)
            if document is None:
                raise ValueError("artifact missing")
            docs[name] = document
        except ARTIFACT_ERRORS as exc:
            issues[name] = str(exc)
    return docs, issues


def http_request_times(doc):
    observed = doc["observed"]
    requests = observed["requests"]
    counts = (observed["request_count"], observed["failure_count"])
    if not isinstance(requests, list) or any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("invalid HTTP request records or counts")
    successful = []
    for row in requests:
        code, error, complete = (row[key] for key in ("status_code", "error", "body_complete"))
        latency = number(row["response_time_ms"])
        if code is not None and (type(code) is not int or not 100 <= code <= 599):
            raise ValueError("invalid HTTP response status code")
        if type(complete) is not bool or (complete and code is None):
            raise ValueError("inconsistent HTTP body completion")
        if error is None:
            if not complete or not 200 <= code < 300:
                raise ValueError("HTTP request without error lacks a complete 2xx response")
            successful.append(latency)
        elif not isinstance(error, str) or not error:
            raise ValueError("invalid HTTP request error")
    failures = len(requests) - len(successful)
    if counts != (len(requests), failures):
        raise ValueError("HTTP counts disagree with request records")
    expected = "error" if not successful else ("partial" if failures else "ok")
    if doc["status"] != expected:
        raise ValueError("HTTP envelope status disagrees with request records")
    return successful


def extract_http(docs, result):
    observed = docs["http"]["observed"]
    successful = http_request_times(docs["http"])
    result["observed"].update(http_requests=observed["request_count"],
                              http_failures=observed["failure_count"])
    result["calculated"]["http_response_time_ms"] = distribution(successful)


def udp_captures_complete(docs):
    sent = docs.get("udp-send", {}).get("observed")
    received = docs.get("udp-receiver", {}).get("observed")
    if not (isinstance(sent, dict) and "sent_packets" in sent
            and isinstance(received, dict) and "received_packets" in received):
        return False
    # Issue #12 cannot calculate a report if the sender never sent a datagram.
    return not (type(sent["sent_packets"]) is int and sent["sent_packets"] == 0
                and sent.get("packets") == [])


def verify_udp(docs):
    sender, receiver = (docs[name] for name in ("udp-send", "udp-receiver"))
    sent, received = sender["observed"], receiver["observed"]
    verified = {"observed": {}, "calculated": {}}
    try:
        calculate_udp_report(sender["config"], sent, receiver["config"], received, verified, sender_schema=3)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise ValueError("udp-send.json / udp-receiver.json: {}".format(exc)) from exc
    expected = verified.get("status", "ok")
    statuses = {"udp-send": expected, "udp-receiver": "ok", "udp-report": expected}
    contradictions = [name + ".json status disagrees with endpoint records"
                      for name, document in docs.items() if document["status"] != statuses[name]]
    report = docs.get("udp-report")
    if report is not None and (verified["calculated"] != report.get("calculated")
                               or verified["observed"] != report.get("observed")):
        contradictions.append("udp-report.json does not agree with the raw endpoint captures")
    if contradictions:
        raise ValueError(", ".join(contradictions))
    return verified


def extract_udp(docs, result, verified):
    result["observed"].update(verified["observed"])
    calculated = verified["calculated"]
    result["calculated"].update(
        udp_send_pps=calculated["sent_packets_per_s"], udp_goodput_bps=calculated["received_payload_bps"],
        udp_target_pps=calculated["target_packets_per_s"], udp_offered_load_status=calculated["offered_load_status"],
        packet_loss_pct=calculated["loss_percent"], udp_packets_lost=calculated["lost_packets"],
        udp_duplicate_packets=calculated["duplicate_packets"],
    )
    result["calculated"].update(udp_timing(docs["udp-send"]["observed"], docs["udp-receiver"]["observed"]))


def extract_bulk(docs, result):
    bulk = docs["bulk"]
    tool = iperf_metrics(bulk["observed"]["iperf3_json"])
    if bulk["reported"] != tool:
        raise ValueError("bulk.json does not agree with the raw iperf3 output")
    result["reported"].update(tcp_sender_bps=tool["sender_bps"],
                              tcp_receiver_bps=tool["receiver_bps"],
                              tcp_sent_bytes=tool["sent_bytes"],
                              tcp_received_bytes=tool["received_bytes"],
                              tcp_retransmissions=tool["tcp_retransmissions"])


EXTRACTORS = {"http": extract_http, "bulk": extract_bulk}


def logged_outcomes(directory):
    path = directory / "commands.jsonl"
    outcomes = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = decode_json(line)
            if not isinstance(entry, dict):
                raise ValueError("expected a command record")
            if entry.get("event") == "finish":
                outcomes[entry["label"]] = entry
            elif entry.get("event") == "start" and entry.get("label") in PROBE_LABELS:
                outcomes.setdefault(entry["label"], entry)
    return outcomes


def exit_status(document, outcome):
    status = ARTIFACT_STATUS[document["status"]]
    if outcome is None:
        return status
    if outcome["status"] in {"timeout", "stopped", "cleanup_error"}:
        # A complete-looking envelope cannot make an aborted command complete.
        return "incomplete"
    expected = {"ok": 0, "partial": 1, "error": 1, "interrupted": 130}
    code = outcome.get("returncode")
    if type(code) is not int:
        raise ValueError("missing or invalid process returncode")
    if code != expected[document["status"]]:
        raise ValueError("process exit code disagrees with JSON status")
    return status


def summarize(directory, workloads, process_outcomes=None):
    directory = Path(directory)
    issues = []
    try:
        logged = logged_outcomes(directory)
    except (OSError, KeyError, TypeError, ValueError, RecursionError) as exc:
        logged = {}
        issues.append("commands.jsonl: {}".format(exc))
    if process_outcomes is None:
        process_outcomes = logged
    else:
        process_outcomes = {**{label: entry for label, entry in logged.items() if label in PROBE_LABELS},
                            **process_outcomes}
    results = {}
    for kind in workloads:
        names = WORKLOAD_FILES[kind]
        result = {"status": "ok", "issues": [], "observed": {}, "reported": {},
                  "calculated": {"packet_loss_pct": None, "one_way_latency_ms": None,
                                 "http_response_time_ms": None}}
        results[kind] = result
        docs, load_issues = load_workload_artifacts(directory, names)
        statuses = []
        for name in names:
            if name in load_issues:
                statuses.append("incomplete")
                result["issues"].append("{}.json: {}".format(name, load_issues[name]))
                continue
            doc = docs[name]
            try:
                if name == "http" and doc["status"] != "interrupted":
                    observation = doc["observed"]
                    # A setup error may occur before the first request record.
                    if doc["status"] != "error" or any(
                            key in observation for key in ("requests", "request_count", "failure_count")):
                        http_request_times(doc)
                status = exit_status(doc, process_outcomes.get(name))
                statuses.append(status)
                if status != "ok":
                    reason = "aborted process" if status == "incomplete" else doc.get("error", status)
                    result["issues"].append(name + ".json: " + str(reason))
            except ARTIFACT_ERRORS as exc:
                statuses.append("incomplete")
                result["issues"].append("{}.json: {}".format(name, exc))

        metrics_available = len(docs) == len(names) and not {"failed", "incomplete"}.intersection(statuses)
        if kind == "udp" and (metrics_available or udp_captures_complete(docs)):
            try:
                verified = verify_udp(docs)
                if metrics_available:
                    extract_udp(docs, result, verified)
            except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
                # Complete captures can disprove a claimed failure.
                statuses = ["incomplete"]
                result["issues"].append("metrics unavailable: {}".format(exc))
        elif metrics_available:
            try:
                EXTRACTORS[kind](docs, result)
            except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
                statuses.append("incomplete")
                result["issues"].append("metrics unavailable: {}".format(exc))
        label = kind + "-ping"
        path = directory / (label + ".stdout.txt")
        if path.is_file() or label in process_outcomes:
            source = "commands.jsonl ({})".format(label)
            try:
                code = None
                if label in process_outcomes:
                    outcome = process_outcomes[label]
                    if not isinstance(outcome, dict) or outcome.get("event") == "start":
                        raise ValueError("missing or invalid ICMP finish record")
                    code = outcome.get("returncode")
                    if type(code) is not int:
                        raise ValueError("ICMP finish record requires an integer returncode")
                source = path.name
                probe = ping_stats(path.read_text(encoding="utf-8", errors="replace"))
                if code is not None:
                    expected = 0 if probe["observed"]["icmp_echo_replies"] else 1
                    if code != expected or outcome.get("status") != ("ok" if expected == 0 else "nonzero"):
                        raise ValueError("ICMP summary disagrees with commands.jsonl process outcome")
                result["icmp_probe"] = probe
            except (OSError, ValueError, OverflowError) as exc:
                statuses.append("incomplete")
                result["issues"].append("{}: {}".format(source, exc))
        result["status"] = combined_status(statuses)
    statuses = [r["status"] for r in results.values()]
    if issues:
        statuses.append("incomplete")
    return {"schema_version": 1, "status": combined_status(statuses), "workloads": results,
            "issues": issues}


def controller_stats(directory):
    """Inventory raw Ryu counters while leaving the flow match text intact."""
    columns = {
        "port": "ts,dpid,port,rx_bytes,tx_bytes,rx_packets,util_pct,path",
        "flow": "ts,dpid,table,priority,cookie,packets,bytes,match",
    }
    result = {}
    for kind, filename in (("port", "sdn_stats.csv"), ("flow", "sdn_flows.csv")):
        counters = {"rows": None, "datapath_ids": None}
        result[kind] = {"artifact": filename, "observed": counters, "issues": []}
        try:
            with (Path(directory) / filename).open(encoding="utf-8") as stream:
                if stream.readline().rstrip() != columns[kind]:
                    raise ValueError("missing or unexpected Ryu header")
                datapaths, count, rejected = set(), 0, 0
                for line in stream:
                    fields = line.rstrip().split(",", 7)
                    try:
                        if len(fields) != 8 or not fields[-1]:
                            raise ValueError()
                        int(fields[0])
                        datapath = int(fields[1])
                        if kind == "port":
                            for counter in fields[2:6]:
                                if int(counter) < 0:
                                    raise ValueError()
                            if not 0 <= float(fields[6]) <= 100 or fields[7] not in ("A", "B"):
                                raise ValueError()
                        else:
                            int(fields[2])
                            int(fields[3])
                            int(fields[4], 16)
                            for counter in fields[5:7]:
                                if int(counter) < 0:
                                    raise ValueError()
                        datapaths.add(datapath)
                        count += 1
                    except ValueError:
                        rejected += 1
                counters.update(rows=count, datapath_ids=sorted(datapaths))
                if rejected:
                    result[kind]["issues"].append("{} malformed rows".format(rejected))
        except (OSError, ValueError) as exc:
            result[kind]["issues"].append(str(exc))
    result["port"]["calculated_in_controller"] = ["util_pct"]
    return result
