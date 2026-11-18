import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments import harness, metrics


EVIDENCE = Path(__file__).resolve().parents[1] / "experiments/evidence/issue12/schema3"


def read(path):
    return json.loads(path.read_bytes())


class TestExperimentMetrics(unittest.TestCase):
    def test_extracts_approved_concurrent_evidence(self):
        summary = metrics.summarize(EVIDENCE / "concurrent-1", ("http", "udp", "bulk"))
        self.assertEqual(summary["status"], "ok")
        http, udp, bulk = (summary["workloads"][kind] for kind in ("http", "udp", "bulk"))
        self.assertEqual(http["observed"]["http_requests"], 24)
        self.assertAlmostEqual(http["calculated"]["http_response_time_ms"]["mean"], 28.778730875)
        self.assertIsNone(http["calculated"]["one_way_latency_ms"])
        self.assertIsNone(http["calculated"]["packet_loss_pct"])
        self.assertAlmostEqual(udp["calculated"]["udp_send_pps"], 796.3148439644096)
        self.assertEqual(udp["calculated"]["packet_loss_pct"], 0)
        self.assertGreater(udp["calculated"]["udp_app_delay_ms"]["p95"], 0)
        self.assertEqual(bulk["reported"]["tcp_receiver_bps"], 87194372.77941693)
        self.assertEqual(bulk["reported"]["tcp_retransmissions"], 0)
        self.assertIsNone(bulk["calculated"]["packet_loss_pct"])

    def test_udp_pairs_by_sequence_and_uses_first_arrival(self):
        clock = "same-clock"
        tx = {"clock_id": clock, "source_port": 40001, "packets": [
            {"sequence": 4, "payload_bytes": 24, "at_monotonic_ns": 1_000_000},
            {"sequence": 7, "payload_bytes": 24, "at_monotonic_ns": 3_000_000}]}
        rx = {"clock_id": clock, "packets": [
            {"sequence": 7, "payload_bytes": 24, "source_port": 40001, "at_monotonic_ns": 9_000_000},
            {"sequence": 4, "payload_bytes": 24, "source_port": 61001, "at_monotonic_ns": 1_100_000},
            {"sequence": 4, "payload_bytes": 24, "source_port": 40001, "at_monotonic_ns": 3_000_000},
            {"sequence": 7, "payload_bytes": 24, "source_port": 40001, "at_monotonic_ns": 7_000_000}]}
        result = metrics.udp_timing(tx, rx)
        self.assertEqual(result["udp_app_delay_ms"],
                         {"count": 2, "min": 2.0, "mean": 3.0, "p95": 4.0, "max": 4.0})
        self.assertEqual(result["udp_app_delay_variation_ms"]["mean"], 2.0)
        rx["clock_id"] = "different"
        self.assertIsNone(metrics.udp_timing(tx, rx)["udp_app_delay_ms"])
        rx["clock_id"] = clock
        rx["packets"][0]["at_monotonic_ns"] = 0
        with self.assertRaisesRegex(ValueError, "before"):
            metrics.udp_timing(tx, rx)

    def test_missing_and_failed_observations_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            result = metrics.summarize(p, ("http", "bulk"))
            self.assertEqual(result["status"], "incomplete")
            evidence = read(EVIDENCE / "concurrent-1/http.json")
            evidence["status"] = "error"
            evidence["error"] = "example failure"
            for request in evidence["observed"]["requests"]:
                request.update(status_code=503, error="HTTP 503")
            evidence["observed"]["failure_count"] = evidence["observed"]["request_count"]
            (p / "http.json").write_text(json.dumps(evidence), encoding="utf-8")
            result = metrics.summarize(p, ("http", "bulk"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["workloads"]["http"]["status"], "failed")
            self.assertEqual(result["workloads"]["bulk"]["status"], "incomplete")

    def test_degraded_udp_preserves_partial_metrics(self):
        result = metrics.summarize(EVIDENCE / "udp-paused", ("udp",))
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["workloads"]["udp"]["calculated"]["packet_loss_pct"], 0)
        self.assertEqual(result["workloads"]["udp"]["calculated"]["udp_send_pps"],
                         read(EVIDENCE / "udp-paused/udp-send.json")["calculated"]["sent_packets_per_s"])

    def test_inconsistent_bulk_metrics_are_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            bulk = read(EVIDENCE / "bulk-only/bulk.json")
            bulk["reported"]["receiver_bps"] += 1000
            (path / "bulk.json").write_text(json.dumps(bulk), encoding="utf-8")
            result = metrics.summarize(path, ("bulk",))
            self.assertEqual(result["status"], "incomplete")
            self.assertNotIn("tcp_receiver_bps", result["workloads"]["bulk"]["reported"])

    def test_controller_flow_match_commas_remain_one_field(self):
        result = metrics.controller_stats(EVIDENCE)
        self.assertGreater(result["port"]["observed"]["rows"], 0)
        self.assertGreater(result["flow"]["observed"]["rows"], 0)
        self.assertIn(4, result["flow"]["observed"]["datapath_ids"])
        self.assertIsNone(metrics.controller_stats(EVIDENCE / "missing")["port"]["observed"]["rows"])

    def test_ping_reports_rtt_without_claiming_application_loss(self):
        sample = ("3 packets transmitted, 2 received, 33.3333% packet loss, time 2001ms\n"
                  "rtt min/avg/max/mdev = 4.010/5.015/6.020/1.005 ms\n")
        value = metrics.ping_stats(sample)
        self.assertEqual(value["observed"], {"icmp_probes_sent": 3, "icmp_echo_replies": 2})
        self.assertAlmostEqual(value["calculated"]["icmp_packet_loss_pct"], 100/3)
        self.assertEqual(value["reported"]["icmp_rtt_ms"]["mean"], 5.015)
        self.assertIsNone(metrics.ping_stats("3 packets transmitted, 0 received, 100% packet loss")["reported"]["icmp_rtt_ms"])


class TestHarness(unittest.TestCase):
    def test_invalid_config_and_repeatable_plan(self):
        for invalid in ({"workloads": []}, {"workloads": ["udp", "udp"]},
                        {"duration_s": True}, {"bogus": 1}, {"udp_port": 5201},
                        {"http_interval_s": float("nan")}, {"bulk_target_bps": -1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                harness.configuration(invalid)
        config = harness.configuration({"workloads": ["udp", "bulk"], "duration_s": 6})
        first = harness.workload_plan(config, Path("run-a"), "python")
        second = harness.workload_plan(config, Path("run-b"), "python")
        self.assertEqual(first["udp-send"]["host"], "staff1")
        self.assertEqual(first["udp-receiver"]["host"], "iot1")
        self.assertNotEqual(first["udp-send"]["argv"], second["udp-send"]["argv"])
        self.assertEqual(first["bulk"]["argv"], second["bulk"]["argv"])
        for key in ("udp-send", "udp-receiver"):
            args = first[key]["argv"]
            self.assertEqual(args[args.index("--stream-id") + 1],
                             first["udp-send"]["argv"][first["udp-send"]["argv"].index("--stream-id") + 1])

    def test_unique_run_records_launch_and_retains_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            first, second = harness.run_directory(p), harness.run_directory(p)
            self.assertNotEqual(first, second)
            job = {"host": None, "argv": [sys.executable, "-B", "-c", "import time; time.sleep(5)"],
                   "timeout_s": 0.2}
            with harness.Processes(first) as commands:
                commands.start("slow", job)
                self.assertEqual(commands.finish("slow")["status"], "timeout")
                self.assertIsNotNone(commands.children["slow"].poll())
            log = [json.loads(line) for line in (first / "commands.jsonl").read_text().splitlines()]
            self.assertEqual(log[0]["argv"], job["argv"])
            self.assertGreater(log[1]["ended_at_unix_ns"], log[0]["started_at_unix_ns"])
            self.assertTrue((first / "slow.stdout.txt").is_file())
            self.assertEqual(log[1]["status"], "timeout")

    def test_readiness_failure_stops_owned_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            job = {"host": None, "argv": [sys.executable, "-B", "-c", "import time; time.sleep(5)"],
                   "timeout_s": 10}
            with self.assertRaises(TimeoutError):
                with harness.Processes(p) as commands:
                    commands.start("listener", job)
                    commands.start("sibling", job)
                    commands.wait_ready("listener", lambda: False, 0.1)
            self.assertIsNotNone(commands.children["listener"].poll())
            self.assertIsNotNone(commands.children["sibling"].poll())
            self.assertEqual(commands.outcomes["listener"]["status"], "timeout")
            self.assertEqual(commands.outcomes["sibling"]["status"], "stopped")

    def test_slack_is_measured_from_launch_independent_of_wait_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Mock(pid=123)
            fake.poll.return_value = None
            fake.wait.return_value = 0
            job = {"host": None, "argv": ["test"], "timeout_s": 5}
            commands = harness.Processes(Path(tmp))
            observed = iter((10, 14, 14))
            with patch.object(harness.subprocess, "Popen", return_value=fake), \
                    patch.object(harness.time, "monotonic", side_effect=lambda: next(observed)), \
                    patch.object(commands, "exited", return_value=True), \
                    patch.object(commands, "terminate"):
                commands.start("probe", job)
                outcome = commands.finish("probe")
            self.assertEqual(commands.deadlines["probe"], 15)
            self.assertEqual(outcome["status"], "ok")
            fake.wait.assert_called_once_with(timeout=0)

    def test_concurrent_wait_enforces_earlier_deadline_of_second_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            job = {"host": None, "argv": [sys.executable, "-B", "-c", "import time; time.sleep(0.6)"],
                   "timeout_s": 3}
            with harness.Processes(path) as commands:
                commands.start("first", job)
                commands.start("second", dict(job, timeout_s=0.2))
                commands.finish_many(("first", "second"))
            self.assertEqual(commands.outcomes["first"]["status"], "ok")
            self.assertEqual(commands.outcomes["second"]["status"], "timeout")
            self.assertLess(commands.outcomes["second"]["ended_at_unix_ns"],
                            commands.outcomes["first"]["ended_at_unix_ns"])

    def test_network_setup_failure_preserves_run_summary_and_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            config = harness.configuration({"workloads": ["bulk"]})
            with patch.object(harness, "diamond", side_effect=RuntimeError("example network failure")):
                result = harness.execute(config, path, "/unused/ryu-manager")
            self.assertEqual(result["status"], "failed")
            self.assertIn("example network failure", result["issues"][0])
            self.assertEqual(result["workloads"]["bulk"]["status"], "incomplete")
            self.assertEqual(read(path / "configuration.json"), config)
            self.assertEqual(read(path / "summary.json"), result)
            self.assertIn("bulk", read(path / "plan.json"))
