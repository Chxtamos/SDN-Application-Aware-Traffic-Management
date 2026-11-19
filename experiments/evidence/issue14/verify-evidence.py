
from collections import Counter
import csv
import io
import json
import math
from pathlib import Path
import re
from statistics import mean
import sys
from zipfile import ZipFile

class ExtractedEvidence:
    def __init__(self, root):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, name):
        return (self.root / name).read_bytes()

    def namelist(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())


archive=Path(sys.argv[1])
with (ExtractedEvidence(archive) if archive.is_dir() else ZipFile(archive)) as z:
    load=lambda name: json.loads(z.read(name))
    result=load("attempt.json")
    assert result["status"]=="pass" and result["source_before"] and result["source_after"]
    assert result["regression"]["ok"] and result["regression"]["tests"]>=88
    assert all(not result["regression"][k] for k in ("failures","errors","skipped"))
    assert result["postflight_clear"] and result["teammate_files_unchanged"]
    assert load("teammate-integrity.json")["unchanged"]
    assert all(row["exclusive"] for row in load("exclusive-access-watch.json"))
    ns=lambda name:{line.split()[0] for line in load(name)["checks"]["netns"][1:] if line.split()}
    assert ns("preflight.json")==ns("postflight.json"), "unexpected network namespace after run"
    suite_name=next(name for name in z.namelist() if name.endswith("/suite-result.json"))
    suite_prefix=suite_name.rsplit("/",1)[0]+"/"
    suite=load(suite_name)
    scenarios=suite["scenarios"]
    assert suite["status"]=="pass" and suite["source_unchanged"]
    assert [s["scenario"] for s in scenarios]==["fixed-a","adaptive","fixed-a-repeat"]
    assert len(set(row["directory"] for row in scenarios))==3
    cfg=lambda row:load(suite_prefix+row["directory"]+"/configuration.json")
    assert cfg(scenarios[0])==cfg(scenarios[2])
    policy=load(suite_prefix+"suite-config.json")["controller_policy"]
    assert policy==dict(busy_persist=3,congest_hi=80.0,congest_lo=45.0,cool_down=10.0,sample_dt=1.0)
    problems=[]
    streams=set()
    def queues(text):
        return {(int(p),int(q)):int(v) for p,q,v in re.findall(
            r"(?m)^\s*port (\d+) queue (\d+): [^\n]*pkts=(\d+)",text)}
    def drops(text):
        counts={}
        for line in text.splitlines():
            if not ("priority=300" in line and "nw_src=10.0.10.1" in line
                    and "in_port=1" in line and line.endswith("actions=drop")):
                continue
            for role,endpoint,endpoint_port in (("tcp","10.0.40.20","22"),("udp","10.0.40.30","5001")):
                if ","+role+"," in line and "nw_dst="+endpoint+"," in line and "tp_dst="+endpoint_port+" " in line:
                    counts[role]=int(re.search(r"n_packets=(\d+)",line)[1])
        return counts

    metrics=[]
    text=lambda name: z.read(name).decode("utf-8",errors="replace")
    for row in scenarios:
        p=suite_prefix+row["directory"]+"/"
        get=lambda name:load(p+name)
        adaptive=row["scenario"]=="adaptive"
        assert row["status"]=="pass" and row["cleanup"]=="verified"
        summary=get("summary.json")
        assert summary["status"]=="ok"
        assert set(summary["controller_statistics"]["port"]["observed"]["datapath_ids"])=={1,2,3,4}
        assert set(summary["controller_statistics"]["flow"]["observed"]["datapath_ids"])==(
            {1,2,3,4} if adaptive else {1,2,4})
        events=get("path-events.json")
        assert [(r["from"],r["to"]) for r in events]==([("A","B"),("B","A")] if adaptive else [])
        actual_log=text(p+"controller.stderr.txt")
        assert [tuple(e) for e in re.findall(r"ADAPTIVE: switch ([AB]) -> ([AB])",actual_log)]==[
            (e["from"],e["to"]) for e in events]
        port_rows=list(csv.DictReader(io.StringIO(text(p+"sdn_stats.csv"))))
        assert {item["path"] for item in port_rows}==({"A","B"} if adaptive else {"A"})
        for turn in events:
            if turn["from"]=="A":
                assert turn["util_a_pct_rounded"]>=80 and turn["util_b_pct_rounded"]<=turn["util_a_pct_rounded"]
            else:
                assert turn["util_b_pct_rounded"]<=45 and turn["util_a_pct_rounded"]<=80

        phases=get("checkpoints.json")
        before_counts=drops(text(p+"before-s1-dump-flows.stdout.txt"))
        current_drops=[]
        for phase in (phases["initial"],phases["active"],phases["after"]):
            after_counts=drops(text(p+phase+"-s1-dump-flows.stdout.txt"))
            assert all(after_counts[proto]>before_counts.get(proto,0) for proto in ("tcp","udp"))
            current_drops.append(after_counts)
            before_counts=after_counts
        port=5 if adaptive else 4
        queue_deltas={}
        for switch in ("s1","s4"):
            initial=queues(text(p+phases["initial"]+"-"+switch+"-queue-stats.stdout.txt"))
            active=queues(text(p+phases["active"]+"-"+switch+"-queue-stats.stdout.txt"))
            delta={q:active[port,q]-initial[port,q] for q in (1,2,3)}
            assert all(delta[q]>0 for q in ((1,2,3) if switch=="s1" else (1,3)))
            flow=text(p+phases["active"]+"-"+switch+"-dump-flows.stdout.txt")
            assert all("set_queue:{},output:{}".format(q,port) in flow for q in (1,2,3))
            queue_deltas[switch]=delta

        windows=get("acl-windows.json")
        probes={}
        for role in ("student","staff"):
            prefix=text(p+"acl-"+role+".stdout.txt").rpartition("\n")[0]
            probes[role]=[json.loads(line) for line in prefix.splitlines()]
        assert all(q["status"]=="timeout" for q in probes["student"])
        for start,end in windows.values():
            for role,status in (("student","timeout"),("staff","reply")):
                assert {q["protocol"] for q in probes[role] if q["status"]==status
                        and start<=q["started_ns"]<=q["finished_ns"]<=end}=={"tcp","udp"}

        http=[sample for sample in get("http.json")["observed"]["requests"]
              if sample["error"] is None and sample["body_complete"]]
        sender=get("udp-send.json")
        receiver=get("udp-receiver.json")
        streams.add(sender["config"]["stream_id"])
        sent=sender["observed"]
        lookup={entry["sequence"]:entry for entry in sent["packets"]}
        received={}
        for match in receiver["observed"]["packets"]:
            seq=match["sequence"]
            if seq not in lookup or match["source_port"]!=sent["source_port"]:
                continue
            record=lookup[seq]
            assert record["payload_bytes"]==match["payload_bytes"]
            assert match["at_monotonic_ns"]>=record["at_monotonic_ns"]
            received[seq]=min(received.get(seq,match["at_unix_ns"]),match["at_unix_ns"])
        loss=len(lookup)-len(received)
        elapsed=sent["window_elapsed_s"]
        assert sent["scheduled_packets"]==sent["attempted_packets"]+sent["skipped_intervals"]
        assert sent["attempted_packets"]==len(lookup)+sent["send_failure_count"]
        udp=summary["workloads"]["udp"]["calculated"]
        assert udp["udp_packets_lost"]==loss
        assert math.isclose(udp["packet_loss_pct"],loss*100/len(lookup))
        assert math.isclose(udp["udp_send_pps"],len(lookup)/elapsed)
        tcp=get("bulk.json")["observed"]["iperf3_json"]
        mbps=tcp["end"]["sum_received"]["bits_per_second"]/1e6
        assert math.isclose(summary["workloads"]["bulk"]["reported"]["tcp_receiver_bps"],mbps*1e6)
        start,eof=(row["workload_overlap"][k] for k in ("started_ns","ended_ns"))
        overlap=(eof-start)/1e9
        assert overlap>0
        assert any(start<=entry["ended_at_unix_ns"]<=eof for entry in http)
        assert any(start<=value<=eof for value in received.values())
        iperf_start=tcp["start"]["timestamp"]["timesecs"]
        assert any(i["sum"]["bytes"]>0 and iperf_start+i["sum"]["end"]>start/1e9
                   and iperf_start+i["sum"]["start"]+1<eof/1e9 for i in tcp["intervals"])
        response=mean(entry["response_time_ms"] for entry in http)
        assert math.isclose(summary["workloads"]["http"]["calculated"]["http_response_time_ms"]["mean"],response)
        metrics.append({"scenario":row["scenario"],"events":events,"http_requests":len(http),
                        "http_mean_ms":response,"udp_pps":udp["udp_send_pps"],"udp_lost":loss,
                        "udp_sent":len(lookup),"tcp_mbps":mbps,"overlap_s":overlap,
                        "active_queue_deltas":queue_deltas,"acl_windows":len(windows),
                        "acl_probe_counts":{role:dict(Counter(q["status"] for q in rows))
                                            for role,rows in probes.items()},
                        "final_drop_counts":current_drops[-1]})
    assert len(streams)==3
    print(json.dumps({"origin":"verified fresh Issue 14 VM evidence",
                      "input_zip_sha256":result["input_zip_sha256"],
                      "linux":result["regression"],"scenarios":metrics},indent=2))
