"""Run the paired planning benchmark or replay recorded traces offline."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from datetime import date
from pathlib import Path
from time import perf_counter

from travel_agent.domain import ProviderError, TripRequest
from travel_agent.planner import advance
from travel_agent.settings import load_settings

from .cases import CORPUS_START, CORPUS_VERSION, SCENARIOS, TravelCase, build_cases
from .exchanges import Recorder, ReplayMismatch, Replayer, ToolSnapshot, canonical, digest, serialized_error, snapshot
from .metrics import assess, summarize


STRATEGIES = ("fixed", "tools", "verified")
PROFILES = ("clean", "timeout")
STAGES = ("research", "draft", "validate")
SCHEMA_VERSION = 2
MAX_STAGE_ATTEMPTS = 3


def _execute(request: TripRequest, *, mode: str, strategy: str, exchange) -> dict:
    checkpoint: dict = {}
    stages = []
    for stage in STAGES:
        for attempt in range(1, MAX_STAGE_ATTEMPTS + 1):
            stage_result = {"stage": stage, "attempt": attempt}
            stages.append(stage_result)
            try:
                # Failed attempts cannot mutate the last committed checkpoint.
                next_checkpoint = advance(stage, request, snapshot(checkpoint), mode=mode, strategy=strategy, exchange=exchange)
            except ProviderError as error:
                stage_result["error"] = serialized_error(error)
                if not error.retryable or attempt == MAX_STAGE_ATTEMPTS:
                    return {"outcome": "failed", "stages": stages, "error": stage_result["error"]}
            else:
                checkpoint = snapshot(next_checkpoint)
                stage_result["checkpoint_sha256"] = digest(checkpoint)
                break
    itinerary = checkpoint["itinerary"]
    return {"outcome": "succeeded", "stages": stages, "itinerary": itinerary, "itinerary_sha256": digest(itinerary)}


def replay_trace(trace: dict) -> dict:
    replay = Replayer(trace["operations"])
    if trace["schema_version"] != SCHEMA_VERSION:
        raise ReplayMismatch(f"Trace schema {trace['schema_version']} differs from the current schema {SCHEMA_VERSION}")
    result = _execute(
        TripRequest.model_validate(trace["request"]), mode=trace["mode"], strategy=trace["strategy"], exchange=replay,
    )
    replay.finish()
    if canonical(result["stages"]) != canonical(trace["result"]["stages"]):
        raise ReplayMismatch("Committed checkpoint or stage outcome differs")
    if canonical(result) != canonical(trace["result"]):
        raise ReplayMismatch("Final itinerary or execution outcome differs")
    return result


def benchmark_case(case: TravelCase, *, mode: str, strategy: str, profile: str, tool_snapshot: ToolSnapshot) -> tuple[dict, dict]:
    request = TripRequest.model_validate(case.request)
    recorder = Recorder(profile, tool_snapshot)
    start = perf_counter()
    result = _execute(request, mode=mode, strategy=strategy, exchange=recorder)
    latency_ms = (perf_counter() - start) * 1_000
    trace = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case.case_id,
        "scenario": case.scenario,
        "request": request.payload(),
        "mode": mode,
        "strategy": strategy,
        "profile": profile,
        "operations": recorder.operations,
        "result": result,
    }
    # Use the serialized representation so inline checks exercise file replay semantics.
    try:
        replay_trace(snapshot(trace))
        replay_error = None
    except ReplayMismatch as error:
        replay_error = str(error)
    metrics = {
        "case_id": case.case_id,
        "scenario": case.scenario,
        "strategy": strategy,
        "profile": profile,
        "outcome": result["outcome"],
        "error": result.get("error"),
        "latency_ms": round(latency_ms, 2),
        "stage_retries": sum(stage["attempt"] > 1 for stage in result["stages"]),
        "injected_faults": recorder.injected_faults,
        "replay_matches": replay_error is None,
        "replay_error": replay_error,
        "live_model_invocations": recorder.model_invocations if mode == "live" else 0,
        "live_model_responses": recorder.model_responses if mode == "live" else 0,
        "fixture_model_invocations": recorder.model_invocations if mode == "fixture" else 0,
        "quality": (
            assess(trace["request"], result["itinerary"], recorder.operations)
            if result["outcome"] == "succeeded" else None
        ),
    }
    return metrics, trace


def _matrix(cases: list[TravelCase], args: argparse.Namespace, tools: ToolSnapshot) -> Iterator[tuple[dict, dict]]:
    for case in cases:
        for strategy in args.strategies:
            for profile in args.profiles:
                yield benchmark_case(case, mode=args.mode, strategy=strategy, profile=profile, tool_snapshot=tools)


def _read_traces(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"Invalid recording at {path}:{line_number}") from error


def _replay_all(traces: Iterable[dict]) -> dict:
    outcomes = []
    for trace in traces:
        error_message = None
        try:
            replay_trace(trace)
        except ReplayMismatch as error:
            error_message = str(error)
        outcomes.append({
            "case_id": trace["case_id"], "strategy": trace["strategy"],
            "profile": trace["profile"], "error": error_message,
        })
    return {"executions": len(outcomes), "replay_matches": sum(item["error"] is None for item in outcomes), "runs": outcomes}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--strategies", choices=STRATEGIES, nargs="+", default=STRATEGIES)
    parser.add_argument("--profiles", choices=PROFILES, nargs="+", default=PROFILES)
    parser.add_argument("--scenarios", choices=SCENARIOS, nargs="+")
    parser.add_argument("--start-date", type=date.fromisoformat, default=CORPUS_START)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    trace_mode = parser.add_mutually_exclusive_group()
    trace_mode.add_argument("--trace-dir", type=Path, help="Write an NDJSON recording to this directory")
    trace_mode.add_argument("--replay-dir", type=Path, help="Read an NDJSON recording from this directory")
    parser.add_argument("--tool-snapshot-in", type=Path)
    parser.add_argument("--tool-snapshot-out", type=Path)
    parser.add_argument("--strict-tools", action="store_true", help="Require all requested tools in the input snapshot")
    parser.add_argument("--require-success", action="store_true", help="Exit nonzero if any execution fails")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.strict_tools and args.tool_snapshot_in is None:
        parser.error("--strict-tools requires --tool-snapshot-in")
    if len(args.strategies) != len(set(args.strategies)) or len(args.profiles) != len(set(args.profiles)):
        parser.error("Select each strategy or profile at most once")
    trace_dir = args.trace_dir or args.replay_dir
    if trace_dir:
        trace_path = (trace_dir / "traces.jsonl").resolve()
        if any(path is not None and path.resolve() == trace_path for path in (args.output, args.tool_snapshot_out)):
            parser.error("Output paths must differ from the recording file")
    return args


def _benchmark(args: argparse.Namespace) -> dict:
    cases = [case for case in build_cases(args.start_date) if args.scenarios is None or case.scenario in args.scenarios]
    cases = cases[:args.limit]
    tool_entries = ()
    if args.tool_snapshot_in is not None:
        recorded = json.loads(args.tool_snapshot_in.read_text(encoding="utf-8"))
        if recorded["schema_version"] != SCHEMA_VERSION or recorded["mode"] != args.mode:
            raise ValueError("Tool snapshot schema or source mode differs from this benchmark")
        tool_entries = recorded["responses"]
    tools = ToolSnapshot(tool_entries, strict=args.strict_tools)
    if args.trace_dir is None:
        metrics = [entry for entry, _ in _matrix(cases, args, tools)]
    else:
        args.trace_dir.mkdir(parents=True, exist_ok=True)
        metrics = []
        with (args.trace_dir / "traces.jsonl").open("w", encoding="utf-8") as output:
            for entry, trace in _matrix(cases, args, tools):
                metrics.append(entry)
                output.write(canonical(trace) + "\n")
                output.flush()
    if args.tool_snapshot_out is not None:
        _write_json(args.tool_snapshot_out, {"schema_version": SCHEMA_VERSION, "mode": args.mode, "responses": tools.entries()})
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": args.mode,
        "corpus": {"version": CORPUS_VERSION, "cases": len(cases), "sha256": digest([asdict(case) for case in cases])},
        "totals": summarize(metrics),
        "by_strategy": {name: summarize([item for item in metrics if item["strategy"] == name]) for name in args.strategies},
        "by_profile": {name: summarize([item for item in metrics if item["profile"] == name]) for name in args.profiles},
        "by_strategy_and_profile": {
            name: {
                profile: summarize([item for item in metrics if item["strategy"] == name and item["profile"] == profile])
                for profile in args.profiles
            }
            for name in args.strategies
        },
        "runs": metrics,
    }


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    try:
        if args.replay_dir is not None:
            report = _replay_all(_read_traces(args.replay_dir / "traces.jsonl"))
            totals = {key: report[key] for key in ("executions", "replay_matches")}
        else:
            if args.mode == "live":
                load_settings()
            report = _benchmark(args)
            totals = report["totals"]
        if args.output is not None:
            _write_json(args.output, report)
    except (OSError, ValueError, ReplayMismatch) as error:
        print(f"Evaluation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(totals, ensure_ascii=False, indent=2))
    complete_replay = totals["executions"] > 0 and totals["replay_matches"] == totals["executions"]
    successful = not args.require_success or totals.get("succeeded", totals["executions"]) == totals["executions"]
    return 0 if complete_replay and successful else 1


if __name__ == "__main__":
    sys.exit(main())
