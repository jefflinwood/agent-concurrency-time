import argparse
import json
from pathlib import Path
import statistics

from .runner import Run, digest, load_config, provenance, schedule


def measurements(path):
    events_path = path.parent / "events.jsonl"
    if not events_path.exists():
        return {}
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    agents = [e for e in events if e["event"] == "agent_finished"]
    usage_records = []
    for event in agents:
        for line in (path.parent / Path(event["stdout"]).name).read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "turn.completed" and "usage" in record:
                usage_records.append(dict(role=event["role"], task_id=event["task_id"], usage=record["usage"]))
    return dict(
        implementation_invocations=sum(e["role"] == "implementation" for e in agents),
        integration_invocations=sum(e["role"] == "integration" for e in agents),
        implementation_agent_seconds=sum(e["seconds"] for e in agents if e["role"] == "implementation"),
        integration_agent_seconds=sum(e["seconds"] for e in agents if e["role"] == "integration"),
        failed_agent_invocations=sum(e["returncode"] != 0 or e["timed_out"] for e in agents),
        failed_validations=sum(e["returncode"] != 0 or e["timed_out"] for e in events if e["event"] == "validation"),
        conflicted_merges=sum(bool(e["conflict_files"]) for e in events if e["event"] == "merge_attempt"),
        conflict_file_occurrences=sum(len(e["conflict_files"]) for e in events if e["event"] == "merge_attempt"),
        integration_edit_file_occurrences=sum(len(e["changed_files"]) for e in events if e["event"] == "agent_edits" and e["role"] == "integration"),
        usage_records=usage_records,
    )


def report(root):
    records = []
    for path in sorted(Path(root).glob("*/summary.json")):
        record = json.loads(path.read_text())
        def read_artifact(name):
            artifact = path.parent / name
            return json.loads(artifact.read_text()) if artifact.exists() else {}
        recorded = provenance(read_artifact("config.json"), read_artifact("metadata.json"))
        record["provenance"] = {**recorded, **record.get("provenance", {})}
        record["measurements"] = measurements(path)
        records.append(record)
    groups = []
    for n in sorted({r["agents"] for r in records}):
        runs = [r for r in records if r["agents"] == n]
        times = [r["work_seconds"] for r in runs if r["status"] == "passed"]
        groups.append(dict(agents=n, runs=len(runs), successes=len(times),
                           failures=sum(r["status"] != "passed" for r in runs),
                           successful_work_seconds=times,
                           median_successful_work_seconds=statistics.median(times) if times else None))
    variants = []
    for record in records:
        if record["provenance"] not in variants:
            variants.append(record["provenance"])
    return dict(provenance=variants, groups=groups, runs=records)


def main():
    parser = argparse.ArgumentParser(description="Local agent concurrency benchmark")
    parser.add_argument("command", choices=["plan", "run", "report"])
    parser.add_argument("--config", default="benchmark/config.json")
    parser.add_argument("--output", default="results")
    parser.add_argument("--run-index", type=int, help="Execute just this zero-based scheduled run (pilot or shard)")
    args = parser.parse_args()
    if args.command == "report":
        print(json.dumps(report(args.output), indent=2))
        return
    config, tasks = load_config(args.config)
    plan = schedule(config)
    if args.command == "plan":
        print(json.dumps(dict(config=config, schedule=plan, tasks=tasks), indent=2))
        return
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    asset_hashes = {key: digest(config[key]) for key in ("tasks", "acceptance", "constraints")}
    manifest = dict(config=config, schedule=plan, tasks=tasks, asset_sha256=asset_hashes)
    path = root / "experiment.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        parser.error("output directory already contains a different experiment")
    path.write_text(json.dumps(manifest, indent=2))
    if args.run_index is not None and not 0 <= args.run_index < len(plan):
        parser.error("run-index out of bounds")
    selected = enumerate(plan) if args.run_index is None else [(args.run_index, plan[args.run_index])]
    failed = False
    for index, entry in selected:
        if any(digest(config[key]) != expected for key, expected in asset_hashes.items()):
            parser.error("benchmark inputs changed during the experiment; start a new experiment")
        destination = root / f"{index:02}-trial-{entry['trial']}-agents-{entry['agents']}"
        # Never silently overwrite or resume a failed trial.
        if destination.exists():
            parser.error(f"run already exists: {destination}; use a new output directory")
        print(f"Starting {destination.name}", flush=True)
        result = Run(destination, config, tasks, entry["agents"], entry["trial"]).run()
        failed |= result["status"] != "passed"
        print(json.dumps(result), flush=True)
    (root / "report.json").write_text(json.dumps(report(root), indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
