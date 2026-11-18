"""Workload implementations for the diamond topology and its existing ACL."""

from contextlib import contextmanager
import hashlib
import http.client
import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

STUDENT, STAFF, MANAGEMENT = "10.0.10.1", "10.0.20.1", "10.0.30.1"
WEB, FILE, IOT = "10.0.40.10", "10.0.40.20", "10.0.40.30"
CLIENTS = (STUDENT, STAFF, MANAGEMENT)
PRIVILEGED = (STAFF, MANAGEMENT)
NS_PER_SECOND = 1_000_000_000
MAX_SECONDS = threading.TIMEOUT_MAX
MAX_SEQUENCE = 2**64 - 1
UDP_RATE_TOLERANCE = 0.05
UDP_HEADER = struct.Struct("!16sQ")
HTTP_BODY = b"SDN interactive workload\n"


def stream_id(value):
    return str(uuid.UUID(value))


def ready(workload, address, port):
    print(json.dumps({"event": "ready", "workload": workload, "address": address,
                      "port": port, "at_unix_ns": time.time_ns()}), file=sys.stderr, flush=True)


def duration_ns(seconds):
    if (type(seconds) not in (int, float)
            or not 1 / NS_PER_SECOND <= seconds <= MAX_SECONDS):
        raise ValueError("duration or interval is outside the supported timer range")
    return round(seconds * NS_PER_SECOND)


def udp_schedule(config):
    rate = config["rate_pps"]
    if type(rate) not in (int, float) or not 1 / MAX_SECONDS <= rate <= NS_PER_SECOND:
        raise ValueError("UDP packet rate is outside the supported timer range")
    window = duration_ns(config["duration"])
    interval = round(NS_PER_SECOND / rate)
    return window, interval, (window + interval - 1) // interval


def paced(start, window, interval, *, gap_after_completion=False):
    """Yield nominal slot indices and dispatch times on a monotonic clock."""
    deadline = start + window
    next_at = start
    next_slot = 0
    while True:
        now = time.monotonic_ns()
        wake_at = min(next_at, deadline)
        if now < wake_at:
            time.sleep((wake_at - now) / NS_PER_SECOND)
            continue
        if now >= deadline:
            return
        slot = (now - start) // interval
        yield slot, now
        if gap_after_completion:
            finished = time.monotonic_ns()
            # Keep small wake/send costs from shifting every later slot. Retain
            # one full interval after a missed slot or a long sender pause.
            missed_slot = slot > next_slot or finished - now >= interval
            correction = 0 if missed_slot else min(interval * 3 // 10, 500_000)
            next_slot = slot + 1
            next_at = max(start + next_slot * interval, finished + interval - correction)
        else:
            next_at = now + interval


def remaining_http_time(deadline):
    remaining = deadline - time.monotonic_ns()
    if remaining <= 0:
        raise TimeoutError("HTTP deadline exceeded")
    return remaining / NS_PER_SECOND


def raise_http_timeout(_signum, _frame):
    raise TimeoutError("HTTP deadline exceeded")


@contextmanager
def http_alarm(deadline):
    """Bound blocking HTTP operations on the POSIX CLI main thread."""
    if (not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread()
            or any(signal.getitimer(signal.ITIMER_REAL))):
        yield
        return
    previous = signal.signal(signal.SIGALRM, raise_http_timeout)
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining_http_time(deadline))
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def http_request(config, workload_deadline=None):
    started = time.monotonic_ns()
    deadline = started + duration_ns(config["timeout"])
    if workload_deadline is not None:
        deadline = min(deadline, workload_deadline)
    record = {"started_at_unix_ns": time.time_ns(), "status_code": None, "body_bytes": 0,
              "error": None, "body_complete": False}
    connection = None
    try:
        with http_alarm(deadline):
            connection_type = (http.client.HTTPSConnection if config["port"] == 443
                               else http.client.HTTPConnection)
            connection = connection_type(config["destination"], config["port"],
                                         timeout=remaining_http_time(deadline),
                                         source_address=(config["source"], 0))
            connection.request("GET", "/", headers={"Connection": "close"})
            transport = connection.sock
            if transport is not None:
                transport.settimeout(remaining_http_time(deadline))
            with connection.getresponse() as response:
                record["status_code"] = response.status
                remaining = remaining_http_time(deadline)
                if transport is not None and transport.fileno() != -1:
                    transport.settimeout(remaining)
                expected = response.length
                # Read at most one MiB plus a sentinel to bound experiment memory.
                body_bytes = len(response.read(1024 * 1024 + 1))
                remaining_http_time(deadline)
                if body_bytes > 1024 * 1024:
                    raise ValueError("HTTP response exceeds one MiB")
                if expected is not None and body_bytes != expected:
                    raise ValueError("incomplete HTTP response body")
                record.update(body_bytes=body_bytes, body_complete=True)
                if not 200 <= response.status < 300:
                    record["error"] = "HTTP {}".format(response.status)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        record["error"] = "{}: {}".format(type(exc).__name__, exc)
    finally:
        if connection is not None:
            connection.close()
    record["ended_at_unix_ns"] = time.time_ns()
    record["response_time_ms"] = (time.monotonic_ns() - started) / 1_000_000
    return record


def http_client(config, result):
    requests = []
    result["observed"]["requests"] = requests
    window = duration_ns(config["duration"])
    interval = duration_ns(config["interval"])
    started = time.monotonic_ns()
    deadline = started + window
    for slot, dispatch in paced(started, window, interval):
        if time.monotonic_ns() >= deadline:
            break
        record = http_request(config, deadline)
        record["schedule_lateness_ms"] = (dispatch - (started + slot * interval)) / 1_000_000
        requests.append(record)
    elapsed = (time.monotonic_ns() - started) / NS_PER_SECOND
    failures = sum(record["error"] is not None for record in requests)
    result["observed"].update(request_count=len(requests), failure_count=failures)
    result["calculated"].update(target_requests_per_s=1 / config["interval"],
                                attempted_requests_per_s=len(requests) / elapsed if elapsed else None)
    if not requests or failures:
        result.update(status="partial" if failures < len(requests) else "error",
                      error="one or more HTTP requests failed" if failures else "no requests ran")


class FixedResponse(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(1.0)
        super().setup()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(HTTP_BODY)))
        self.end_headers()
        self.wfile.write(HTTP_BODY)
        self.server.responses += 1

    def log_message(self, _format, *_args):
        pass


def http_server(config, result):
    with HTTPServer((config["bind"], 80), FixedResponse) as server:
        server.responses = 0
        deadline = time.monotonic_ns() + duration_ns(config["duration"])
        ready("http-server", config["bind"], 80)
        try:
            with http_alarm(deadline):
                while time.monotonic_ns() < deadline:
                    server.timeout = remaining_http_time(deadline)
                    server.handle_request()
        except TimeoutError:
            if time.monotonic_ns() < deadline:
                raise
        result["observed"]["responses_written"] = server.responses


def udp_clock_id():
    """Identify a monotonic clock shared by Linux network namespaces."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
            boot = stream.read().strip()
        try:
            namespace = os.readlink("/proc/self/ns/time")
        except FileNotFoundError:
            namespace = "host"
    except OSError:
        return None
    return hashlib.sha256("{}:{}".format(boot, namespace).encode("ascii")).hexdigest()


def udp_load(config, sent, elapsed):
    target = config["rate_pps"]
    achieved = sent / elapsed
    within_tolerance = (1 - UDP_RATE_TOLERANCE) * target <= achieved <= (1 + UDP_RATE_TOLERANCE) * target
    return {
        "target_packets_per_s": target,
        "sent_packets_per_s": achieved,
        "offered_load_tolerance_percent": 100 * UDP_RATE_TOLERANCE,
        "offered_load_status": "within_tolerance" if within_tolerance else "degraded",
    }


def udp_send(config, result):
    token = uuid.UUID(config["stream_id"]).bytes
    padding = bytes(config["payload_size"] - UDP_HEADER.size)
    window, interval, scheduled = udp_schedule(config)
    if scheduled > MAX_SEQUENCE + 1:
        raise ValueError("UDP schedule exceeds the sequence number range")
    sent, errors = [], []
    observed = result["observed"]
    observed.update(packets=sent, send_errors=errors, clock_id=udp_clock_id())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((config["source"], 0))
        sock.setblocking(False)
        observed["source_port"] = sock.getsockname()[1]
        started = time.monotonic_ns()
        observed.update(window_start_unix_ns=time.time_ns(), window_start_monotonic_ns=started)
        for sequence, _ in paced(started, window, interval, gap_after_completion=True):
            datagram = UDP_HEADER.pack(token, sequence) + padding
            stamp = time.time_ns()
            monotonic_stamp = time.monotonic_ns()
            if monotonic_stamp >= started + window:
                break
            if monotonic_stamp >= started + (sequence + 1) * interval:
                continue
            packet = {"sequence": sequence, "at_unix_ns": stamp, "at_monotonic_ns": monotonic_stamp}
            try:
                size = sock.sendto(datagram, (config["destination"], config["port"]))
                if size != len(datagram):
                    raise OSError("incomplete UDP send")
            except OSError as exc:
                errors.append(dict(packet, error=str(exc)))
                continue
            sent.append(dict(packet, payload_bytes=size))
        observed["window_end_unix_ns"] = time.time_ns()
        finished = time.monotonic_ns()
        elapsed = finished - started
        observed.update(window_end_monotonic_ns=finished, window_elapsed_ns=elapsed,
                        window_elapsed_s=elapsed / NS_PER_SECOND)
    attempted = len(sent) + len(errors)
    observed.update(scheduled_packets=scheduled, attempted_packets=attempted,
                    skipped_intervals=scheduled - attempted, sent_packets=len(sent),
                    send_failure_count=len(errors),
                    sent_payload_bytes=sum(record["payload_bytes"] for record in sent))
    elapsed = observed["window_elapsed_s"]
    load = udp_load(config, len(sent), elapsed)
    result["calculated"].update(
        load, target_payload_bps=config["rate_pps"] * config["payload_size"] * 8,
        sent_payload_bps=observed["sent_payload_bytes"] * 8 / elapsed,
    )
    if not sent:
        result.update(status="error", error="no datagrams sent")
    elif errors or load["offered_load_status"] == "degraded":
        result.update(status="partial", error="local UDP send failures" if errors else
                      "UDP offered load outside the +/-5% rate tolerance")


def udp_receive(config, result):
    token = uuid.UUID(config["stream_id"]).bytes
    packets = []
    observed = result["observed"]
    observed.update(packets=packets, ignored_packets=0, clock_id=udp_clock_id())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((config["bind"], config["port"]))
        started = time.monotonic_ns()
        observed.update(window_start_unix_ns=time.time_ns(), window_start_monotonic_ns=started)
        deadline = started + duration_ns(config["duration"])
        ready("udp-receive", config["bind"], config["port"])
        while True:
            remaining = deadline - time.monotonic_ns()
            if remaining <= 0:
                break
            sock.settimeout(remaining / NS_PER_SECOND)
            try:
                data, (source, port) = sock.recvfrom(65535)
            except socket.timeout:
                break
            monotonic_stamp = time.monotonic_ns()
            stamp = time.time_ns()
            if source != config["source"] or len(data) < UDP_HEADER.size:
                observed["ignored_packets"] += 1
                continue
            packet_token, sequence = UDP_HEADER.unpack_from(data)
            if token != packet_token:
                observed["ignored_packets"] += 1
                continue
            packets.append({"sequence": sequence, "at_unix_ns": stamp,
                            "at_monotonic_ns": monotonic_stamp,
                            "payload_bytes": len(data), "source_port": port})
        observed["window_end_unix_ns"] = time.time_ns()
        observed["window_end_monotonic_ns"] = time.monotonic_ns()
    observed.update(received_packets=len(packets),
                    received_payload_bytes=sum(record["payload_bytes"] for record in packets))


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: {}".format(key))
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("nonfinite JSON number: {}".format(value))


def finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        reject_constant(value)
    return number


def decode_json(text):
    return json.loads(text, parse_float=finite_json_float, parse_constant=reject_constant,
                      object_pairs_hook=unique_json_object)


def read_udp_run(path, workload, allowed_statuses):
    try:
        with open(path, encoding="utf-8-sig") as stream:
            document = decode_json(stream.read())
        schema = document["schema_version"]
        if (type(schema) is not int or schema not in (1, 2, 3) or document["workload"] != workload
                or document["status"] not in allowed_statuses):
            raise ValueError("unexpected workload, schema version, or run status")
        observed, config = document["observed"], document["config"]
        if workload == "udp-send":
            failures = checked_integer(observed["send_failure_count"], "send_failure_count")
            degraded = False
            if schema >= 3:
                udp_schedule(config)
                sent_count = checked_integer(observed["sent_packets"], "sent_packets")
                elapsed = observed["window_elapsed_s"]
                duration_ns(elapsed)
                load = udp_load(config, sent_count, elapsed)
                if document["calculated"]["offered_load_status"] != load["offered_load_status"]:
                    raise ValueError("UDP offered-load status conflicts with the send count and window")
                degraded = load["offered_load_status"] == "degraded"
            if (failures > 0 or degraded) != (document["status"] == "partial"):
                raise ValueError("UDP sender status conflicts with its send failures or offered load")
        return config, observed, schema
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid {} result {}: {}".format(workload, path, exc)) from exc


def udp_report(config, result):
    sender, sent, sender_schema = read_udp_run(config["sender"], "udp-send", {"ok", "partial"})
    receiver, received, receiver_schema = read_udp_run(config["receiver"], "udp-receive", {"ok"})
    if (sender_schema == 3) != (receiver_schema == 3):
        raise ValueError("UDP captures mix clock-aware and legacy schemas; pair matching schemas")
    try:
        calculate_udp_report(sender, sent, receiver, received, result, sender_schema)
    except (KeyError, TypeError) as exc:
        raise ValueError("missing or invalid UDP observation: {}".format(exc)) from exc


def checked_integer(value, label, minimum=0, maximum=MAX_SEQUENCE):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("{} must be an integer in [{}, {}]".format(label, minimum, maximum))
    return value


def checked_count(observed, key, expected):
    count = checked_integer(observed[key], key, maximum=MAX_SEQUENCE + 1)
    if count != expected:
        raise ValueError("{} does not match UDP records or schedule".format(key))
    return count


def capture_window(observed, clock):
    start = checked_integer(observed["window_start_" + clock + "_ns"], "window start")
    end = checked_integer(observed["window_end_" + clock + "_ns"], "window end")
    if end <= start:
        raise ValueError("UDP observation window must have a positive duration")
    return start, end


def packet_sequence(packet, window, limit, stamp_key):
    stamp = checked_integer(packet[stamp_key], "packet timestamp")
    if not window[0] <= stamp <= window[1]:
        raise ValueError("packet timestamp lies outside its UDP observation window")
    return checked_integer(packet["sequence"], "packet sequence", maximum=limit - 1)


def sender_records(config, sent, window, schema, stamp_key):
    packets, errors = sent["packets"], sent["send_errors"]
    if not isinstance(packets, list) or not isinstance(errors, list) or not packets:
        raise ValueError("UDP sender must contain successful packet and error lists")
    checked_count(sent, "sent_packets", len(packets))
    checked_count(sent, "send_failure_count", len(errors))
    attempted = len(packets) + len(errors)
    scheduled = skipped = None
    limit = attempted
    if schema >= 2:
        _, _, planned = udp_schedule(config)
        scheduled = checked_count(sent, "scheduled_packets", planned)
        checked_count(sent, "attempted_packets", attempted)
        skipped = checked_count(sent, "skipped_intervals", scheduled - attempted)
        limit = scheduled

    payload_size = checked_integer(config["payload_size"], "configured payload size",
                                   UDP_HEADER.size, 1472)
    successful = {}
    for packet in packets:
        seq = packet_sequence(packet, window, limit, stamp_key)
        size = checked_integer(packet["payload_bytes"], "sent payload bytes", payload_size, payload_size)
        if seq in successful:
            raise ValueError("duplicate successful UDP sender sequence")
        successful[seq] = (size, packet[stamp_key])
    checked_count(sent, "sent_payload_bytes", sum(size for size, _ in successful.values()))
    failed = set()
    for error in errors:
        seq = packet_sequence(error, window, limit, stamp_key)
        if seq in successful or seq in failed:
            raise ValueError("duplicate or overlapping failed UDP sender sequence")
        if not isinstance(error["error"], str) or not error["error"]:
            raise ValueError("UDP send error lacks an error message")
        failed.add(seq)
    return successful, limit, scheduled, attempted, skipped


def receiver_matches(received, window, successful, source_port, sequence_limit, stamp_key):
    packets = received["packets"]
    if not isinstance(packets, list):
        raise ValueError("UDP receiver packets must be a list")
    checked_count(received, "received_packets", len(packets))
    checked_integer(received["ignored_packets"], "ignored_packets")
    matched = set()
    duplicates = unmatched = total_bytes = 0
    for packet in packets:
        port = checked_integer(packet["source_port"], "receiver source port", 1, 65535)
        limit = sequence_limit if port == source_port else MAX_SEQUENCE + 1
        seq = packet_sequence(packet, window, limit, stamp_key)
        size = checked_integer(packet["payload_bytes"], "received payload bytes", UDP_HEADER.size, 65507)
        total_bytes += size
        sender = successful.get(seq)
        if port != source_port or sender is None or sender[0] != size:
            unmatched += 1
        elif packet[stamp_key] < sender[1]:
            raise ValueError("UDP packet was received before its send timestamp")
        elif seq in matched:
            duplicates += 1
        else:
            matched.add(seq)
    checked_count(received, "received_payload_bytes", total_bytes)
    return matched, duplicates, unmatched


def calculate_udp_report(sender, sent, receiver, received, result, sender_schema=2):
    if (any(sender[key] != receiver[key] for key in ("stream_id", "source", "port"))
            or sender["destination"] != receiver["bind"]):
        raise ValueError("sender and receiver describe different UDP streams")
    if (sender["source"] not in PRIVILEGED or sender["destination"] != IOT
            or not isinstance(sender["stream_id"], str)
            or stream_id(sender["stream_id"]) != sender["stream_id"]):
        raise ValueError("invalid UDP stream configuration")
    checked_integer(sender["port"], "UDP service port", 5000, 5100)
    checked_integer(receiver["port"], "receiver UDP service port", 5000, 5100)

    clock, comparison = "unix", "legacy_utc_same_host"
    if sender_schema >= 3:
        sender_clock, receiver_clock = sent["clock_id"], received["clock_id"]
        if sender_clock is None and receiver_clock is None:
            comparison = "utc_same_host_fallback"
        elif isinstance(sender_clock, str) and sender_clock and sender_clock == receiver_clock:
            clock, comparison = "monotonic", "host_monotonic"
        else:
            raise ValueError("UDP captures do not share a comparable monotonic clock")
    stamp_key = "at_" + clock + "_ns"
    send_window, receive_window = capture_window(sent, clock), capture_window(received, clock)
    if not receive_window[0] <= send_window[0] < send_window[1] <= receive_window[1]:
        raise ValueError("receiver window must cover sender window; check timing and clock")
    elapsed = sent["window_elapsed_s"]
    duration_ns(elapsed)
    window_ns = duration_ns(sender["duration"])
    if sender_schema >= 2:
        elapsed_ns = checked_integer(sent["window_elapsed_ns"], "sender elapsed nanoseconds",
                                     minimum=window_ns)
        if elapsed != elapsed_ns / NS_PER_SECOND:
            raise ValueError("sender elapsed seconds do not match its monotonic nanoseconds")
        if clock == "monotonic" and send_window[1] - send_window[0] != elapsed_ns:
            raise ValueError("sender elapsed nanoseconds do not match the monotonic window")
    elif elapsed < sender["duration"]:
        raise ValueError("sender elapsed seconds are shorter than the configured window")
    source_port = checked_integer(sent["source_port"], "sender source port", 1, 65535)
    successful, limit, scheduled, attempted, skipped = sender_records(
        sender, sent, send_window, sender_schema, stamp_key)
    matched, duplicates, unmatched = receiver_matches(
        received, receive_window, successful, source_port, limit, stamp_key)

    lost = len(successful) - len(matched)
    result["observed"].update(scheduled_packets=scheduled, attempted_packets=attempted,
                              skipped_intervals=skipped, sent_packets=len(successful),
                              clock_comparison=comparison,
                              send_failure_count=sent["send_failure_count"],
                              received_packets=received["received_packets"],
                              ignored_packets=received["ignored_packets"],
                              sender_window_elapsed_s=elapsed)
    result["calculated"].update(
        received_unique_packets=len(matched),
        duplicate_packets=duplicates,
        unmatched_packets=unmatched,
        lost_packets=lost,
        loss_percent=100 * lost / len(successful),
        received_payload_bps=8 * sum(successful[seq][0] for seq in matched) / elapsed,
    )

    if sender_schema >= 3:
        load = udp_load(sender, len(successful), elapsed)
        result["calculated"].update(load)
        if sent["send_failure_count"] or load["offered_load_status"] == "degraded":
            result.update(status="partial", error="UDP sender had local failures or degraded offered load")


def run_iperf(command, timeout, result):
    result["tool_command"] = command
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        for name in ("stdout", "stderr"):
            output = getattr(exc, name)
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            result["observed"]["iperf3_" + name] = output
        raise
    observed = result["observed"]
    observed.update(iperf3_exit_code=process.returncode, iperf3_stderr=process.stderr)
    try:
        document = decode_json(process.stdout)
    except (ValueError, RecursionError) as exc:
        observed["iperf3_stdout"] = process.stdout
        raise ValueError("iperf3 returned no valid JSON (exit {}): {}".format(
            process.returncode, exc)) from exc
    observed["iperf3_json"] = document
    if not isinstance(document, dict):
        raise ValueError("iperf3 JSON must be an object")
    if "error" in document:
        raise ValueError(document["error"] or "iperf3 reported an error")
    if process.returncode:
        raise ValueError("iperf3 exited with code {}".format(process.returncode))
    return document


def bitrate(value, label):
    if type(value) in (int, float) and value >= 0:
        try:
            if math.isfinite(value):
                return value
        except OverflowError:
            pass
    raise ValueError("{} must be a finite nonnegative number".format(label))


def iperf_transfer(summary, role):
    units = checked_integer(summary["bytes"], "iperf3 " + role + " bytes")
    rate = bitrate(summary["bits_per_second"], "iperf3 " + role + " bitrate")
    seconds = bitrate(summary["seconds"], "iperf3 " + role + " seconds")
    if units == rate == 0:
        return units, rate
    # iperf3 can round seconds to microseconds and bitrate to whole bits/s.
    rounding_bits = rate * 0.5e-6 + seconds * 0.5
    if not (units and rate > 0 and seconds > 0
            and math.isclose(rate * seconds, 8 * units, rel_tol=1e-9, abs_tol=rounding_bits)):
        raise ValueError("iperf3 {} bytes, seconds and bitrate are inconsistent".format(role))
    return units, rate


def iperf_metrics(document):
    try:
        sender = document["end"]["sum_sent"]
        receiver = document["end"]["sum_received"]
        sent_bytes, sender_bps = iperf_transfer(sender, "sender")
        received_bytes, receiver_bps = iperf_transfer(receiver, "receiver")
        metrics = {
            "sender_bps": sender_bps,
            "receiver_bps": receiver_bps,
            "sent_bytes": sent_bytes,
            "received_bytes": received_bytes,
            "tcp_retransmissions": None,
        }
        if "retransmits" in sender:
            metrics["tcp_retransmissions"] = checked_integer(
                sender["retransmits"], "iperf3 TCP retransmissions")
    except (KeyError, TypeError) as exc:
        raise ValueError("iperf3 JSON lacks a complete TCP transfer summary") from exc
    return metrics


def bulk_client(config, result):
    command = ["iperf3", "-c", config["destination"], "-B", config["source"], "-p",
               str(config["port"]), "-t", str(config["duration"]), "-b",
               str(config["target_bps"]), "-J"]
    document = run_iperf(command, config["duration"] + config["timeout_slack"], result)
    result["reported"].update(iperf_metrics(document))


def bulk_server(config, result):
    command = ["iperf3", "-s", "-1", "-B", config["bind"], "-p", "5201", "-J"]
    document = run_iperf(command, config["timeout"], result)
    iperf_metrics(document)
