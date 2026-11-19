"""Run reproducible Issue #13 experiments on the project diamond."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from experiments import metrics
from traffic import workloads

ROOT = Path(__file__).resolve().parents[1]
SWITCHES = ("s1", "s2", "s3", "s4")
CLIENTS = {"http": ("student1", workloads.STUDENT, workloads.WEB),
           "udp": ("staff1", workloads.STAFF, workloads.IOT),
           "bulk": ("mgmt1", workloads.MANAGEMENT, workloads.FILE)}
DEFAULTS = {"workloads": ["http", "udp", "bulk"], "duration_s": 6, "http_interval_s": .25,
            "http_timeout_s": 2, "udp_rate_pps": 800, "udp_payload_bytes": 1200,
            "udp_port": 5000, "bulk_target_bps": 0, "controller_port": 17656,
            "qos": True, "adaptive": False}
READINESS_S = 5
POLL_S = .02
TERM_GRACE_S = 2
KILL_GRACE_S = 2
EXIT_STATUS = {"ok": 0, "degraded": 1, "incomplete": 2, "failed": 3}


def configuration(overrides):
    if not isinstance(overrides, dict) or overrides.keys() - DEFAULTS.keys():
        raise ValueError("configuration must be an object containing only documented fields")
    config = {**DEFAULTS, **overrides}
    kinds = config["workloads"]
    if (not isinstance(kinds, list) or not kinds
            or not all(isinstance(kind, str) and kind in CLIENTS for kind in kinds)
            or len(set(kinds)) != len(kinds)):
        raise ValueError("workloads must contain distinct names from http, udp, bulk")
    # All durations and deadlines must be representable on the target host.
    for key, lo, hi in (
        ("duration_s", 1, 86400), ("udp_payload_bytes", workloads.UDP_HEADER.size, 1472),
        ("udp_port", 5000, 5100), ("bulk_target_bps", 0, workloads.MAX_SEQUENCE),
        ("controller_port", 1024, 65535),
    ):
        if type(config[key]) is not int or not lo <= config[key] <= hi:
            raise ValueError("{} must be an integer in [{}, {}]".format(key, lo, hi))
    for key in ("http_interval_s", "http_timeout_s"):
        workloads.duration_ns(config[key])
    for key in ("qos", "adaptive"):
        if type(config[key]) is not bool:
            raise ValueError(key + " must be a boolean")
    workloads.udp_schedule({"duration": config["duration_s"], "rate_pps": config["udp_rate_pps"]})
    config["workloads"] = list(kinds)
    return config


def packet_stream(run_dir):
    # Each unique run directory produces its own reproducible stream ID.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "issue13:" + Path(run_dir).name))


def workload_plan(config, run_dir, python):
    duration = config["duration_s"]
    lifetime = duration + READINESS_S + 3
    stream = packet_stream(run_dir)
    selected = config["workloads"]
    plan = {}

    def add(name, host, timeout, *args):
        plan[name] = {"host": host, "argv": [python, "-B", "-m", "traffic", *map(str, args)],
                      "timeout_s": timeout, "json": True}

    if "http" in selected:
        add("http-server", "web1", lifetime + 3, "http-server", "--duration", lifetime)
        add("http", CLIENTS["http"][0], duration + 3, "http", "--source", CLIENTS["http"][1],
            "--interval", config["http_interval_s"], "--timeout", config["http_timeout_s"],
            "--duration", duration)
    if "udp" in selected:
        udp_options = ("--stream-id", stream, "--source", CLIENTS["udp"][1], "--port", config["udp_port"])
        add("udp-receiver", "iot1", lifetime + 3, "udp-receive", *udp_options, "--duration", lifetime)
        add("udp-send", CLIENTS["udp"][0], duration + 3, "udp-send", *udp_options, "--duration", duration,
            "--rate-pps", config["udp_rate_pps"], "--payload-size", config["udp_payload_bytes"])
        add("udp-report", None, 15, "udp-report", "--sender", Path(run_dir) / "udp-send.json",
            "--receiver", Path(run_dir) / "udp-receiver.json")
    if "bulk" in selected:
        add("bulk-server", "file1", lifetime + 8, "bulk-server", "--timeout", lifetime + 5)
        add("bulk", CLIENTS["bulk"][0], duration + 13, "bulk", "--source", CLIENTS["bulk"][1],
            "--duration", duration, "--target-bps", config["bulk_target_bps"])
    return plan


def run_directory(parent):
    parent = Path(parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(tempfile.mkdtemp(prefix="issue13-" + stamp + "-", dir=str(parent)))


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class Processes:
    """Keep launch deadlines and own only the subprocesses started by this run."""

    def __init__(self, directory, hosts=None):
        self.directory = Path(directory)
        self.hosts = hosts if hosts is not None else {}
        self.children, self.deadlines, self.outcomes = {}, {}, {}
        self.stdout, self.stderr = {}, {}
        self.started = {}
        self.stopping, self.cleaned = {}, set()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        for label in self.children:
            if label not in self.cleaned and label not in self.outcomes:
                try:
                    if self.exited(label):
                        self.reap(label, "stopped")
                    else:
                        self.begin_stop(label, "stopped", time.monotonic())
                except (OSError, subprocess.SubprocessError) as exc:
                    self.complete(label, "cleanup_error", None, error=str(exc))
        self.finish_many(self.children)
        failures = [name for name in self.children if name not in self.cleaned]
        if failures:
            raise RuntimeError("process cleanup failed: " + ", ".join(failures))

    def record(self, **event):
        with (self.directory / "commands.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, allow_nan=False) + "\n")

    def start(self, label, job, env=None):
        if label in self.started:
            raise ValueError("duplicate command label: " + label)
        started, row = time.time_ns(), time.monotonic()
        self.started[label] = started
        self.deadlines[label] = row + job["timeout_s"]
        self.stdout[label] = self.directory / (label + (".json" if job.get("json") else ".stdout.txt"))
        self.stderr[label] = self.directory / (label + ".stderr.txt")
        group = os.name == "posix"
        params = {"cwd": str(ROOT), "env": env, "stdin": subprocess.DEVNULL}
        options = {}
        host = job["host"]
        launch = subprocess.Popen
        if group:
            options["start_new_session"] = True
        if host is not None:
            node = self.hosts[host]
            launch = node.popen
            # Python creates the process group. Avoid mnexec -d because it can fork
            # when the child is already the group leader.
            options["mncmd"] = ["mnexec", "-a", str(node.pid)]
        self.record(event="start", label=label, host=host, argv=job["argv"],
                    timeout_s=job["timeout_s"], started_at_unix_ns=started)
        try:
            with self.stdout[label].open("wb") as stdout, self.stderr[label].open("wb") as stderr:
                self.children[label] = launch(job["argv"], stdout=stdout, stderr=stderr,
                                              **params, **options)
        except OSError as exc:
            self.complete(label, "launch_error", None, error=str(exc))
            raise

    def complete(self, label, status, code, **extra):
        result = {"label": label, "status": status, "returncode": code,
                  "ended_at_unix_ns": time.time_ns(), **extra}
        self.outcomes[label] = result
        self.record(event="finish", **result)
        return result

    @staticmethod
    def terminate(process, force=False):
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            (process.kill if force else process.terminate)()

    def exited(self, label):
        process = self.children[label]
        if os.name == "posix":
            # Keep the leader PID reserved until the owned group is signaled.
            # poll()/wait() would reap the leader and allow PID/PGID reuse.
            return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        return process.poll() is not None

    def reap(self, label, status=None):
        process = self.children[label]
        if os.name == "posix":
            self.terminate(process, force=True)
        code = process.wait(timeout=0)
        self.cleaned.add(label)
        return self.complete(label, status or ("ok" if code == 0 else "nonzero"), code)

    def begin_stop(self, label, status, now):
        if label in self.stopping or label in self.cleaned:
            return
        self.terminate(self.children[label])
        self.stopping[label] = (status, now + TERM_GRACE_S, False)

    def tick(self):
        # No per-process grace wait here. Every process can reach its own
        # deadline while another process is still handling a signal.
        for label in self.children:
            if label in self.outcomes:
                continue
            now = time.monotonic()
            try:
                done = self.exited(label)
                now = time.monotonic()
                if label not in self.stopping and now >= self.deadlines[label]:
                    if done:
                        self.reap(label, "timeout")
                        continue
                    self.begin_stop(label, "timeout", now)
                if done:
                    stop = self.stopping.get(label)
                    self.reap(label, stop[0] if stop else None)
                elif label in self.stopping:
                    status, limit, forced = self.stopping[label]
                    if now >= limit and not forced:
                        self.terminate(self.children[label], force=True)
                        self.stopping[label] = (status, time.monotonic() + KILL_GRACE_S, True)
                    elif now >= limit:
                        self.complete(label, "cleanup_error", None, deadline_status=status)
            except (OSError, subprocess.SubprocessError) as exc:
                self.complete(label, "cleanup_error", None, error=str(exc))

    def stop(self, label, status):
        if label not in self.outcomes:
            if self.exited(label):
                return self.reap(label, status)
            self.begin_stop(label, status, time.monotonic())
            self.finish_many((label,))
        return self.outcomes[label]

    def finish(self, label):
        self.finish_many((label,))
        return self.outcomes[label]

    def finish_many(self, labels):
        labels = set(labels)
        while labels - self.outcomes.keys():
            self.tick()
            if labels - self.outcomes.keys():
                time.sleep(POLL_S)

    def wait_ready(self, label, ready, timeout):
        deadline = min(time.monotonic() + timeout, self.deadlines[label])
        while time.monotonic() < deadline:
            self.tick()
            if label in self.outcomes:
                if self.outcomes[label]["status"] == "timeout":
                    raise TimeoutError(label + " timed out before becoming ready")
                raise RuntimeError(label + " exited before becoming ready")
            available = ready()
            if time.monotonic() >= deadline:
                break
            self.tick()
            if available and time.monotonic() < deadline and label not in self.outcomes:
                self.record(event="ready", label=label, at_unix_ns=time.time_ns())
                return
            time.sleep(min(POLL_S, max(0, deadline - time.monotonic())))
        self.stop(label, "timeout")
        raise TimeoutError(label + " did not become ready")

    def text(self, label):
        return self.stdout[label].read_text(encoding="utf-8", errors="replace")


def shell_job(argv, host=None, timeout=5):
    return {"host": host, "argv": argv, "timeout_s": timeout}


def run_command(processes, label, argv, host=None, timeout=5):
    processes.start(label, shell_job(argv, host, timeout))
    result = processes.finish(label)
    return result, processes.text(label)


def ready_event(processes, label):
    expected = "udp-receive" if label == "udp-receiver" else label
    for row in processes.stderr[label].read_bytes().splitlines():
        try:
            event = json.loads(row)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(event, dict) and event.get("event") == "ready" and event.get("workload") == expected:
            return True
    return False


def bulk_listener(processes):
    phase = 0

    def listening():
        nonlocal phase
        phase += 1
        outcome, text = run_command(processes, "bulk-readiness-{}".format(phase),
                                    ["ss", "-H", "-lnt", "sport", "=", ":5201"], "file1", 1)
        return outcome["status"] == "ok" and any(
            len(row.split()) > 3 and row.split()[3] in {"10.0.40.20:5201", "[::ffff:10.0.40.20]:5201"}
            for row in text.splitlines())

    return listening


def execute_workloads(processes, config, plan):
    servers = [label for kind, label in (("http", "http-server"), ("udp", "udp-receiver"),
                                        ("bulk", "bulk-server")) if kind in config["workloads"]]
    for label in servers:
        processes.start(label, plan[label])
    readiness_deadline = time.monotonic() + READINESS_S
    for label in servers:
        ready = bulk_listener(processes) if label == "bulk-server" else lambda: ready_event(processes, label)
        processes.wait_ready(label, ready, max(0, readiness_deadline - time.monotonic()))
    clients = ["udp-send" if kind == "udp" else kind for kind in config["workloads"]]
    for label in clients:
        processes.start(label, plan[label])
    processes.finish_many(clients + servers)
    if "udp-send" in clients:
        processes.start("udp-report", plan["udp-report"])
        processes.finish("udp-report")


@contextmanager
def diamond(config, directory, processes, ryu_manager):
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        raise RuntimeError("live runs require root on the intended Linux Mininet host")
    from mininet.link import TCLink
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController
    from network.qos_lab_topology import QoSLabTopo
    from network.qos_runtime import apply, cleanup

    checks = {
        "bridges": ["ovs-vsctl", "--timeout=5", "list-br"],
        "qos": ["ovs-vsctl", "--timeout=5", "--bare", "--no-headings", "--columns=_uuid", "list", "QoS"],
        "queues": ["ovs-vsctl", "--timeout=5", "--bare", "--no-headings", "--columns=_uuid", "list", "Queue"],
        "interfaces": ["ip", "-o", "link", "show"],
        "processes": ["ps", "-eo", "comm="],
    }
    for label, argv in checks.items():
        outcome, text = run_command(processes, "preflight-" + label, argv, timeout=8)
        if outcome["status"] != "ok":
            raise RuntimeError("could not verify preflight " + label)
        conflict = bool(text.strip())
        if label == "interfaces":
            conflict = bool(re.search(r"(?m)^\d+:\s+(?:s[1-4]|student1|staff1|mgmt1|web1|iot1|file1)"
                                      r"(?:-eth\d+)?[:@]", text))
        if label == "processes":
            conflict = bool(re.search(r"(?m)^\s*(?:mnexec|ryu-manager|ovs-testcontrol)\s*$", text))
        if conflict:
            raise RuntimeError("conflicting VM resources in preflight " + label)

    port = config["controller_port"]
    port_filter = ["sport", "=", ":" + str(port)]
    outcome, listeners = run_command(processes, "preflight-controller-port",
                                     ["ss", "-H", "-lnt", *port_filter])
    if outcome["status"] != "ok" or listeners.strip():
        raise RuntimeError("could not verify that the controller port was free")
    controller_env = {"SDN_RUN_DIR": str(directory), "SDN_QOS": str(int(config["qos"])),
                      "SDN_ADAPTIVE": str(int(config["adaptive"])), "PYTHONDONTWRITEBYTECODE": "1"}
    controller = [ryu_manager, "--ofp-listen-host", "127.0.0.1", "--ofp-tcp-listen-port", str(port),
                  str(ROOT / "controller" / "ryu_controller.py")]
    processes.record(event="environment", label="controller", variables=controller_env)
    processes.start("controller", shell_job(controller, timeout=config["duration_s"] + 150),
                    env={**os.environ, **controller_env})
    listener_checks = 0

    def controller_ready():
        nonlocal listener_checks
        listener_checks += 1
        outcome, text = run_command(processes, "controller-readiness-{}".format(listener_checks),
                                    ["ss", "-H", "-lntp", *port_filter], timeout=1)
        pid = processes.children["controller"].pid
        return outcome["status"] == "ok" and any(
            "127.0.0.1:" + str(port) in row and re.search(r"\bpid={}\b".format(pid), row)
            for row in text.splitlines())

    processes.wait_ready("controller", controller_ready, READINESS_S)
    net = Mininet(topo=QoSLabTopo(), controller=lambda name: RemoteController(
        name, ip="127.0.0.1", port=port), switch=OVSSwitch, link=TCLink, autoSetMacs=True, build=False)
    # Workload processes use a nested Processes context and exit before the network.
    attempted_qos = False
    try:
        net.build()
        net.start()
        if not net.waitConnected(timeout=15):
            raise RuntimeError("the four switches did not connect to Ryu")
        if config["qos"]:
            attempted_qos = True
            apply(directory.name)
        yield {name: net[name] for name in ("student1", "staff1", "mgmt1", "web1", "iot1", "file1")}
    finally:
        try:
            if attempted_qos:
                cleanup(directory.name)
        finally:
            net.stop()


def snapshots(processes, phase, qos):
    operations = ("dump-ports", "dump-flows", "queue-stats") if qos else ("dump-ports", "dump-flows")
    for switch in SWITCHES:
        for operation in operations:
            label = "{}-{}-{}".format(phase, switch, operation)
            run_command(processes, label, ["ovs-ofctl", "-O", "OpenFlow13", operation, switch])


def probes(processes, kinds):
    for kind in kinds:
        host, source, destination = CLIENTS[kind]
        run_command(processes, kind + "-ping", ["env", "LC_ALL=C", "ping", "-n", "-c", "3",
                    "-i", ".2", "-W", "1", "-I", source, destination], host, timeout=5)


def execute(config, directory, ryu_manager):
    plan = workload_plan(config, directory, sys.executable)
    write_json(directory / "configuration.json", config)
    write_json(directory / "plan.json", plan)
    started = time.time_ns()
    issues, statuses = [], []
    infrastructure = Processes(directory)
    runs = None
    try:
        with infrastructure, diamond(config, directory, infrastructure, ryu_manager) as hosts:
            runs = Processes(directory, hosts)
            with runs:
                snapshots(runs, "before", config["qos"])
                try:
                    execute_workloads(runs, config, plan)
                finally:
                    snapshots(runs, "after", config["qos"])
                probes(runs, config["workloads"])
                infrastructure.tick()
                if "controller" in infrastructure.outcomes:
                    raise RuntimeError("controller exited during experiment")
    except (OSError, RuntimeError, subprocess.SubprocessError, ImportError, KeyboardInterrupt) as exc:
        issues.append("execution: {}".format(str(exc) or type(exc).__name__))
        statuses.append("incomplete" if isinstance(exc, (TimeoutError, KeyboardInterrupt)) else "failed")

    observed_statuses = {**infrastructure.outcomes, **(runs.outcomes if runs else {})}
    summary = metrics.summarize(directory, config["workloads"], process_outcomes=observed_statuses)
    issues.extend(summary["issues"])
    telemetry = metrics.controller_stats(directory)
    for label in ("http-server", "bulk-server"):
        if label not in plan:
            continue
        kind = label.split("-")[0]
        try:
            server = metrics.artifact(directory, label)
            outcome = observed_statuses.get(label)
            if server is None or outcome is None:
                raise ValueError("service result missing")
            status = metrics.exit_status(server, outcome)
            if status != "ok":
                raise ValueError("service status " + status)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            result = summary["workloads"][kind]
            result["status"] = metrics.combined_status(("incomplete", result["status"]))
            result["issues"].append(label + ".json: " + str(exc))
    for label, outcome in observed_statuses.items():
        if (label.startswith(("before-", "after-")) and outcome["status"] != "ok"):
            statuses.append("incomplete")
            issues.append(label + " OVS snapshot did not complete successfully")
        if outcome["status"] == "cleanup_error":
            statuses.append("failed")
            issues.append(label + " process group cleanup could not be verified")
    if any(entry["issues"] or not entry["observed"]["rows"] for entry in telemetry.values()):
        statuses.append("incomplete")
        issues.append("Ryu port or flow statistics are missing, empty or malformed")
    statuses.extend(result["status"] for result in summary["workloads"].values())
    summary.update(status=metrics.combined_status(statuses), issues=issues,
                   started_at_unix_ns=started, ended_at_unix_ns=time.time_ns(),
                   controller_statistics=telemetry, processes=observed_statuses)
    write_json(directory / "summary.json", summary)
    return summary


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="start a fresh Mininet run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--ryu-manager", required=True)
    replay = commands.add_parser("summarize", help="recalculate metrics from saved workload artifacts")
    replay.add_argument("directory", type=Path)
    replay.add_argument("--workloads", nargs="+", choices=sorted(CLIENTS), required=True)
    replay.add_argument("--controller-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "summarize":
            config = configuration({"workloads": args.workloads})
            result = metrics.summarize(args.directory, config["workloads"])
            if args.controller_dir:
                result["controller_statistics"] = metrics.controller_stats(args.controller_dir)
                if any(item["issues"] or not item["observed"]["rows"]
                       for item in result["controller_statistics"].values()):
                    result["status"] = metrics.combined_status((result["status"], "incomplete"))
            print(json.dumps(result, indent=2, allow_nan=False))
        else:
            config = configuration(workloads.decode_json(args.config.read_text(encoding="utf-8")))
            directory = run_directory(args.output_root)
            print(str(directory), flush=True)
            result = execute(config, directory, args.ryu_manager)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return EXIT_STATUS[result["status"]]


if __name__ == "__main__":
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, interrupt)
    sys.exit(main())
