"""Local integration-contract tests. Historical captures exercise parsers only."""

import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from experiments import harness, integration, integration_checks as checks, integration_probe

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "controller/ryu_controller.py"
HISTORICAL = ROOT / "experiments/evidence/issue13/review/mininet/concurrent-1"


def read_snapshots(directory, phase):
    return {(switch, metric): (directory / "{}-{}-{}.stdout.txt".format(phase, switch, metric)).read_text()
            for switch in harness.SWITCHES for metric in integration.SNAPSHOT_TYPES}


def synthetic_snapshots(path, packets):
    port, transit, _ = checks.PATHS[path]
    rows = {(sw, "dump-flows"): "" for sw in harness.SWITCHES}
    for sw in ("s1", "s4"):
        reverse = sw == "s4"
        rows[sw, "queue-stats"] = "\n".join(
            "  port {} queue {}: bytes=0, pkts={}, errors=0".format(p, q, packets if p == port else 0)
            for p in (4, 5) for q in range(4))
        for service in checks.APPLICATIONS:
            src, dst = (service.destination, service.source) if reverse else (service.source, service.destination)
            header = " cookie=0x200, n_packets={}, priority=200,{},".format(packets, service.protocol)
            l4 = "tp_{}={}".format("src" if reverse else "dst", service.port)
            rows[sw, "dump-flows"] += (
                header + "in_port={},nw_src={},nw_dst={},{} actions=set_queue:{},output:{}\n"
                .format(checks.EDGE_PORTS[src], src, dst, l4, service.queue, port))
            rows[transit, "dump-flows"] += header + "{} actions=output:{}\n".format(l4, 1 if reverse else 2)
    return rows


class TestRouteEvidence(unittest.TestCase):
    def test_historical_path_a_and_actual_overlap(self):
        checks.verify_path(read_snapshots(HISTORICAL, "after"), read_snapshots(HISTORICAL, "before"),
                           "A", checks.APPLICATIONS)
        result = checks.workload_overlap(*(json.loads((HISTORICAL / file).read_bytes())
                                          for file in ("http.json", "udp-receiver.json", "bulk.json")))
        self.assertGreater(result["duration_ns"], 0)

    def test_path_b_requires_both_edge_rules_and_transit_activity(self):
        baseline = synthetic_snapshots("B", 0)
        active = synthetic_snapshots("B", 2)
        checks.verify_path(active, baseline, "B", checks.APPLICATIONS)
        for artifact, before, after, diagnostic in (
            (("s4", "dump-flows"), "set_queue:1,output:5", "set_queue:1,output:4", "edge rule"),
            (("s1", "dump-flows"), "set_queue:2", "set_queue:0", "edge rule"),
            (("s3", "dump-flows"), "n_packets=2", "n_packets=0", "transit forwarding"),
            (("s1", "queue-stats"), "pkts=2", "pkts=0", "did not advance"),
            (("s4", "queue-stats"), "port 4 queue 0", "port 2 queue 0", "missing configured"),
        ):
            with self.subTest(artifact=artifact, diagnostic=diagnostic):
                altered = dict(active)
                altered[artifact] = altered[artifact].replace(before, after)
                with self.assertRaisesRegex(RuntimeError, diagnostic):
                    checks.verify_path(altered, baseline, "B", checks.APPLICATIONS)

    def test_stale_inactive_transit_is_a_failure(self):
        active = synthetic_snapshots("B", 2)
        active["s2", "dump-flows"] = active["s3", "dump-flows"]
        with self.assertRaisesRegex(RuntimeError, "inactive transit"):
            checks.verify_path(active, synthetic_snapshots("B", 0), "B", checks.APPLICATIONS)

    def test_route_events_need_order_and_policy_compliance(self):
        policy = integration.controller_policy(CONTROLLER)
        log = "ADAPTIVE: switch A -> B (util A=85% B=2%)\nADAPTIVE: switch B -> A (util A=2% B=19%)"
        events = checks.path_events(log)
        checks.path_history(events, True, policy)
        checks.path_history([], False, policy)
        for rows, adaptive, error in (([], True, "history"), (events, False, "history"),
                                      (events[::-1], True, "history")):
            with self.subTest(error=error, adaptive=adaptive):
                with self.assertRaisesRegex(RuntimeError, error):
                    checks.path_history(rows, adaptive, policy)
        events[1]["util_b_pct_rounded"] = 60
        with self.assertRaisesRegex(RuntimeError, "recovery event"):
            checks.path_history(events, True, policy)

    def test_controller_policy_persistence_and_cooldown(self):
        policy = integration.controller_policy(CONTROLLER)
        tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))
        controller = next(node for node in tree.body
                          if isinstance(node, ast.ClassDef) and node.name == "RyuController")
        decision = next(node for node in controller.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_decide_reroute")
        tick = Mock(return_value=0.0)
        scope = {"time": SimpleNamespace(time=tick)}
        exec(compile(ast.Module(body=[decision], type_ignores=[]), str(CONTROLLER), "exec"), scope)
        state = SimpleNamespace(**policy, path_util={"A": policy["congest_hi"], "B": 0},
                                current_path="A", cooldown_until=0, congest_count=0)

        def switch(path):
            state.current_path = path
            state.cooldown_until = tick() + policy["cool_down"]

        state._switch_path = Mock(side_effect=switch)
        decide = lambda: scope["_decide_reroute"](state)
        for _ in range(policy["busy_persist"] - 1):
            decide()
        state._switch_path.assert_not_called()
        decide()
        state._switch_path.assert_called_once_with("B")
        state.path_util["B"] = policy["congest_lo"] - 1
        decide()
        self.assertEqual(state.congest_count, 0)
        tick.return_value = state.cooldown_until
        state.path_util["B"] = policy["congest_lo"]
        for _ in range(policy["busy_persist"]):
            decide()
        self.assertEqual(state.current_path, "B")
        state.path_util["B"] -= 1
        decide()
        self.assertEqual(state.current_path, "B")
        self.assertEqual(state.congest_count, 0)
        state.path_util["A"] = policy["congest_hi"] - 1
        for _ in range(policy["busy_persist"]):
            decide()
        self.assertEqual(state.current_path, "A")


class TestIntegrationACL(unittest.TestCase):
    def test_probe_windows_include_both_roles_and_transports(self):
        records = {role: [{"protocol": protocol, "status": verdict, "started_ns": 2, "finished_ns": 8}
                          for protocol in ("tcp", "udp")]
                   for role, verdict in (("student", "timeout"), ("staff", "reply"))}
        checks.verify_acl(records, 1, 9)
        checks.verify_acl_run(records)
        with self.assertRaisesRegex(RuntimeError, "in this interval"):
            checks.verify_acl(records, 3, 9)
        records["student"][1]["status"] = "reply"
        with self.assertRaisesRegex(RuntimeError, "student udp"):
            checks.verify_acl_run(records)

    def test_denied_packet_must_hit_actual_matching_drop_flow(self):
        flow = ("cookie=0x0, n_packets=2, priority=300,tcp,in_port=1,nw_src=10.0.10.1,"
                "nw_dst=10.0.40.20,tp_dst=22 actions=drop\n"
                "cookie=0x0, n_packets=3, priority=300,udp,in_port=1,nw_src=10.0.10.1,"
                "nw_dst=10.0.40.30,tp_dst=5001 actions=drop")
        checks.verify_denies("", flow)
        with self.assertRaisesRegex(RuntimeError, "udp deny counter"):
            checks.verify_denies("", flow.replace("tp_dst=5001 actions=drop", "tp_dst=5001 actions=output:4"))
        with self.assertRaisesRegex(RuntimeError, "tcp deny counter"):
            checks.verify_denies(flow, flow)

    def test_staff_handshake_timeout_is_retained_without_counting_as_reply(self):
        def row(protocol, status):
            return {"protocol": protocol, "status": status, "started_ns": 2, "finished_ns": 8}

        staff_partial = row("tcp", "connected_without_reply")
        records = {"student": [row("tcp", "timeout"), row("udp", "timeout")],
                   "staff": [staff_partial, row("udp", "reply")]}
        checks.verify_acl_run(records)
        with self.assertRaisesRegex(RuntimeError, "staff tcp"):
            checks.verify_acl(records, 1, 9)
        records["staff"].append(row("tcp", "reply"))
        checks.verify_acl(records, 1, 9)
        records["student"].append(staff_partial)
        with self.assertRaisesRegex(RuntimeError, "student tcp"):
            checks.verify_acl_run(records)

    def test_tcp_handshake_is_a_denial_failure_even_when_echo_is_missing(self):
        with patch.object(integration_probe.socket, "socket") as factory:
            client = factory.return_value.__enter__.return_value
            client.recv.side_effect = integration_probe.socket.timeout
            self.assertEqual(integration_probe.attempt(checks.ACL_SERVICES[0]), "connected_without_reply")


class TestIntegrationLifecycle(unittest.TestCase):
    def test_plan_round_trip_fixed_repeat_and_sustained_adaptive(self):
        a, adaptive, repeat = integration.plan(17658)
        self.assertEqual(a[1], repeat[1])
        self.assertFalse(a[1]["adaptive"])
        self.assertTrue(adaptive[1]["adaptive"])
        self.assertGreater(adaptive[1]["duration_s"], a[1]["duration_s"])
        for _, config in (a, adaptive, repeat):
            self.assertTrue(config["qos"])
            self.assertEqual(config["workloads"], ["http", "udp", "bulk"])
            self.assertEqual(config["controller_port"], 17658)

    def test_postflight_runs_even_after_harness_failure_and_blocks_success(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(harness, "execute", side_effect=RuntimeError("exercise failure")), \
                patch.object(harness, "check_idle", side_effect=RuntimeError("still busy")) as guard:
            label, config = integration.plan(17658)[0]
            result = integration.run_case(Path(temp), label, config, "ryu-manager")
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["cleanup"], "unverified")
            self.assertEqual(result["issues"], ["exercise failure", "still busy"])
            self.assertEqual(guard.call_args.kwargs["phase"], "postflight")
            self.assertTrue((Path(temp) / result["directory"] / "integration.json").is_file())

    def test_missing_telemetry_stops_acceptance(self):
        summary = {"status": "ok", "controller_statistics": {
            "port": {"observed": {"datapath_ids": [1, 2, 4]}}}}
        with self.assertRaisesRegex(RuntimeError, "all four"):
            integration.validate_result(Path("."), integration.plan(17656)[0][1], summary)

    def test_controller_and_probes_are_guarded(self):
        for ready in (False, True):
            with self.subTest(ready=ready):
                proc = Mock()
                with patch.object(integration.time, "monotonic", side_effect=[0.0, 1.0]):
                    with self.assertRaisesRegex(TimeoutError, "path result"):
                        integration.wait_for(proc, lambda: None, lambda: ready, .5, "path result")
                proc.tick.assert_called_once()


if __name__ == "__main__":
    unittest.main()
