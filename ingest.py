"""Ingest one ML-agent run dir into memory/routing/.

For each run we drop a structured JSON record under
``memory/routing/runs/<model>/<task>/<timestamp>.json`` and append a
flat row to ``memory/routing/index.jsonl``. No LLM calls — purely
mechanical aggregation of `manager_state_detailed.json`,
`manager_state.json`, and `mlebench_grade.json`.

Usage:
    python -m memory.routing.ingest <run_dir>
    python -m memory.routing.ingest --task <task_name>             # all runs under a task
    python -m memory.routing.ingest --all                          # everything under LOG_ROOT
    python -m memory.routing.ingest --log-root <path> --all
    python -m memory.routing.ingest --model-default <model_id> ... # label rows with no state.model

See memory/routing/README.md for the JSON schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MEMORY_ROOT = Path(__file__).resolve().parent.parent
ROUTING_ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROUTING_ROOT / "runs"
INDEX_PATH = ROUTING_ROOT / "index.jsonl"
SCHEMA_VERSION = 1

DEFAULT_LOG_ROOT = Path("/data5/ruiyi/mlebench-playground/code")
LOG_ROOT = Path(os.environ.get("ML_AGENT_LOG_ROOT", str(DEFAULT_LOG_ROOT)))

# Roles we want a per-role cost breakdown for. Must align with
# utils.schemas._ROLE_META; duplicated as a literal here to avoid
# importing the live module from a memory utility script.
KNOWN_ROLES = ("manager", "setup", "designer", "reviser", "coder", "tuner", "aggregator")


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _empty_role_bucket() -> dict:
    return {
        "calls": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "turns": 0,
        "errors": 0,
    }


def _aggregate_costs(detailed: dict | None) -> dict:
    """Walk history[*].messages[*].cost and the manager_cost block."""
    per_role = {r: _empty_role_bucket() for r in KNOWN_ROLES}
    totals = {
        "total_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_turns": 0,
        "errors_count": 0,
        "per_role": per_role,
    }
    if not detailed:
        return totals

    for entry in detailed.get("history", []) or []:
        role = entry.get("role") or "manager"
        bucket = per_role.setdefault(role, _empty_role_bucket())
        bucket["calls"] += len(entry.get("messages", []) or [])
        for msg in entry.get("messages", []) or []:
            cost = msg.get("cost") or {}
            bucket["cost_usd"] += float(cost.get("cost_usd") or 0.0)
            bucket["input_tokens"] += int(cost.get("input_tokens") or 0)
            bucket["output_tokens"] += int(cost.get("output_tokens") or 0)
            bucket["cache_read_input_tokens"] += int(cost.get("cache_read_input_tokens") or 0)
            bucket["cache_creation_input_tokens"] += int(cost.get("cache_creation_input_tokens") or 0)
            bucket["turns"] += int(cost.get("turns") or 0)

    mc = detailed.get("manager_cost") or {}
    if mc:
        bucket = per_role["manager"]
        bucket["calls"] += 1
        bucket["cost_usd"] += float(mc.get("cost_usd") or 0.0)
        bucket["input_tokens"] += int(mc.get("input_tokens") or 0)
        bucket["output_tokens"] += int(mc.get("output_tokens") or 0)
        bucket["cache_read_input_tokens"] += int(mc.get("cache_read_input_tokens") or 0)
        bucket["cache_creation_input_tokens"] += int(mc.get("cache_creation_input_tokens") or 0)
        bucket["turns"] += int(mc.get("turns") or 0)

    for bucket in per_role.values():
        totals["total_usd"] += bucket["cost_usd"]
        totals["total_input_tokens"] += bucket["input_tokens"]
        totals["total_output_tokens"] += bucket["output_tokens"]
        totals["cache_read_input_tokens"] += bucket["cache_read_input_tokens"]
        totals["cache_creation_input_tokens"] += bucket["cache_creation_input_tokens"]
        totals["total_turns"] += bucket["turns"]

    totals["errors_count"] = len(detailed.get("errors") or [])
    return totals


def _extract_result(slim: dict | None, detailed: dict | None, grade: dict | None) -> dict:
    """Best-effort extraction of headline result from whatever files exist."""
    out: dict[str, Any] = {
        "score": None,
        "metric_name": None,
        "metric_direction": None,
        "medal": "ungraded",
        "above_median": None,
        "bronze_threshold": None,
    }

    metrics = (detailed or {}).get("metrics") or (slim or {}).get("metrics") or {}
    # Aggregator (top-level) is conventionally keyed 0; fall back to lowest-number candidate.
    chosen = metrics.get(0) or metrics.get("0")
    if chosen is None and metrics:
        # pick candidate with mlebench_grade if present, else first numeric key
        numeric_keys = sorted(int(k) for k in metrics.keys() if str(k).isdigit())
        for k in numeric_keys:
            cand = metrics.get(k) or metrics.get(str(k))
            if cand and "mlebench_grade" in cand:
                chosen = cand
                break
        if chosen is None and numeric_keys:
            chosen = metrics.get(numeric_keys[0]) or metrics.get(str(numeric_keys[0]))

    if grade is not None:
        out["bronze_threshold"] = grade.get("bronze_threshold")
        out["above_median"] = grade.get("above_median")
        if grade.get("gold_medal"):
            out["medal"] = "gold"
        elif grade.get("silver_medal"):
            out["medal"] = "silver"
        elif grade.get("bronze_medal"):
            out["medal"] = "bronze"
        elif grade.get("score") is not None:
            out["medal"] = "none"
        if grade.get("score") is not None and out["score"] is None:
            out["score"] = grade.get("score")

    if chosen:
        mg = chosen.get("mlebench_grade") or {}
        if mg:
            out["bronze_threshold"] = out["bronze_threshold"] or mg.get("bronze_threshold")
            out["above_median"] = out["above_median"] if out["above_median"] is not None else mg.get("above_median")
            if out["score"] is None:
                out["score"] = mg.get("score")
            if out["medal"] == "ungraded":
                if mg.get("gold_medal"):
                    out["medal"] = "gold"
                elif mg.get("silver_medal"):
                    out["medal"] = "silver"
                elif mg.get("bronze_medal"):
                    out["medal"] = "bronze"
                elif mg.get("score") is not None:
                    out["medal"] = "none"
        # primary metric value when separate from grade
        if out["score"] is None:
            for k in ("score", "primary_metric", "accuracy"):
                if k in chosen:
                    out["score"] = chosen[k]
                    break

    return out


def _extract_pipeline(slim: dict | None, detailed: dict | None) -> dict:
    src = detailed or slim or {}
    started = (detailed or slim or {}).get("run_started_at_unix") or 0
    wall = None
    if started and started > 0:
        wall = max(0, int(time.time() - started))  # only meaningful when run has ended; backfill estimate

    return {
        "num_candidates": src.get("num_candidates") or src.get("num_designs"),
        "alive_at_end": src.get("alive_nums"),
        "agent_calls": src.get("agent_calls"),
        "max_agent_calls": src.get("max_agent_calls"),
        "tuner_runs": src.get("tuner_runs"),
        "reviser_runs": src.get("reviser_runs"),
        "consumed_seconds": src.get("consumed_seconds"),
        "wall_elapsed_seconds": src.get("wall_elapsed_seconds") or wall,
    }


def _model_id(detailed: dict | None, slim: dict | None, default: str | None) -> str:
    for src in (detailed or {}, slim or {}):
        m = src.get("model")
        if m:
            return m
    return default or "unknown"


def _extract_model_role_map(detailed: dict | None, fallback_model: str) -> dict | None:
    """Build {role: most_common_model} by scanning history[*].messages[*].model.

    Returns None when no per-message ``model`` field is present in any
    message (i.e. the run pre-dates Message.model). Otherwise returns a
    dict mapping each non-manager role seen in history to the
    most-frequent non-empty model string among its messages, with
    model="" entries falling back to ``fallback_model``. Manager-issued
    instruction messages always carry model="" by construction, so the
    "manager" role's slot is filled with ``fallback_model`` (which is
    the run-wide model -- equal to state.model and to the manager-agent's
    actual model). This is the data source a future router/bandit reads
    to attribute cost and outcomes per role.
    """
    if not detailed:
        return None
    history = detailed.get("history") or []
    saw_any_model_field = False
    counts: dict[str, dict[str, int]] = {}
    for entry in history:
        role = entry.get("role") or "manager"
        if role == "manager":
            # Manager-side messages are instructions, not LLM outputs; they
            # always have model="" by convention. Attribute the manager role
            # to the run-wide model below.
            continue
        bucket = counts.setdefault(role, {})
        for msg in entry.get("messages", []) or []:
            if "model" in msg:
                saw_any_model_field = True
            m = (msg.get("model") or "").strip() or fallback_model
            bucket[m] = bucket.get(m, 0) + 1
    if not saw_any_model_field:
        return None
    role_map: dict[str, str] = {}
    for role, bucket in counts.items():
        if not bucket:
            continue
        # most-frequent model wins; ties broken by lexicographic order for
        # determinism across re-ingests of the same run.
        role_map[role] = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    # Manager role: derived from run-wide model (manager LLM call is logged
    # in detailed.manager_cost; its model is state.model, captured here as
    # fallback_model).
    role_map["manager"] = fallback_model
    return role_map


def build_record(run_dir: Path, model_default: str | None, source: str) -> dict:
    detailed = _read_json(run_dir / "manager_state_detailed.json")
    slim = _read_json(run_dir / "manager_state.json")
    grade = _read_json(run_dir / "mlebench_grade.json")

    model = _model_id(detailed, slim, model_default)
    model_role_map = _extract_model_role_map(detailed, fallback_model=model)

    record = {
        "schema_version": SCHEMA_VERSION,
        "task": run_dir.parent.name,
        "timestamp": run_dir.name,
        "run_dir": str(run_dir),
        "model": model,
        "model_role_map": model_role_map,
        "result": _extract_result(slim, detailed, grade),
        "pipeline": _extract_pipeline(slim, detailed),
        "cost": _aggregate_costs(detailed),
        "provenance": {
            "manager_state_sha256": _sha256(run_dir / "manager_state.json"),
            "ingest_unix": int(time.time()),
            "source": source,
        },
    }
    return record


def _output_path(record: dict) -> Path:
    safe_model = record["model"].replace("/", "_")
    return RUNS_DIR / safe_model / record["task"] / f"{record['timestamp']}.json"


def _index_row(record: dict) -> dict:
    cost = record.get("cost") or {}
    pipeline = record.get("pipeline") or {}
    result = record.get("result") or {}
    return {
        "model": record["model"],
        "task": record["task"],
        "timestamp": record["timestamp"],
        "score": result.get("score"),
        "medal": result.get("medal"),
        "bronze_threshold": result.get("bronze_threshold"),
        "above_median": result.get("above_median"),
        "agent_calls": pipeline.get("agent_calls"),
        "wall_s": pipeline.get("wall_elapsed_seconds"),
        "cost_usd": cost.get("total_usd"),
        "errors": cost.get("errors_count"),
        "run_dir": record["run_dir"],
    }


def _rewrite_index_excluding(model: str, task: str, timestamp: str) -> None:
    """Drop any prior row that matches (model, task, timestamp) — used on --force."""
    if not INDEX_PATH.exists():
        return
    keep: list[str] = []
    for line in INDEX_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            keep.append(line)
            continue
        if (row.get("model"), row.get("task"), row.get("timestamp")) == (model, task, timestamp):
            continue
        keep.append(line)
    INDEX_PATH.write_text("\n".join(keep) + ("\n" if keep else ""))


def ingest_one(run_dir: Path, model_default: str | None, force: bool, source: str) -> Path | None:
    run_dir = run_dir.resolve()
    if not run_dir.exists():
        print(f"[skip] not a dir: {run_dir}", file=sys.stderr)
        return None

    record = build_record(run_dir, model_default, source=source)
    out = _output_path(record)

    if out.exists() and not force:
        print(f"[skip] {out} exists")
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")

    if force:
        _rewrite_index_excluding(record["model"], record["task"], record["timestamp"])
    with INDEX_PATH.open("a") as f:
        f.write(json.dumps(_index_row(record)) + "\n")

    print(f"[ok] {out}  (model={record['model']})")
    return out


def iter_run_dirs(task_filter: str | None, log_root: Path) -> list[Path]:
    runs: list[Path] = []
    if not log_root.exists():
        return runs
    for task_dir in sorted(log_root.iterdir()):
        if not task_dir.is_dir():
            continue
        if task_filter and task_dir.name != task_filter:
            continue
        for run_dir in sorted(task_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            if (run_dir / "manager_state.json").exists() or (run_dir / "manager_state_detailed.json").exists():
                runs.append(run_dir)
    return runs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", nargs="?", help="single run directory")
    p.add_argument("--task", help="ingest all runs under LOG_ROOT/<task>")
    p.add_argument("--all", action="store_true", help="ingest every run under LOG_ROOT")
    p.add_argument("--log-root", help=f"override LOG_ROOT (default: {LOG_ROOT}; env: ML_AGENT_LOG_ROOT)")
    p.add_argument("--model-default", default=None,
                   help="model id to record when state.model is missing/empty")
    p.add_argument("--force", action="store_true", help="overwrite existing routing JSON + index row")
    p.add_argument("--source", default="cli",
                   help="provenance tag (cli | auto-curate | backfill)")
    p.add_argument("--limit", type=int, help="cap number of runs (testing)")
    args = p.parse_args()

    if not (args.run_dir or args.task or args.all):
        p.error("specify a run_dir, --task, or --all")

    log_root = Path(args.log_root) if args.log_root else LOG_ROOT

    if args.run_dir:
        targets = [Path(args.run_dir)]
    else:
        targets = iter_run_dirs(args.task, log_root=log_root)
        if args.limit:
            targets = targets[: args.limit]

    if not targets:
        print("no runs found", file=sys.stderr)
        return 1

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"ingesting {len(targets)} run(s) into {ROUTING_ROOT}")
    for rd in targets:
        try:
            ingest_one(rd, args.model_default, args.force, args.source)
        except Exception as exc:
            print(f"[error] {rd}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
