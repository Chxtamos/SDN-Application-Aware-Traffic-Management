"""Run repeatable application workloads inside the project host namespaces."""

import argparse
import json
import math
import signal
import subprocess
import sys
import time

from . import workloads


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number")
    return number


def seconds(value):
    number = positive(value)
    try:
        workloads.duration_ns(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return number


def positive_int(value):
    number = int(value)
    if not 0 < number <= workloads.MAX_SECONDS:
        raise argparse.ArgumentTypeError("expected a positive duration within the supported timer range")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def payload_size(value):
    size = int(value)
    if not workloads.UDP_HEADER.size <= size <= 1472:
        raise argparse.ArgumentTypeError("expected a UDP payload size from 24 to 1472 bytes")
    return size


def run_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="workload", required=True)

    http = commands.add_parser("http", help="send paced HTTP(S) GET requests to web1")
    http.add_argument("--source", choices=workloads.CLIENTS, default=workloads.STUDENT)
    http.add_argument("--destination", choices=(workloads.WEB,), default=workloads.WEB)
    http.add_argument("--port", type=int, choices=(80, 443), default=80)
    http.add_argument("--interval", type=seconds, default=0.25)
    http.add_argument("--duration", type=seconds, default=10)
    http.add_argument("--timeout", type=seconds, default=2, help="per-request time budget in seconds")

    web = commands.add_parser("http-server", help="serve a fixed HTTP response on web1:80")
    web.add_argument("--bind", choices=(workloads.WEB,), default=workloads.WEB)
    web.add_argument("--duration", type=seconds, default=20)

    send = commands.add_parser("udp-send", help="send sequenced telemetry to iot1")
    send.add_argument("--source", choices=workloads.PRIVILEGED, default=workloads.STAFF)
    send.add_argument("--destination", choices=(workloads.IOT,), default=workloads.IOT)
    send.add_argument("--port", type=int, choices=range(5000, 5101), default=5000,
                      metavar="5000-5100")
    send.add_argument("--stream-id", required=True, type=workloads.stream_id)
    send.add_argument("--duration", type=seconds, default=10)
    send.add_argument("--rate-pps", type=positive, default=800)
    send.add_argument("--payload-size", type=payload_size, default=1200)

    receive = commands.add_parser("udp-receive", help="capture telemetry on iot1")
    receive.add_argument("--source", choices=workloads.PRIVILEGED, default=workloads.STAFF)
    receive.add_argument("--bind", choices=(workloads.IOT,), default=workloads.IOT)
    receive.add_argument("--port", type=int, choices=range(5000, 5101), default=5000,
                         metavar="5000-5100")
    receive.add_argument("--stream-id", required=True, type=workloads.stream_id)
    receive.add_argument("--duration", type=seconds, default=20)

    report = commands.add_parser("udp-report", help="join sender and receiver JSON by sequence")
    report.add_argument("--sender", required=True)
    report.add_argument("--receiver", required=True)

    bulk = commands.add_parser("bulk", help="send a TCP iperf3 transfer to file1:5201")
    bulk.add_argument("--source", choices=workloads.PRIVILEGED, default=workloads.STAFF)
    bulk.add_argument("--destination", choices=(workloads.FILE,), default=workloads.FILE)
    bulk.add_argument("--port", type=int, choices=(5201,), default=5201)
    bulk.add_argument("--duration", type=positive_int, default=10)
    bulk.add_argument("--target-bps", type=nonnegative_int, default=0,
                      help="TCP target bitrate in bits/s, or 0 for uncapped")
    bulk.add_argument("--timeout-slack", type=seconds, default=10,
                      help="seconds allowed beyond the requested transfer duration")

    server = commands.add_parser("bulk-server", help="serve one iperf3 test on file1:5201")
    server.add_argument("--bind", choices=(workloads.FILE,), default=workloads.FILE)
    server.add_argument("--timeout", type=seconds, default=30, help="total server lifetime in seconds")
    return parser


COMMANDS = {
    "http": workloads.http_client,
    "http-server": workloads.http_server,
    "udp-send": workloads.udp_send,
    "udp-receive": workloads.udp_receive,
    "udp-report": workloads.udp_report,
    "bulk": workloads.bulk_client,
    "bulk-server": workloads.bulk_server,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config = vars(run_parser().parse_args(argv))
    workload = config.pop("workload")
    started = time.monotonic()
    result = {
        "schema_version": 3,
        "workload": workload,
        "command": [sys.executable, "-B", "-m", "traffic", *argv],
        "config": config,
        "started_at_unix_ns": time.time_ns(),
        "status": "ok",
        "observed": {},
        "reported": {},
        "calculated": {},
    }
    try:
        COMMANDS[workload](config, result)
    except (OSError, ValueError, OverflowError, subprocess.TimeoutExpired) as exc:
        result.update(status="error", error=str(exc))
    except KeyboardInterrupt:
        result.update(status="interrupted", error="interrupted")
    result["ended_at_unix_ns"] = time.time_ns()
    result["elapsed_s"] = time.monotonic() - started
    try:
        output = json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        result.update(status="error", error="result serialization failed: {}".format(exc),
                      observed={}, reported={}, calculated={})
        output = json.dumps(result, allow_nan=False)
    print(output, flush=True)
    if result["status"] == "interrupted":
        return 130
    return 0 if result["status"] == "ok" else 1


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    sys.exit(main())
