"""Run the Issue #14 fixed, adaptive and repeat-start integration checks."""

import argparse
import ast
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

from experiments import harness
from experiments import integration_checks as checks

PROBE_GRACE_S = 2.5
PROBE_JOBS = (("acl-tcp-server", "file1", "tcp"), ("acl-udp-server", "iot1", "udp"),
              ("acl-staff", "staff1", "probe"), ("acl-student", "student1", "probe"))
SNAPSHOT_TYPES = ("dump-ports", "dump-flows", "queue-stats")


def controller_policy(source):
    names = {"sample_dt", "congest_hi", "congest_lo", "cool_down", "busy_persist"}
    tree = ast.parse(source.read_text(encoding="utf-8"))
    controller = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == "RyuController")
    init = next(node for node in controller.body
                if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    policy = {}
    for node in init.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self" and target.attr in names):
                policy[target.attr] = ast.literal_eval(node.value)
    if set(policy) != names:
        raise ValueError("Could not identify the current controller timing policy")
    return policy


def plan(port):
    return [(label, harness.configuration({"duration_s": duration, "adaptive": adaptive,
                                           "controller_port": port}))
            for label, duration, adaptive in (
                ("fixed-a", 12, False), ("adaptive", 24, True), ("fixed-a-repeat", 12, False))]


def wait_for(processes, observe, condition, seconds, label):
    deadline = time.monotonic() + seconds
    while True:
        processes.tick()
        observe()
        ready = condition()
        remaining = deadline - time.monotonic()
        if remaining < 0:
            raise TimeoutError("timed out waiting for " + label)
        if ready:
            return
        time.sleep(min(.05, remaining))


def read_snapshot(processes, phase):
    data = {}
    for switch in harness.SWITCHES:
        for metric in SNAPSHOT_TYPES:
            label = "{}-{}-{}".format(phase, switch, metric)
            checks.require(processes.outcomes[label]["status"] == "ok", "snapshot failed: " + label)
            data[switch, metric] = processes.text(label)
    return data


def snapshot(processes, phase):
    harness.snapshots(processes, phase, True)
    return read_snapshot(processes, phase)


def start_acl(processes, observe, duration):
    deadline = time.monotonic() + harness.READINESS_S
    for label, host, mode in PROBE_JOBS:
        if mode == "probe":
            continue
        argv = [sys.executable, "-B", "-m", "experiments.integration_probe", mode]
        processes.start(label, harness.shell_job(argv, host, duration + 80))
        processes.wait_ready(label, lambda: '"event": "ready"' in processes.text(label),
                             max(0, deadline - time.monotonic()))
    for label, host, mode in PROBE_JOBS:
        if mode == "probe":
            argv = [sys.executable, "-B", "-m", "experiments.integration_probe", mode]
            processes.start(label, harness.shell_job(argv, host, duration + 80))
    wait_for(processes, observe, lambda: all(processes.text("acl-" + role).count("\n") >= 2
                                             for role in ("staff", "student")),
             harness.READINESS_S, "first ACL probe observations")


def exercise(processes, infrastructure, config, workload_plan):
    directory = processes.directory
    known_events = []
    events_file = directory / "path-events.json"
    windows = {}
    snapshots = {}

    def observe():
        infrastructure.tick()
        checks.require("controller" not in infrastructure.outcomes, "controller exited during integration")
        text = infrastructure.stderr["controller"].read_text(encoding="utf-8", errors="replace")
        current = checks.path_events(text)
        for event in current[len(known_events):]:
            known_events.append({**event, "observed_at_unix_ns": time.time_ns()})
        for label, _, _ in PROBE_JOBS:
            checks.require(label not in processes.outcomes, "ACL fixture exited before shutdown: " + label)

    def settle(seconds):
        end = time.monotonic() + seconds
        wait_for(processes, observe, lambda: time.monotonic() >= end, seconds + 1, "observations")

    try:
        initial_baseline = read_snapshot(processes, "before")
        warm_start = time.time_ns()
        start_acl(processes, observe, config["duration_s"])
        settle(PROBE_GRACE_S)
        initial = snapshot(processes, "initial-a")
        snapshots["initial"] = "initial-a"
        checks.require(not known_events, "the idle fixture unexpectedly changed path")
        checks.verify_path(initial, initial_baseline, "A", checks.ACL_SERVICES, udp_replies=True)
        checks.verify_denies(initial_baseline["s1", "dump-flows"], initial["s1", "dump-flows"])
        active_start = time.time_ns()
        windows["before_load"] = (warm_start, active_start)
        clients, servers = harness.start_workloads(processes, config, workload_plan)
        active_path = "A"
        if config["adaptive"]:
            wait_for(processes, observe,
                     lambda: known_events or any(name in processes.outcomes for name in clients),
                     config["duration_s"] - 5, "A to B transition during concurrent load")
            checks.require([(event["from"], event["to"]) for event in known_events] == [("A", "B")],
                           "expected A to B during concurrent load")
            active_path = "B"
        settle(PROBE_GRACE_S)
        checks.require(not any(name in processes.outcomes for name in clients),
                       "a workload client completed before its active-path checkpoint")
        active_name = "path-" + active_path.lower()
        active = snapshot(processes, active_name)
        snapshots["active"] = active_name
        checks.verify_path(active, initial, active_path, checks.APPLICATIONS)
        checks.verify_denies(initial["s1", "dump-flows"], active["s1", "dump-flows"])
        windows["under_load"] = (active_start, time.time_ns())

        wait_for(processes, observe, lambda: all(name in processes.outcomes for name in clients + servers),
                 config["duration_s"] + 25, "workloads and listeners to finish")
        harness.finish_workloads(processes, workload_plan, clients, servers)
        if config["adaptive"]:
            wait_for(processes, observe, lambda: len(known_events) >= 2, 35, "low-load recovery to A")
        after = time.time_ns()
        settle(PROBE_GRACE_S)
        recovered = snapshot(processes, "after-load")
        snapshots["after"] = "after-load"
        checks.path_history(known_events, config["adaptive"],
                            controller_policy(harness.ROOT / "controller/ryu_controller.py"))
        checks.verify_path(recovered, active, "A", checks.ACL_SERVICES, udp_replies=True)
        checks.verify_denies(active["s1", "dump-flows"], recovered["s1", "dump-flows"])
        windows["after_load"] = (after, time.time_ns())

        for event in known_events:
            center = event["observed_at_unix_ns"]
            margin = int(PROBE_GRACE_S * 1e9)
            windows[event["from"] + "_to_" + event["to"] + "_before_observed"] = (center - margin, center)
            windows[event["from"] + "_to_" + event["to"] + "_after_observed"] = (center, center + margin)
        for role in ("student", "staff"):
            outcome = processes.stop("acl-" + role, "stopped")
            checks.require(outcome["status"] == "stopped", "could not stop the owned ACL probe")
        records = {role: checks.acl_records(directory, role) for role in ("student", "staff")}
        checks.verify_acl_run(records)
        for label, (start, end) in windows.items():
            try:
                checks.verify_acl(records, start, end)
            except RuntimeError as error:
                raise RuntimeError("{}: {}".format(label, error)) from error
    except (KeyError, ValueError) as error:
        raise RuntimeError("invalid integration evidence: {}".format(error)) from error
    finally:
        harness.write_json(events_file, known_events)
        harness.write_json(directory / "acl-windows.json", windows)
        harness.write_json(directory / "checkpoints.json", snapshots)
        # The harness context stops these foreground probe jobs before removing Mininet.


def validate_result(directory, config, summary):
    checks.require(summary["status"] == "ok", "workload/telemetry status: " + summary["status"])
    telemetry = summary["controller_statistics"]
    checks.require(set(telemetry["port"]["observed"]["datapath_ids"]) == {1, 2, 3, 4},
                   "Ryu port samples did not cover all four datapaths")
    expected = {1, 2, 3, 4} if config["adaptive"] else {1, 2, 4}
    checks.require(expected <= set(telemetry["flow"]["observed"]["datapath_ids"]),
                   "Ryu flow samples did not cover the exercised paths")
    documents = [json.loads((directory / name).read_text(encoding="utf-8"))
                 for name in ("http.json", "udp-receiver.json", "bulk.json")]
    return checks.workload_overlap(*documents)


def run_case(parent, label, config, ryu_manager):
    directory = harness.run_directory(parent, "issue14-" + label)
    result = {"scenario": label, "directory": directory.name, "issues": []}
    print(str(directory), flush=True)
    try:
        summary = harness.execute(config, directory, ryu_manager, exercise=exercise)
        result["harness_status"] = summary["status"]
        result["workload_overlap"] = validate_result(directory, config, summary)
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        result["issues"].append(str(error) or type(error).__name__)
    finally:
        try:
            with harness.Processes(directory) as processes:
                harness.check_idle(processes, config["controller_port"], phase="postflight")
            result["cleanup"] = "verified"
        except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            result["cleanup"] = "unverified"
            result["issues"].append(str(error) or type(error).__name__)
    result["status"] = "pass" if not result["issues"] else "fail"
    harness.write_json(directory / "integration.json", result)
    return result


def system_versions(ryu_manager):
    commands = {"mininet": ["mn", "--version"], "ovs": ["ovs-vsctl", "--version"],
                "iperf3": ["iperf3", "--version"], "ryu": [ryu_manager, "--version"]}
    versions = {"python": platform.python_version(), "os_release": Path("/etc/os-release").read_text(),
                "kernel": platform.release(), "openflow": "1.3"}
    for key, command in commands.items():
        result = subprocess.run(command, check=True, timeout=8, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        versions[key] = result.stdout.strip()
    return versions


def source_hashes():
    files = sorted(path for folder in ("controller", "network", "traffic", "experiments", "tests")
                   for path in (harness.ROOT / folder).glob("*.py"))
    return {path.relative_to(harness.ROOT).as_posix(): sha256(path.read_bytes()).hexdigest() for path in files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("experiments/evidence/issue14"))
    parser.add_argument("--controller-port", type=int, default=17656)
    parser.add_argument("--ryu-manager")
    parser.add_argument("--revision", help="Git base revision of the transferred source")
    parser.add_argument("--plan", action="store_true", help="print the scenarios without starting a network")
    options = parser.parse_args(argv)
    try:
        scenarios = plan(options.controller_port)
        policy = controller_policy(harness.ROOT / "controller/ryu_controller.py")
        if options.plan:
            print(json.dumps({"policy": policy, "scenarios": dict(scenarios)}, indent=2))
            return 0
        if not (sys.platform.startswith("linux") and os.geteuid() == 0):
            raise ValueError("integration requires Linux Mininet and root")
        if not (options.ryu_manager and options.revision):
            raise ValueError("--ryu-manager and --revision are required for a live run")
        before = source_hashes()
        versions = system_versions(options.ryu_manager)
        directory = harness.run_directory(options.output_root, "issue14-suite")
        harness.write_json(directory / "suite-config.json", {
            "revision": options.revision, "source_sha256": before, "environment": versions,
            "controller_policy": policy, "scenarios": dict(scenarios)})
        results = []
        for label, config in scenarios:
            result = run_case(directory, label, config, options.ryu_manager)
            results.append(result)
            if result["status"] != "pass":
                break
        identical = before == source_hashes()
        passed = len(results) == len(scenarios) and all(row["status"] == "pass" for row in results) and identical
        harness.write_json(directory / "suite-result.json", {
            "status": "pass" if passed else "fail", "source_unchanged": identical, "scenarios": results})
        return 0 if passed else 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, harness.interrupt)
    sys.exit(main())
