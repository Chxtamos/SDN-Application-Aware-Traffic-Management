import contextlib
import io
import json
import socket
import subprocess
import sys
import unittest
import uuid
from unittest.mock import call, patch

from traffic import __main__ as cli
from traffic import workloads as traffic


SECOND = 1_000_000_000
TOKEN = "18d20a90-81fa-41ed-bec8-915b7454d225"


def result():
    return {"status": "ok", "observed": {}, "reported": {}, "calculated": {}}


class FakeClock:
    def __init__(self, oversleep_ns=0):
        self.now = 0
        self.oversleep_ns = oversleep_ns

    def monotonic_ns(self):
        return self.now

    def sleep(self, seconds):
        self.now += round(seconds * SECOND) + self.oversleep_ns
        self.oversleep_ns = 0


def udp_observations():
    sender = {"stream_id": TOKEN, "source": traffic.STAFF, "port": 5000,
              "destination": traffic.IOT, "duration": 2, "rate_pps": 2, "payload_size": 24}
    receiver = {"stream_id": TOKEN, "source": traffic.STAFF, "port": 5000, "bind": traffic.IOT}
    sent = {
        "window_start_unix_ns": 2 * SECOND, "window_end_unix_ns": 4 * SECOND,
        "window_elapsed_s": 2, "window_elapsed_ns": 2 * SECOND, "source_port": 40000,
        "packets": [{"sequence": seq, "payload_bytes": 24, "at_unix_ns": 2 * SECOND + seq}
                    for seq in (0, 2)],
        "send_errors": [{"sequence": 1, "at_unix_ns": 2 * SECOND + 1, "error": "queue full"}],
        "scheduled_packets": 4, "attempted_packets": 3, "skipped_intervals": 1,
        "sent_packets": 2, "sent_payload_bytes": 48, "send_failure_count": 1,
    }
    received = {
        "window_start_unix_ns": SECOND, "window_end_unix_ns": 5 * SECOND,
        "received_packets": 4, "received_payload_bytes": 97, "ignored_packets": 2,
        "packets": [
            {"sequence": seq, "payload_bytes": size, "source_port": port, "at_unix_ns": 3 * SECOND}
            for seq, size, port in ((0, 24, 40000), (0, 24, 40000), (2, 24, 55555), (2, 25, 40000))
        ],
    }
    return sender, sent, receiver, received


def clock_aware_udp_observations():
    sender, sent, receiver, received = udp_observations()
    for observed in (sent, received):
        observed.update(clock_id="vm-test-clock",
                        window_start_monotonic_ns=observed["window_start_unix_ns"],
                        window_end_monotonic_ns=observed["window_end_unix_ns"])
        for row in observed["packets"] + observed.get("send_errors", []):
            row["at_monotonic_ns"] = row["at_unix_ns"]
    return sender, sent, receiver, received


class TestTraffic(unittest.TestCase):
    def test_cli_enforces_project_endpoints_and_validates_inputs(self):
        for argv in (
            ["bulk", "--source", traffic.STUDENT],
            ["bulk", "--destination", traffic.IOT],
            ["bulk", "--port", "5202"],
            ["udp-send", "--source", traffic.STUDENT, "--stream-id", TOKEN],
            ["udp-send", "--port", "4999", "--stream-id", TOKEN],
            ["udp-send", "--payload-size", "23", "--stream-id", TOKEN],
            ["udp-send", "--stream-id", "invalid"],
            ["http", "--destination", traffic.FILE],
            ["http", "--interval", "inf"],
            ["http", "--duration", "0"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.run_parser().parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_pacing_never_catches_up_after_an_oversleep(self):
        clock = FakeClock(oversleep_ns=490_000_000)
        with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic.time, "sleep", side_effect=clock.sleep):
            slots = list(traffic.paced(0, SECOND, SECOND // 4, gap_after_completion=True))
        self.assertEqual(slots, [(0, 0), (2, 740_000_000), (3, 990_000_000)])
        self.assertEqual(clock.now, SECOND)

    def test_pacing_waits_after_slow_udp_send_and_skips_final_slot(self):
        clock = FakeClock()
        with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic.time, "sleep", side_effect=clock.sleep):
            slots = traffic.paced(0, SECOND, SECOND // 4, gap_after_completion=True)
            self.assertEqual(next(slots), (0, 0))
            clock.now = 600_000_000
            self.assertEqual(list(slots), [(3, 850_000_000)])
        self.assertEqual(clock.now, SECOND)

    def test_http_get_binds_source_and_records_response_outcomes(self):
        config = vars(cli.run_parser().parse_args(["http"]))
        with patch.object(traffic.time, "monotonic_ns", return_value=0), \
                patch.object(traffic, "http_alarm", return_value=contextlib.nullcontext()), \
                patch.object(traffic.http.client, "HTTPConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.length = 4
            response.read.return_value = b"test"
            row = traffic.http_request(config)
            self.assertEqual(row["status_code"], 200)
            self.assertEqual(row["body_bytes"], 4)
            self.assertTrue(row["body_complete"])
            self.assertIsNone(row["error"])
            self.assertGreaterEqual(row["response_time_ms"], 0)
            response.status = 503
            self.assertEqual(traffic.http_request(config)["error"], "HTTP 503")
            response.length = 8
            self.assertIn("incomplete", traffic.http_request(config)["error"])
            connection.request.side_effect = socket.timeout("timed out")
            timeout = traffic.http_request(config)
            self.assertIsNone(timeout["status_code"])
            self.assertIn("timed out", timeout["error"])
        factory.assert_called_with(traffic.WEB, 80, timeout=2,
                                   source_address=(traffic.STUDENT, 0))
        connection.request.assert_called_with("GET", "/", headers={"Connection": "close"})
        self.assertEqual(connection.close.call_count, 4)

    def test_http_cli_exports_mixed_results_and_nonzero_status(self):
        output = io.StringIO()
        with patch.object(traffic, "paced", return_value=iter([(0, 0), (1, SECOND // 4)])), \
                patch.object(traffic, "http_request", side_effect=[
                    {"status_code": 200, "error": None}, {"status_code": 503, "error": "HTTP 503"}
                ]), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["http", "--duration", "1"]), 1)
        doc = json.loads(output.getvalue())
        self.assertEqual(doc["workload"], "http")
        self.assertEqual(doc["status"], "partial")
        self.assertEqual(doc["observed"]["request_count"], 2)
        self.assertEqual(doc["observed"]["failure_count"], 1)
        self.assertEqual(doc["calculated"]["target_requests_per_s"], 4)
        self.assertEqual(doc["command"][-3:], ["http", "--duration", "1"])

    def test_http_deadline_limits_the_full_request_and_disarms_timer(self):
        config = vars(cli.run_parser().parse_args(["http", "--timeout", "2"]))
        clock = FakeClock()
        with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic.signal, "ITIMER_REAL", 0, create=True), \
                patch.object(traffic.signal, "SIGALRM", 14, create=True), \
                patch.object(traffic.signal, "getitimer", return_value=(0, 0), create=True), \
                patch.object(traffic.signal, "setitimer", create=True) as timer, \
                patch.object(traffic.signal, "signal", return_value="previous") as register, \
                patch.object(traffic.http.client, "HTTPConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.length = 4

            def slow_body(_length):
                clock.now = SECOND // 4
                register.call_args.args[1](14, None)

            response.read.side_effect = slow_body
            row = traffic.http_request(config, workload_deadline=SECOND // 4)
        factory.assert_called_once_with(traffic.WEB, 80, timeout=0.25,
                                        source_address=(traffic.STUDENT, 0))
        self.assertEqual(timer.call_args_list, [call(0, 0.25), call(0, 0)])
        self.assertEqual(register.call_args, call(14, "previous"))
        connection.close.assert_called_once()
        self.assertEqual(row["status_code"], 200)
        self.assertEqual(row["response_time_ms"], 250)
        self.assertIn("deadline", row["error"])

    def test_http_does_not_dispatch_a_request_past_the_workload_deadline(self):
        config = vars(cli.run_parser().parse_args(["http", "--duration", "1"]))
        clock = FakeClock()
        doc = result()

        def delayed_dispatch(*_args):
            clock.now = SECOND
            yield 0, 0

        with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic, "paced", side_effect=delayed_dispatch), \
                patch.object(traffic, "http_request") as request:
            traffic.http_client(config, doc)
        request.assert_not_called()
        self.assertEqual(doc["observed"]["request_count"], 0)
        self.assertEqual(doc["status"], "error")

    def test_http_fallback_rejects_body_that_returns_after_budget(self):
        config = vars(cli.run_parser().parse_args(["http", "--timeout", "1"]))
        clock = FakeClock()
        with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic, "http_alarm", return_value=contextlib.nullcontext()), \
                patch.object(traffic.http.client, "HTTPConnection") as factory:
            response = factory.return_value.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.length = 4

            def late_body(_length):
                clock.now = 2 * SECOND
                return b"test"

            response.read.side_effect = late_body
            row = traffic.http_request(config)
        self.assertIn("deadline", row["error"])
        factory.return_value.close.assert_called_once()

    def test_http_fallback_does_not_read_body_after_headers_exhaust_budget(self):
        config = vars(cli.run_parser().parse_args(["http", "--timeout", "1"]))
        clock = FakeClock()
        with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic, "http_alarm", return_value=contextlib.nullcontext()), \
                patch.object(traffic.http.client, "HTTPConnection") as factory:
            connection = factory.return_value
            response_context = connection.getresponse.return_value
            response = response_context.__enter__.return_value
            response.status = 200

            def slow_headers():
                clock.now = SECOND
                return response_context

            connection.getresponse.side_effect = slow_headers
            row = traffic.http_request(config)
        response.read.assert_not_called()
        connection.close.assert_called_once()
        self.assertIn("deadline", row["error"])

    def test_udp_sender_distinguishes_send_errors_and_packs_sequences(self):
        config = vars(cli.run_parser().parse_args([
            "udp-send", "--stream-id", TOKEN, "--duration", "1", "--rate-pps", "4"]))
        clock = FakeClock(oversleep_ns=490_000_000)
        doc = result()
        with patch.object(traffic.socket, "socket") as factory, \
                patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic.time, "sleep", side_effect=clock.sleep):
            sock = factory.return_value.__enter__.return_value
            sock.getsockname.return_value = (traffic.STAFF, 40000)
            sock.sendto.side_effect = [1200, BlockingIOError("queue full"), 1200]
            traffic.udp_send(config, doc)
        sock.bind.assert_called_once_with((traffic.STAFF, 0))
        self.assertEqual(
            sock.sendto.call_args_list,
            [call(traffic.UDP_HEADER.pack(uuid.UUID(TOKEN).bytes, seq) + bytes(1176),
                  (traffic.IOT, 5000)) for seq in (0, 2, 3)],
        )
        self.assertEqual([packet["sequence"] for packet in doc["observed"]["packets"]], [0, 3])
        self.assertEqual(doc["observed"]["send_errors"][0]["sequence"], 2)
        self.assertEqual(doc["observed"]["scheduled_packets"], 4)
        self.assertEqual(doc["observed"]["attempted_packets"], 3)
        self.assertEqual(doc["observed"]["skipped_intervals"], 1)
        self.assertEqual(doc["observed"]["sent_packets"], 2)
        self.assertEqual(doc["observed"]["send_failure_count"], 1)
        self.assertEqual(doc["calculated"]["target_payload_bps"], 38400)
        self.assertEqual(doc["status"], "partial")
        factory.return_value.__exit__.assert_called_once()

    def test_udp_receiver_filters_unrelated_datagrams_and_keeps_duplicates(self):
        config = vars(cli.run_parser().parse_args(["udp-receive", "--stream-id", TOKEN]))
        doc = result()
        accepted = traffic.UDP_HEADER.pack(uuid.UUID(TOKEN).bytes, 3)
        with patch.object(traffic.socket, "socket") as factory, \
                contextlib.redirect_stderr(io.StringIO()) as events:
            sock = factory.return_value.__enter__.return_value
            sock.recvfrom.side_effect = [
                (b"short", (traffic.STAFF, 40000)),
                (accepted, (traffic.STUDENT, 40000)),
                (bytes(24), (traffic.STAFF, 40000)),
                (accepted, (traffic.STAFF, 40000)),
                (accepted, (traffic.STAFF, 40000)),
                socket.timeout(),
            ]
            traffic.udp_receive(config, doc)
        sock.bind.assert_called_once_with((traffic.IOT, 5000))
        factory.return_value.__exit__.assert_called_once()
        self.assertEqual(json.loads(events.getvalue())["event"], "ready")
        self.assertEqual(doc["observed"]["ignored_packets"], 3)
        self.assertEqual(doc["observed"]["received_packets"], 2)
        self.assertEqual(doc["observed"]["received_payload_bytes"], 48)
        self.assertEqual(doc["observed"]["packets"][0]["sequence"], 3)

    def test_udp_report_joins_unique_sequences_and_excludes_local_send_errors(self):
        sender, sent, receiver, received = udp_observations()
        doc = result()
        traffic.calculate_udp_report(sender, sent, receiver, received, doc)
        self.assertEqual(doc["observed"]["scheduled_packets"], 4)
        self.assertEqual(doc["observed"]["attempted_packets"], 3)
        self.assertEqual(doc["observed"]["skipped_intervals"], 1)
        self.assertEqual(doc["observed"]["send_failure_count"], 1)
        self.assertEqual(doc["calculated"], {
            "received_unique_packets": 1, "duplicate_packets": 1, "unmatched_packets": 2,
            "lost_packets": 1, "loss_percent": 50, "received_payload_bps": 96,
        })
        received.update(packets=[], received_packets=0, received_payload_bytes=0)
        traffic.calculate_udp_report(sender, sent, receiver, received, doc)
        self.assertEqual(doc["calculated"]["loss_percent"], 100)
        received["window_end_unix_ns"] = 3 * SECOND
        with self.assertRaisesRegex(ValueError, "receiver window"):
            traffic.calculate_udp_report(sender, sent, receiver, received, doc)
        with self.assertRaisesRegex(ValueError, "different UDP streams"):
            traffic.calculate_udp_report(sender, sent, dict(receiver, port=5001), received, doc)

    def test_udp_report_rejects_counter_and_record_corruption(self):
        for side, path, value in (
            ("sent", ("sent_packets",), 3),
            ("sent", ("sent_payload_bytes",), 999),
            ("sent", ("send_failure_count",), 0),
            ("sent", ("scheduled_packets",), 5),
            ("sent", ("attempted_packets",), 4),
            ("sent", ("skipped_intervals",), False),
            ("sent", ("window_elapsed_s",), 1e-100),
            ("sent", ("window_elapsed_s",), 30),
            ("sent", ("window_elapsed_ns",), SECOND),
            ("sent", ("packets", 1, "sequence"), 0),
            ("sent", ("packets", 1, "sequence"), -1),
            ("sent", ("packets", 1, "sequence"), 4),
            ("sent", ("send_errors", 0, "sequence"), 2),
            ("sent", ("send_errors", 0, "error"), ""),
            ("sent", ("packets", 0, "payload_bytes"), 25),
            ("received", ("received_packets",), 5),
            ("received", ("received_payload_bytes",), 1),
            ("received", ("ignored_packets",), -1),
            ("received", ("packets", 0, "source_port"), 0),
            ("received", ("packets", 0, "sequence"), 4),
            ("received", ("packets", 2, "sequence"), 2**64),
            ("received", ("packets", 0, "sequence"), True),
            ("received", ("packets", 0, "at_unix_ns"), 0),
        ):
            with self.subTest(side=side, path=path, value=value):
                sender, sent, receiver, received = udp_observations()
                target = sent if side == "sent" else received
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(ValueError):
                    traffic.calculate_udp_report(sender, sent, receiver, received, result())

    def test_udp_report_accepts_legacy_artifacts_and_reports_unknown_schedule(self):
        sender, sent, receiver, received = udp_observations()
        for key in ("scheduled_packets", "attempted_packets", "skipped_intervals", "window_elapsed_ns"):
            del sent[key]
        documents = [
            {"schema_version": 1, "workload": "udp-send", "status": "partial",
             "config": sender, "observed": sent},
            {"schema_version": 1, "workload": "udp-receive", "status": "ok",
             "config": receiver, "observed": received},
        ]
        files = [io.StringIO(json.dumps(doc)) for doc in documents]
        out = io.StringIO()
        with patch("builtins.open", side_effect=files), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["udp-report", "--sender", "tx", "--receiver", "rx"]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["schema_version"], 3)
        self.assertIsNone(report["observed"]["scheduled_packets"])
        self.assertIsNone(report["observed"]["skipped_intervals"])
        self.assertEqual(report["observed"]["attempted_packets"], 3)
        self.assertEqual(report["calculated"]["loss_percent"], 50)

    def test_udp_report_cli_emits_json_error_for_inconsistent_capture(self):
        sender, sent, receiver, received = udp_observations()
        received["received_packets"] += 1
        inputs = [
            io.StringIO(json.dumps({"schema_version": 2, "workload": workload, "status": status,
                                    "config": config, "observed": observed}))
            for workload, status, config, observed in (
                ("udp-send", "partial", sender, sent), ("udp-receive", "ok", receiver, received))
        ]
        output = io.StringIO()
        with patch("builtins.open", side_effect=inputs), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["udp-report", "--sender", "tx", "--receiver", "rx"]), 1)
        doc = json.loads(output.getvalue())
        self.assertEqual(doc["status"], "error")
        self.assertIn("received_packets", doc["error"])
        self.assertFalse(doc["calculated"])

    def test_udp_report_rejects_invalid_stream_type_and_sender_status(self):
        for invalid in ("stream", "status"):
            with self.subTest(invalid=invalid):
                sender, sent, receiver, received = udp_observations()
                status = "partial"
                if invalid == "stream":
                    sender["stream_id"] = receiver["stream_id"] = 7
                else:
                    status = "ok"
                inputs = [
                    io.StringIO(json.dumps({"schema_version": 2, "workload": workload, "status": state,
                                            "config": config, "observed": observed}))
                    for workload, state, config, observed in (
                        ("udp-send", status, sender, sent), ("udp-receive", "ok", receiver, received))
                ]
                output = io.StringIO()
                with patch("builtins.open", side_effect=inputs), contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["udp-report", "--sender", "tx", "--receiver", "rx"]), 1)
                doc = json.loads(output.getvalue())
                self.assertEqual(doc["status"], "error")
                self.assertIn(invalid, doc["error"])

    def test_udp_report_rejects_duplicate_json_fields(self):
        raw = '{"schema_version":2,"schema_version":1,"workload":"udp-send","status":"ok"}'
        with patch("builtins.open", return_value=io.StringIO(raw)):
            with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
                traffic.read_udp_run("tx", "udp-send", {"ok"})

    def test_udp_report_rejects_failed_receiver_file(self):
        document = {"schema_version": 1, "workload": "udp-receive", "status": "error"}
        with patch("builtins.open", return_value=io.StringIO(json.dumps(document))):
            with self.assertRaisesRegex(ValueError, "invalid udp-receive result"):
                traffic.read_udp_run("capture.json", "udp-receive", {"ok"})

    def test_bulk_exports_iperf_summary_and_replayable_command(self):
        payload = {"end": {"sum_sent": {"bits_per_second": 105, "bytes": 71, "seconds": 568 / 105, "retransmits": 0},
                           "sum_received": {"bits_per_second": 99, "bytes": 66, "seconds": 528 / 99}}}
        output = io.StringIO()
        with patch.object(traffic.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(payload), "")) as run, contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["bulk", "--duration", "5", "--source", traffic.MANAGEMENT,
                                       "--target-bps", "30000000"]), 0)
        command = ["iperf3", "-c", traffic.FILE, "-B", traffic.MANAGEMENT, "-p",
                   "5201", "-t", "5", "-b", "30000000", "-J"]
        run.assert_called_once_with(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=15, check=False)
        doc = json.loads(output.getvalue())
        self.assertEqual(doc["tool_command"], command)
        self.assertEqual(doc["observed"]["iperf3_json"], payload)
        self.assertEqual(doc["reported"], {"sender_bps": 105, "receiver_bps": 99,
                                          "sent_bytes": 71, "received_bytes": 66,
                                          "tcp_retransmissions": 0})

    def test_bulk_errors_are_structured(self):
        for failure, expected in (
            (FileNotFoundError("missing"), "missing"),
            (subprocess.TimeoutExpired(["iperf3"], 20, output=b"partial"), "timed out"),
            (subprocess.CompletedProcess([], 1, '{"error":"server busy"}', ""), "server busy"),
            (subprocess.CompletedProcess([], 0, '{"error":null}', ""), "reported an error"),
            (subprocess.CompletedProcess([], 0, '{"end":{}}', ""), "TCP transfer summary"),
            (subprocess.CompletedProcess([], 1, "invalid", "fatal"), "no valid JSON"),
        ):
            with self.subTest(failure=failure), patch.object(traffic.subprocess, "run") as run:
                if isinstance(failure, Exception):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["bulk"]), 1)
                doc = json.loads(output.getvalue())
                self.assertEqual(doc["status"], "error")
                self.assertIn(expected, doc["error"])

    def test_bulk_rejects_bad_metrics_and_nonfinite_json_with_structured_errors(self):
        for section, field, bad_value in (
            ("sum_sent", "bits_per_second", "null"),
            ("sum_sent", "bits_per_second", '"100"'),
            ("sum_sent", "bits_per_second", "NaN"),
            ("sum_sent", "bits_per_second", "Infinity"),
            ("sum_sent", "bits_per_second", "-Infinity"),
            ("sum_sent", "bits_per_second", "1e309"),
            ("sum_sent", "bits_per_second", "true"),
            ("sum_sent", "bits_per_second", "-1"),
            ("sum_sent", "bits_per_second", "1" + "0" * 400),
            ("sum_received", "bits_per_second", "null"),
            ("sum_received", "bits_per_second", '"100"'),
            ("sum_received", "bits_per_second", "1e309"),
            ("sum_sent", "bytes", '"100"'),
            ("sum_received", "bytes", "null"),
            ("sum_received", "bytes", "1.25"),
            ("sum_received", "bytes", "-1"),
            ("sum_sent", "retransmits", "null"),
            ("sum_sent", "retransmits", "false"),
        ):
            with self.subTest(section=section, field=field, value=bad_value[:30]):
                payload = {"end": {"sum_sent": {"bits_per_second": 100, "bytes": 70, "seconds": 5.6, "retransmits": 0},
                                   "sum_received": {"bits_per_second": 90, "bytes": 65, "seconds": 520 / 90}}}
                payload["end"][section][field] = "BAD_METRIC"
                raw = json.dumps(payload).replace('"BAD_METRIC"', bad_value)
                output = io.StringIO()
                with patch.object(traffic.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, raw, "")), contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["bulk"]), 1)
                doc = json.loads(output.getvalue())
                self.assertEqual(doc["status"], "error")
                self.assertTrue(doc["error"])
                self.assertFalse(doc["reported"])

    def test_bulk_server_rejects_nested_numeric_overflow_and_missing_summary(self):
        for raw in ('{"intervals":[{"sum":{"bits_per_second":1e309}}]}', '{"end":{}}',
                    '{"end":{},"end":{}}'):
            with self.subTest(raw=raw):
                output = io.StringIO()
                with patch.object(traffic.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, raw, "")), contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["bulk-server"]), 1)
                self.assertEqual(json.loads(output.getvalue())["status"], "error")

    def test_bulk_timeout_reaps_a_local_child(self):
        children = []
        popen = subprocess.Popen

        def track_child(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child

        with patch.object(traffic.subprocess, "Popen", side_effect=track_child):
            with self.assertRaises(subprocess.TimeoutExpired):
                traffic.run_iperf([sys.executable, "-B", "-c", "import time; time.sleep(5)"],
                                  0.3, result())
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_bulk_server_uses_one_off_and_bounded_lifetime(self):
        payload = {"end": {"sum_sent": {"bits_per_second": 0, "bytes": 0, "seconds": 0},
                           "sum_received": {"bits_per_second": 99.5, "bytes": 66, "seconds": 528 / 99.5}}}
        with patch.object(traffic, "run_iperf", return_value=payload) as run:
            doc = result()
            traffic.bulk_server({"bind": traffic.FILE, "timeout": 30}, doc)
        run.assert_called_once_with(["iperf3", "-s", "-1", "-B", traffic.FILE, "-p", "5201", "-J"],
                                    30, doc)


    def test_absolute_udp_pacing_absorbs_recurring_wake_and_send_cost(self):
        for send_cost in (0, 100_000):
            with self.subTest(send_cost=send_cost):
                clock = FakeClock()
                arrivals = []

                def delayed_sleep(seconds):
                    clock.sleep(seconds)
                    clock.now += 230_000

                with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                        patch.object(traffic.time, "sleep", side_effect=delayed_sleep):
                    for slot, current in traffic.paced(0, 6 * SECOND, 1_250_000,
                                                       gap_after_completion=True):
                        arrivals.append((slot, current))
                        clock.now += send_cost
                self.assertGreaterEqual(len(arrivals), 4560)
                self.assertLessEqual(len(arrivals), 4800)
                self.assertEqual(len({slot for slot, _ in arrivals}), len(arrivals))
                self.assertGreaterEqual(min(b[1] - a[1] for a, b in zip(arrivals, arrivals[1:])), 875_000)

    def test_absolute_udp_pacing_skips_expired_slots_after_pause(self):
        clock = FakeClock()
        with patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic.time, "sleep", side_effect=clock.sleep):
            slots = traffic.paced(0, SECOND, 25_000_000, gap_after_completion=True)
            self.assertEqual(next(slots), (0, 0))
            clock.now = 350_000_000
            resumed = list(slots)
        self.assertGreaterEqual(resumed[0][0], 14)
        self.assertGreaterEqual(resumed[1][1] - resumed[0][1], 25_000_000)
        self.assertTrue(all(next_slot > slot for (slot, _), (next_slot, _) in zip(resumed, resumed[1:])))

    def test_udp_degraded_offered_load_has_partial_status_without_socket_failure(self):
        config = vars(cli.run_parser().parse_args([
            "udp-send", "--stream-id", TOKEN, "--duration", "1", "--rate-pps", "4"]))
        clock = FakeClock(oversleep_ns=490_000_000)
        doc = result()
        with patch.object(traffic.socket, "socket") as factory, \
                patch.object(traffic.time, "monotonic_ns", side_effect=clock.monotonic_ns), \
                patch.object(traffic.time, "sleep", side_effect=clock.sleep):
            sock = factory.return_value.__enter__.return_value
            sock.getsockname.return_value = (traffic.STAFF, 40000)
            sock.sendto.return_value = 1200
            traffic.udp_send(config, doc)
        self.assertEqual(doc["observed"]["skipped_intervals"], 1)
        self.assertEqual(doc["observed"]["send_failure_count"], 0)
        self.assertEqual(doc["status"], "partial")
        self.assertEqual(doc["calculated"]["offered_load_status"], "degraded")

    def test_udp_rejects_impossible_legacy_receive_time_including_duplicates(self):
        for index in (0, 1):
            with self.subTest(index=index):
                sender, sent, receiver, received = udp_observations()
                received["packets"][index]["at_unix_ns"] = sent["window_start_unix_ns"] - 1
                for version in (1, 2):
                    with self.subTest(version=version), self.assertRaisesRegex(ValueError, "before.*send"):
                        traffic.calculate_udp_report(sender, sent, receiver, received, result(), version)

    def test_udp_v3_compares_shared_monotonic_timestamps(self):
        sender, sent, receiver, received = clock_aware_udp_observations()
        # UTC checks alone would pass this impossible duplicate.
        received["packets"][1]["at_monotonic_ns"] = sent["packets"][0]["at_monotonic_ns"] - 1
        with self.assertRaisesRegex(ValueError, "before.*send"):
            traffic.calculate_udp_report(sender, sent, receiver, received, result(), 3)
        received["packets"][1]["at_monotonic_ns"] = received["packets"][0]["at_monotonic_ns"]
        doc = result()
        traffic.calculate_udp_report(sender, sent, receiver, received, doc, 3)
        self.assertEqual(doc["observed"]["clock_comparison"], "host_monotonic")
        received["clock_id"] = "different-clock"
        with self.assertRaisesRegex(ValueError, "clock"):
            traffic.calculate_udp_report(sender, sent, receiver, received, result(), 3)

    def test_udp_report_propagates_degraded_offered_load(self):
        sender, sent, receiver, received = clock_aware_udp_observations()
        sent.update(send_errors=[], send_failure_count=0, attempted_packets=2, skipped_intervals=2)
        documents = [
            {"schema_version": 3, "workload": "udp-send", "status": "partial",
             "config": sender, "observed": sent,
             "calculated": {"offered_load_status": "degraded"}},
            {"schema_version": 3, "workload": "udp-receive", "status": "ok",
             "config": receiver, "observed": received},
        ]
        output = io.StringIO()
        with patch("builtins.open", side_effect=[io.StringIO(json.dumps(d)) for d in documents]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["udp-report", "--sender", "tx", "--receiver", "rx"]), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["calculated"]["offered_load_status"], "degraded")
        self.assertEqual(report["calculated"]["loss_percent"], 50)

    def test_bulk_rejects_numeric_but_contradictory_summaries(self):
        for summary in ({"bytes": 0, "seconds": 1, "bits_per_second": 1_000_000},
                        {"bytes": 1200, "seconds": 1, "bits_per_second": 9999},
                        {"bytes": 1200, "seconds": 0, "bits_per_second": 9600}):
            for command in ("bulk", "bulk-server"):
                with self.subTest(summary=summary, command=command):
                    raw = json.dumps({"end": {"sum_sent": summary, "sum_received": summary}})
                    output = io.StringIO()
                    with patch.object(traffic.subprocess, "run", return_value=subprocess.CompletedProcess(
                            [], 0, raw, "")), contextlib.redirect_stdout(output):
                        self.assertEqual(cli.main([command]), 1)
                    self.assertEqual(json.loads(output.getvalue())["status"], "error")

    def test_bulk_summary_allows_rounding_and_valid_zero(self):
        doc = {"end": {"sum_sent": {"bytes": 0, "seconds": 0, "bits_per_second": 0},
                       "sum_received": {"bytes": 1200, "seconds": 1.234568,
                                        "bits_per_second": 9600 / 1.2345678}}}
        self.assertEqual(traffic.iperf_metrics(doc)["received_bytes"], 1200)

    def test_http_timeout_after_partial_delivery_marks_body_incomplete(self):
        config = vars(cli.run_parser().parse_args(["http"]))
        with patch.object(traffic, "http_alarm", return_value=contextlib.nullcontext()), \
                patch.object(traffic.http.client, "HTTPConnection") as factory:
            response = factory.return_value.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.length = 4
            delivered = []

            def partial_then_timeout(_size):
                delivered.append(b"pa")
                raise TimeoutError("HTTP deadline exceeded")

            response.read.side_effect = partial_then_timeout
            row = traffic.http_request(config)
        self.assertEqual(delivered, [b"pa"])
        self.assertEqual(row["body_bytes"], 0)
        self.assertFalse(row["body_complete"])
        self.assertIn("deadline", row["error"])
