import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from experiments import harness, metrics
from test_harness import EVIDENCE, read


class Clock:
    now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class TestDeadlines(unittest.TestCase):
    def test_slow_cleanup_does_not_mask_late_success_of_another_child(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(harness.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(harness.time, "sleep", side_effect=clock.sleep), \
                patch.object(harness.Processes, "exited", lambda self, label: self.children[label].poll() is not None):
            run = harness.Processes(Path(tmp))
            first, second = Mock(pid=1001), Mock(pid=1002)
            forced = set()
            signals = []

            def signal_child(child, force=False):
                signals.append((child.pid, force, clock.now))
                if force:
                    forced.add(child.pid)

            first.poll.side_effect = lambda: -9 if first.pid in forced else None
            second.poll.side_effect = lambda: 0 if clock.now >= .3 else None
            first.wait.side_effect = lambda timeout: (clock.sleep(.4) or -9) if timeout else -9
            second.wait.return_value = 0
            run.children.update(first=first, second=second)
            run.deadlines.update(first=.1, second=.2)
            with patch.object(run, "terminate", side_effect=signal_child):
                run.finish_many(("first", "second"))
            self.assertEqual(run.outcomes["first"]["status"], "timeout")
            self.assertEqual(run.outcomes["second"]["status"], "timeout")
            early_signal = next(at for pid, force, at in signals if pid == second.pid and not force)
            self.assertLessEqual(early_signal, .221)

    def test_late_readiness_is_not_accepted_after_probe_returns(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(harness.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(harness.time, "sleep", side_effect=clock.sleep), \
                patch.object(harness.Processes, "exited", lambda self, label: self.children[label].poll() is not None):
            run = harness.Processes(Path(tmp))
            child = Mock(pid=1001)
            child.poll.return_value = None
            child.wait.return_value = -15
            run.children["listener"] = child
            run.deadlines["listener"] = 5
            with patch.object(run, "terminate", side_effect=lambda *_a, **_kw:
                              setattr(child.poll, "return_value", -15)):
                with self.assertRaises(TimeoutError):
                    run.wait_ready("listener", lambda: (clock.sleep(.2) or True), .1)
            self.assertEqual(run.outcomes["listener"]["status"], "timeout")
            self.assertNotIn('"event": "ready"', (Path(tmp) / "commands.jsonl").read_text())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process group regression")
    def test_abnormal_wrapper_exit_reaps_owned_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "child.pid"
            child = ("import os,pathlib,signal,sys,time;"
                     "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                     "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(90)")
            wrapper = (
                "import os, pathlib, subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-B', '-c', sys.argv[1], sys.argv[2]])\n"
                "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n"
                "os._exit(7)\n")

            def executable(pid, group):
                try:
                    state, _, pgid = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()[:3]
                    return int(pgid) == group and state != "Z"
                except FileNotFoundError:
                    return False

            with subprocess.Popen([sys.executable, "-B", "-c", "import time;time.sleep(10)"],
                                  start_new_session=True) as unrelated:
                try:
                    with harness.Processes(root) as run:
                        run.start("wrapper", harness.shell_job(
                            [sys.executable, "-B", "-c", wrapper, child, str(marker)], timeout=4))
                        group = run.children["wrapper"].pid
                        begin = time.monotonic()
                        outcome = run.finish("wrapper")
                    pid = int(marker.read_text())
                    self.assertEqual(outcome["returncode"], 7)
                    until = time.monotonic() + .5
                    while executable(pid, group) and time.monotonic() < until:
                        time.sleep(.01)
                    self.assertFalse(executable(pid, group))
                    self.assertLess(time.monotonic() - begin, 5)
                    self.assertIsNone(unrelated.poll())
                finally:
                    unrelated.terminate()
                    if marker.exists():
                        pid = int(marker.read_text())
                        if executable(pid, group):
                            os.kill(pid, signal.SIGKILL)


class TestStatusIntegrity(unittest.TestCase):
    def test_http_status_matches_completed_request_records(self):
        original = read(EVIDENCE / "concurrent-1/http.json")
        for case in ("empty", "failed-request", "incomplete-body", "invalid-status-code"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp)
                doc = copy.deepcopy(original)
                observed = doc["observed"]
                row = observed["requests"][0]
                if case == "empty":
                    observed.update(requests=[], request_count=0, failure_count=0)
                elif case == "failed-request":
                    row.update(status_code=503,error="HTTP 503")
                    observed["failure_count"] = 1
                elif case == "incomplete-body":
                    row["body_complete"] = False
                else:
                    row["status_code"] = 700
                (p / "http.json").write_text(json.dumps(doc), encoding="utf-8")
                result = metrics.summarize(p, ("http",))
                self.assertEqual(result["status"], "incomplete")
                self.assertIn("http.json", " ".join(result["workloads"]["http"]["issues"]))

    def test_udp_envelopes_match_recomputed_statuses(self):
        for trial, false_status in (("concurrent-1","partial"),("udp-paused","ok")):
            with self.subTest(trial=trial), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp)
                for name in ("udp-send", "udp-receiver", "udp-report"):
                    filename = name + ".json"
                    doc = read(EVIDENCE / trial / filename)
                    if name != "udp-receiver":
                        doc["status"] = false_status
                    (p / filename).write_text(json.dumps(doc), encoding="utf-8")
                result = metrics.summarize(p, ("udp",))
                self.assertEqual(result["status"], "incomplete")
                self.assertIn("udp-send.json", " ".join(result["workloads"]["udp"]["issues"]))

    def test_error_udp_artifacts_with_complete_captures_are_incomplete(self):
        for bad_name, include_report in (("udp-send", True), ("udp-receiver", True),
                                         ("udp-report", True), ("udp-send", False)):
            with self.subTest(artifact=bad_name, include_report=include_report), \
                    tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                for name in metrics.WORKLOAD_FILES["udp"]:
                    if name == "udp-report" and not include_report:
                        continue
                    doc = read(EVIDENCE / "concurrent-1" / (name + ".json"))
                    if name == bad_name:
                        doc.update(status="error", error="claimed failure")
                    (path / (name + ".json")).write_text(json.dumps(doc), encoding="utf-8")
                outcomes = {bad_name: {"status": "nonzero", "returncode": 1}}
                result = metrics.summarize(path, ("udp",), process_outcomes=outcomes)
                self.assertEqual(result["status"], "incomplete")
                self.assertIn(bad_name + ".json status disagrees",
                              " ".join(result["workloads"]["udp"]["issues"]))

    def test_udp_error_without_sufficient_endpoint_observations_stays_failed(self):
        for failed_name, sent_observed in (("udp-send", {}), ("udp-send", {"packets": []}),
                                           ("udp-receiver", {"packets": []})):
            with self.subTest(artifact=failed_name, observed=sent_observed), \
                    tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                for name in ("udp-send", "udp-receiver"):
                    doc = read(EVIDENCE / "concurrent-1" / (name + ".json"))
                    if name == failed_name:
                        doc.update(status="error", error="socket failure", observed=sent_observed)
                    (path / (name + ".json")).write_text(json.dumps(doc), encoding="utf-8")
                result = metrics.summarize(path, ("udp",))
                self.assertEqual(result["status"], "failed")
                udp = result["workloads"]["udp"]
                self.assertIn(failed_name + ".json: socket failure", udp["issues"])
                self.assertIsNone(udp["calculated"]["packet_loss_pct"])

    def test_udp_status_validation_matrix(self):
        send_exit = "udp-send.json: process exit code disagrees"
        report_exit = "udp-report.json: process exit code disagrees"
        send_missing_exit = "udp-send.json: missing or invalid process returncode"
        report_missing_exit = "udp-report.json: missing or invalid process returncode"
        send_status = "udp-send.json status disagrees"
        report_status = "udp-report.json status disagrees"
        raw_sender = "udp-send.json / udp-receiver.json: sent_packets does not match"
        raw_receiver = "udp-send.json / udp-receiver.json: received_packets does not match"
        report_metrics = "udp-report.json does not agree with the raw endpoint captures"
        cases = (
            ("matching ok", {}, "ok", ()),
            ("matching degraded", {"trial": "udp-paused"}, "degraded", ()),
            ("historical ok without metadata", {"codes": ("unrecorded", "unrecorded")}, "ok", ()),
            ("historical degraded without metadata", {"trial": "udp-paused",
             "codes": ("unrecorded", "unrecorded")}, "degraded", ()),
            ("contradictory ok exit", {"codes": ("wrong", "matching")}, "incomplete", (send_exit,)),
            ("contradictory partial exit", {"trial": "udp-paused",
             "codes": ("matching", "wrong")}, "incomplete", (report_exit,)),
            ("missing ok exit", {"codes": ("missing", "matching")}, "incomplete", (send_missing_exit,)),
            ("null partial exit", {"trial": "udp-paused",
             "codes": ("matching", "null")}, "incomplete", (report_missing_exit,)),
            ("error sender with incomplete capture and missing report", {"states": ("error", "absent"),
             "capture": "missing-sender"}, "failed", ("udp-send.json: sender failed",)),
            ("no successful UDP sends", {"states": ("error", "error"),
             "capture": "zero-sent"}, "failed", ("udp-send.json: sender failed",)),
            ("error report after missing receiver", {"states": ("ok", "error"),
             "capture": "missing-receiver"}, "failed", ("udp-report.json: report failed",)),
            ("error sender without exit metadata and insufficient data", {"states": ("error", "absent"),
             "codes": ("unrecorded", "unrecorded"), "capture": "missing-sender"},
             "failed", ("udp-send.json: sender failed",)),
            ("unconfirmed error with insufficient data", {"states": ("error", "absent"),
             "codes": ("wrong", "unrecorded"), "capture": "missing-sender"}, "incomplete", (send_exit,)),
            ("error sender despite valid captures", {"states": ("error", "ok")},
             "incomplete", (send_status,)),
            ("error report despite valid captures", {"states": ("ok", "error")},
             "incomplete", (report_status,)),
            ("false ok report for degraded captures", {"trial": "udp-paused",
             "states": ("partial", "ok")}, "incomplete", (report_status,)),
            ("false partial sender for good captures", {"states": ("partial", "ok")},
             "incomplete", (send_status,)),
            ("two false errors and contradictory sender exit", {"states": ("error", "error"),
             "codes": ("wrong", "matching")}, "incomplete", (send_exit, send_status, report_status)),
            ("two false errors and missing sender exit", {"states": ("error", "error"),
             "codes": ("missing", "matching")}, "incomplete", (send_missing_exit, send_status, report_status)),
            ("two false errors and contradictory report exit", {"states": ("error", "error"),
             "codes": ("matching", "wrong")}, "incomplete", (report_exit, send_status, report_status)),
            ("complete corrupt sender and contradictory exits", {"states": ("error", "error"),
             "codes": ("wrong", "wrong"), "capture": "corrupt-sender"},
             "incomplete", (send_exit, report_exit, raw_sender)),
            ("complete corrupt receiver with error report", {"states": ("ok", "error"),
             "codes": ("missing", "matching"), "capture": "corrupt-receiver"},
             "incomplete", (send_missing_exit, raw_receiver)),
            ("corrupt report and wrong sender exit", {"codes": ("wrong", "matching"),
             "corrupt_report": True}, "incomplete", (send_exit, report_metrics)),
            ("corrupt report, false partial sender and missing report exit", {"states": ("partial", "ok"),
             "codes": ("matching", "missing"), "corrupt_report": True},
             "incomplete", (report_missing_exit, send_status, report_metrics)),
        )
        for case, overrides, expected, reasons in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                names = ("udp-send", "udp-receiver", "udp-report")
                trial = EVIDENCE / overrides.get("trial", "concurrent-1")
                docs = {name: read(trial / (name + ".json")) for name in names}
                default_states = tuple(docs[name]["status"] for name in ("udp-send", "udp-report"))
                sender_status, report_status = overrides.get("states", default_states)
                if sender_status == "error":
                    docs["udp-send"]["error"] = "sender failed"
                if report_status == "error":
                    docs["udp-report"]["error"] = "report failed"
                docs["udp-send"]["status"] = sender_status
                docs["udp-report"]["status"] = report_status

                capture = overrides.get("capture")
                if capture == "corrupt-sender":
                    docs["udp-send"]["observed"]["sent_packets"] += 1
                elif capture == "corrupt-receiver":
                    docs["udp-receiver"]["observed"]["received_packets"] += 1
                elif capture == "missing-sender":
                    docs["udp-send"]["observed"] = {}
                elif capture == "missing-receiver":
                    del docs["udp-receiver"]
                elif capture == "zero-sent":
                    docs["udp-send"]["observed"].update(packets=[], sent_packets=0, sent_payload_bytes=0)
                    docs["udp-receiver"]["observed"].update(
                        packets=[], received_packets=0, received_payload_bytes=0)
                if report_status == "absent":
                    del docs["udp-report"]
                elif overrides.get("corrupt_report"):
                    docs["udp-report"]["calculated"]["loss_percent"] = -1

                outcomes = {}
                code_modes = overrides.get("codes", ("matching", "matching"))
                for name, state, mode in zip(("udp-send", "udp-report"),
                                             (sender_status, report_status), code_modes):
                    if state == "absent" or mode == "unrecorded":
                        continue
                    code = {"ok": 0, "partial": 1, "error": 1}[state]
                    if mode == "missing":
                        outcomes[name] = {"status": "ok" if code == 0 else "nonzero"}
                    else:
                        if mode == "wrong":
                            code = 1 - code
                        elif mode == "null":
                            code = None
                        outcomes[name] = {"status": "ok" if code == 0 else "nonzero",
                                          "returncode": code}
                for name, doc in docs.items():
                    (path / (name + ".json")).write_text(json.dumps(doc), encoding="utf-8")
                summary = metrics.summarize(path, ("udp",), process_outcomes=outcomes)
                actual = summary["workloads"]["udp"]
                self.assertEqual(summary["status"], expected)
                self.assertEqual(actual["status"], expected)
                for reason in reasons:
                    self.assertIn(reason, " ".join(actual["issues"]))
                if expected in ("ok", "degraded"):
                    self.assertGreater(actual["calculated"]["udp_send_pps"], 0)
                else:
                    self.assertIsNone(actual["calculated"]["packet_loss_pct"])

    def test_started_icmp_without_finish_is_incomplete(self):
        for has_stdout in (False, True):
            with self.subTest(has_stdout=has_stdout), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                (path / "http.json").write_bytes((EVIDENCE / "concurrent-1/http.json").read_bytes())
                (path / "commands.jsonl").write_text(
                    json.dumps({"event": "start", "label": "http-ping"}) + "\n", encoding="utf-8")
                if has_stdout:
                    (path / "http-ping.stdout.txt").write_text(
                        "3 packets transmitted, 0 received, 100% packet loss\n", encoding="utf-8")
                for outcomes in (None, {}):
                    summary = metrics.summarize(path, ("http",), process_outcomes=outcomes)
                    self.assertEqual(summary["status"], "incomplete")
                    http = summary["workloads"]["http"]
                    self.assertIn("commands.jsonl", " ".join(http["issues"]))
                    self.assertIn("http-ping", " ".join(http["issues"]))
                    self.assertNotIn("icmp_probe", http)

    def test_icmp_finish_metadata_requires_an_integer_exit_code(self):
        for fields in ({}, {"returncode": None}, {"returncode": "1"}, {"returncode": True},
                       {"returncode": 1.0}):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                (path / "http.json").write_bytes((EVIDENCE / "concurrent-1/http.json").read_bytes())
                (path / "http-ping.stdout.txt").write_text(
                    "3 packets transmitted, 0 received, 100% packet loss\n", encoding="utf-8")
                command = {"event": "finish", "label": "http-ping", "status": "nonzero", **fields}
                (path / "commands.jsonl").write_text(json.dumps(command) + "\n", encoding="utf-8")
                summary = metrics.summarize(path, ("http",))
                self.assertEqual(summary["status"], "incomplete")
                http = summary["workloads"]["http"]
                self.assertIn("commands.jsonl", " ".join(http["issues"]))
                self.assertIn("returncode", " ".join(http["issues"]))
                self.assertNotIn("icmp_probe", http)

    def test_icmp_command_log_preserves_total_loss_and_flags_missing_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "http.json").write_bytes((EVIDENCE / "concurrent-1/http.json").read_bytes())
            entries = ({"event": "start", "label": "http-ping"},
                       {"event": "finish", "label": "http-ping", "status": "nonzero", "returncode": 1})
            (path / "commands.jsonl").write_text(
                "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
            summary = metrics.summarize(path, ("http",))
            self.assertEqual(summary["status"], "incomplete")
            self.assertIn("http-ping.stdout.txt", " ".join(summary["workloads"]["http"]["issues"]))
            (path / "http-ping.stdout.txt").write_text(
                "3 packets transmitted, 0 received, 100% packet loss\n", encoding="utf-8")
            summary = metrics.summarize(path, ("http",))
            self.assertEqual(summary["status"], "ok")
            probe = summary["workloads"]["http"]["icmp_probe"]
            self.assertEqual(probe["calculated"]["icmp_packet_loss_pct"], 100)
            self.assertIsNone(probe["reported"]["icmp_rtt_ms"])

    def test_process_exit_code_mismatch_is_incomplete(self):
        for returncode in (0, 2):
            with self.subTest(returncode=returncode):
                summary = metrics.summarize(
                    EVIDENCE / "udp-paused", ("udp",),
                    process_outcomes={"udp-send": {"status": "nonzero" if returncode else "ok",
                                                  "returncode": returncode}})
                self.assertEqual(summary["status"], "incomplete")
                self.assertIn("udp-send.json", " ".join(summary["workloads"]["udp"]["issues"]))

    def test_executed_icmp_with_bad_or_missing_summary_is_incomplete(self):
        for value in (None, "", "ping summary missing", "3 packets transmitted, 2 received, 33% packet loss",
                      "3 packets transmitted, 4 received, 0% packet loss",
                      "3 packets transmitted, 2 received, 90% packet loss\n"
                      "rtt min/avg/max/mdev = 1/2/3/0 ms",
                      "3 packets transmitted, 0 received, 100% packet loss\n"
                      "rtt min/avg/max/mdev = 1/2/3/0 ms"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                (path / "http.json").write_bytes((EVIDENCE / "concurrent-1/http.json").read_bytes())
                if value is not None:
                    (path / "http-ping.stdout.txt").write_text(value, encoding="utf-8")
                result = metrics.summarize(path, ("http",),
                                           process_outcomes={"http-ping": {"status": "ok", "returncode": 0}})
                self.assertEqual(result["status"], "incomplete")
                self.assertIn("http-ping.stdout.txt", " ".join(result["workloads"]["http"]["issues"]))

    def test_genuine_total_icmp_loss_and_historical_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "http.json").write_bytes((EVIDENCE / "concurrent-1/http.json").read_bytes())
            self.assertEqual(metrics.summarize(path, ("http",))["status"], "ok")
            (path / "http-ping.stdout.txt").write_text(
                "3 packets transmitted, 0 received, 100% packet loss, time 400ms\n", encoding="utf-8")
            summary = metrics.summarize(path, ("http",))["workloads"]["http"]
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["icmp_probe"]["calculated"]["icmp_packet_loss_pct"], 100)
            self.assertIsNone(summary["icmp_probe"]["reported"]["icmp_rtt_ms"])


    def test_malformed_stored_icmp_is_incomplete_without_process_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "http.json").write_bytes((EVIDENCE / "concurrent-1/http.json").read_bytes())
            (path / "http-ping.stdout.txt").write_text("ping summary missing", encoding="utf-8")
            result = metrics.summarize(path, ("http",))
            self.assertEqual(result["status"], "incomplete")

    def test_exited_wrapper_still_gets_process_group_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = harness.Processes(Path(tmp))
            leader = Mock(pid=1001, returncode=None)
            leader.wait.return_value = -9
            run.children["bulk"] = leader
            run.deadlines["bulk"] = time.monotonic() + 5
            with patch.object(harness.os, "name", "posix"), \
                    patch.object(run, "exited", return_value=True), \
                    patch.object(run, "terminate") as terminate:
                with run:
                    result = run.finish("bulk")
            self.assertEqual(result["returncode"], -9)
            terminate.assert_called_with(leader, force=True)
