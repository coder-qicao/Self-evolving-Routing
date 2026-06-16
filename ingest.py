"""Ingest one ML-agent run dir into memory/routing/.

For each run we drop a structured JSON record under
``memory/routing/runs/<model>/<task>/<timestamp>.json`` and append a
flat row to ``memory/routing/index.jsonl``. No LLM calls — purely
mechanical aggregation of `manager_state_detailed.json`,
`manager_state.json`, and `mlebench_grade.json`.

Usage:
    python -m memory.routing.ingest <run_dir>
    python -m memory.routing.ingest --log-root <path> --task <task_name>  # all runs under a task
    python -m memory.routing.ingest --log-root <path> --all               # everything under log-root
    python -m memory.routing.ingest --model-default <model_id> ...        # label rows with no state.model

See memory/routing/README.md for the JSON schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

# Machine-specific path prefixes get scrubbed out of every ingested record so the
# repo's memory/routing/ never carries a personal absolute path (CS58). The
# experiment runs live under a per-developer dir (e.g. /workspace/mlebench-...),
# so without this each ingest would re-introduce a CS58 violation into the tree.
# Mirrors the accepted generic forms: /home/<user> -> ${HOME}, /data<N>/<user>
# -> /workspace. The (?<![\w.]) lookbehind matches CS58's own anchor so URL-ish
# segments aren't touched.
_PERSONAL_DATA_RE = re.compile(r"(?<![\w.])/data\d+/[A-Za-z_][\w.\-]*")
_PERSONAL_HOME_RE = re.compile(r"(?<![\w.])/home/[A-Za-z_][\w.\-]*")


def _scrub_personal_paths(value: Any) -> Any:
    """Recursively replace personal path prefixes in a record's string values
    (/data<N>/<user> -> /workspace, /home/<user> -> ${HOME}); non-strings pass
    through unchanged."""
    if isinstance(value, str):
        value = _PERSONAL_DATA_RE.sub("/workspace", value)
        return _PERSONAL_HOME_RE.sub("${HOME}", value)
    if isinstance(value, dict):
        return {k: _scrub_personal_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_personal_paths(v) for v in value]
    return value

MEMORY_ROOT = Path(__file__).resolve().parent.parent
ROUTING_ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROUTING_ROOT / "runs"
INDEX_PATH = ROUTING_ROOT / "index.jsonl"
SCHEMA_VERSION = 1

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

    # v3 path: cost lives under nodes[<node_id>].units[<role>].cost.total_*.
    # Walked AFTER history so v2 runs that happen to also have a nodes block
    # (none currently exist, but defensive) do not double-count — history
    # buckets are already populated above and this path only adds when the
    # v3 fields are populated. ``nodes`` is a dict keyed by str node_id.
    # ``root`` is the root candidate's node (also referenced by nodes[<id>]),
    # so we skip the duplicate by iterating nodes only.
    nodes = detailed.get("nodes") or {}
    if isinstance(nodes, dict):
        for node in nodes.values():
            for role, unit in (node.get("units") or {}).items():
                c = (unit or {}).get("cost") or {}
                if not c:
                    continue
                bucket = per_role.setdefault(role, _empty_role_bucket())
                bucket["calls"] += 1
                bucket["cost_usd"] += float(c.get("total_cost_usd") or 0.0)
                bucket["input_tokens"] += int(c.get("total_input_tokens") or 0)
                bucket["output_tokens"] += int(c.get("total_output_tokens") or 0)
                bucket["cache_read_input_tokens"] += int(c.get("total_cache_read_input_tokens") or 0)
                bucket["cache_creation_input_tokens"] += int(c.get("total_cache_creation_input_tokens") or 0)
                bucket["turns"] += int(c.get("total_turns") or 0)

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
        elif grade.get("above_median"):
            out["medal"] = "above_median"
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


# Units that are not LLM agents (no model to attribute).
_NON_AGENT_UNITS = frozenset({"trainer", "holdout_inference", "holdout_grader"})


def _unit_model(unit: dict) -> "str | None":
    """The model an agent unit ran on. v3 records it at
    ``units/<role>/steps[]/sdk_records[]/data.model`` (the SDK init message).
    Returns the most-frequent claude model id across the unit's records, or None."""
    counts: dict[str, int] = {}
    for step in (unit.get("steps") or []):
        for rec in (step.get("sdk_records") or []):
            data = rec.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    continue
            m = data.get("model") if isinstance(data, dict) else None
            if isinstance(m, str) and m.strip():
                counts[m.strip()] = counts.get(m.strip(), 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _models_from_units(units: dict) -> dict[str, str]:
    """``{role: model}`` for the agent roles in a v3 ``units`` dict."""
    out: dict[str, str] = {}
    for role, unit in (units or {}).items():
        if role in _NON_AGENT_UNITS or not isinstance(unit, dict):
            continue
        m = _unit_model(unit)
        if m:
            out[role] = m
    return out


def _per_node_models_by_role(units: dict) -> dict[str, str]:
    """Per-role model a node's units actually ran on (v3:
    ``units/<role>/steps/sdk_records/data.model``). Empty when the node's
    units carry no SDK records (e.g. it never ran)."""
    return _models_from_units(units)


def _extract_per_node(
    detailed: dict | None,
    coder_model: str | None,
) -> list[dict]:
    """Per-node breakdown — substrate for per-node routing decisions.

    One row per node in ``detailed.nodes``. Captures depth, parent linkage,
    status, final metric, the coder unit's cost + turn count, and the
    per-role ``models_by_role`` actually used for the node (M3 of the
    per-node-routing spec: lets analysis attribute outcome → per-node
    routing without rerunning anything). Empty ``models_by_role`` ⇒ this
    run pre-dates per-message model capture OR the per-node-routing flag
    was off so every node used the run-level decision; callers should fall
    back to ``model_role_map`` (run-wide per-role) in that case.

    The ``coder_model`` argument is kept for backwards compatibility (older
    consumers read ``coder_model`` directly off the row) and is also
    derived from ``models_by_role["coder"]`` when available.
    """
    if not detailed:
        return []
    nodes = detailed.get("nodes") or {}
    if not isinstance(nodes, dict):
        return []
    out: list[dict] = []
    for nid, n in nodes.items():
        units = n.get("units") or {}
        c = (units.get("coder") or {}).get("cost") or {}
        coder_ran = bool(c.get("total_cost_usd") or c.get("total_turns"))
        trainer = units.get("trainer") or {}
        hi = units.get("holdout_inference") or {}
        hg = units.get("holdout_grader") or {}
        node_row = {
            "node_id": int(nid),
            "depth": n.get("depth"),
            "parent_node_id": n.get("parent_node_id"),
            "status": n.get("status"),
            "metric": n.get("metric"),
            "metric_source": n.get("metric_source") or None,
            "coder_cost_usd": float(c.get("total_cost_usd") or 0.0) if coder_ran else 0.0,
            "coder_turns": int(c.get("total_turns") or 0) if coder_ran else 0,
            "coder_duration_s": float(c.get("total_duration_s") or 0.0) if coder_ran else 0.0,
            "trainer_return_code": trainer.get("return_code"),
            "trainer_duration_s": (
                (trainer.get("ended_at") or 0.0) - (trainer.get("started_at") or 0.0)
                if trainer.get("started_at") else None
            ),
            "holdout_inference_rc": hi.get("return_code"),
            "holdout_grader_rc": hg.get("return_code"),
        }
        per_node_models = _per_node_models_by_role(units)
        node_row["models_by_role"] = per_node_models
        # Backwards-compat: ``coder_model`` is the run-wide coder model
        # by default, but the per-node mapping (when present) is authoritative.
        node_row["coder_model"] = per_node_models.get("coder") or (
            coder_model if coder_ran else None
        )
        out.append(node_row)
    out.sort(key=lambda r: r["node_id"])
    return out


def _extract_pipeline(slim: dict | None, detailed: dict | None) -> dict:
    src = detailed or slim or {}
    started = (detailed or slim or {}).get("run_started_at_unix") or 0
    wall = None
    if started and started > 0:
        wall = max(0, int(time.time() - started))  # only meaningful when run has ended; backfill estimate

    # v3 falls back to nodes dict for num_candidates (one node per candidate
    # under the root). v2 still wins via num_candidates / num_designs.
    nodes = src.get("nodes")
    v3_num_candidates = (len(nodes) if isinstance(nodes, dict) else None)

    return {
        "num_candidates": src.get("num_candidates") or src.get("num_designs") or v3_num_candidates,
        "alive_at_end": src.get("alive_nums"),
        "agent_calls": src.get("agent_calls"),
        "max_agent_calls": src.get("max_agent_calls"),
        "tuner_runs": src.get("tuner_runs"),
        "reviser_runs": src.get("reviser_runs"),
        "consumed_seconds": src.get("consumed_seconds"),
        "wall_elapsed_seconds": src.get("wall_elapsed_seconds") or wall,
        "terminal": src.get("pipeline_terminal") or src.get("terminal"),
    }


def _model_id(detailed: dict | None, slim: dict | None, default: str | None) -> str:
    for src in (detailed or {}, slim or {}):
        m = src.get("model")
        if m:
            return m
    return default or "unknown"


def _extract_model_role_map(detailed: dict | None) -> dict | None:
    """Build ``{role: model}`` the run ACTUALLY used, from v3 units.

    Aggregates per-role models across the root-level units (setup / splitter /
    designer / aggregator) AND every node's units (coder / evaluator / judge /
    selector / reviser). Each unit's model comes from
    ``units/<role>/steps/sdk_records/data.model``. Most-frequent model per role
    wins (ties broken lexicographically for stable re-ingest). Returns None when
    no unit recorded a model. This is the data source the router reads to
    attribute cost and outcomes per (role, model)."""
    if not detailed:
        return None
    counts: dict[str, dict[str, int]] = {}

    def _tally(units: dict) -> None:
        for role, m in _models_from_units(units).items():
            bucket = counts.setdefault(role, {})
            bucket[m] = bucket.get(m, 0) + 1

    _tally((detailed.get("root") or {}).get("units") or {})
    for n in (detailed.get("nodes") or {}).values():
        if isinstance(n, dict):
            _tally(n.get("units") or {})
    if not counts:
        return None
    return {
        role: sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for role, bucket in counts.items() if bucket
    }


def _top_level_model(role_map: dict | None) -> str | None:
    """One label for the run: the model if uniform across roles, else 'mixed'.
    None when there's no role map (caller falls back to state.model / default)."""
    if not role_map:
        return None
    models = {m for r, m in role_map.items() if r != "manager"}
    if not models:
        return None
    return next(iter(models)) if len(models) == 1 else "mixed"


def build_record(run_dir: Path, model_default: str | None, source: str) -> dict:
    detailed = _read_json(run_dir / "manager_state_detailed.json")
    slim = _read_json(run_dir / "manager_state.json")
    grade = _read_json(run_dir / "mlebench_grade.json")

    model_role_map = _extract_model_role_map(detailed)
    # Top-level label: the per-role models the run actually used (uniform → that
    # model, heterogeneous → "mixed"). Only fall back to the state-level model /
    # default when no per-role model was recorded at all.
    model = _top_level_model(model_role_map) or _model_id(detailed, slim, model_default)

    coder_model = (model_role_map or {}).get("coder") if model_role_map else None
    coder_model = coder_model or model
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
        "per_node": _extract_per_node(detailed, coder_model=coder_model),
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

    record = _scrub_personal_paths(build_record(run_dir, model_default, source=source))
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
    assert log_root.exists(), f"log_root does not exist: {log_root}"
    runs: list[Path] = []
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
    p.add_argument("--task", help="ingest all runs under <log-root>/<task>")
    p.add_argument("--all", action="store_true", help="ingest every run under <log-root>")
    p.add_argument("--log-root", help="root directory containing per-task run artifacts (required with --task/--all)")
    p.add_argument("--model-default", default=None,
                   help="model id to record when state.model is missing/empty")
    p.add_argument("--force", action="store_true", help="overwrite existing routing JSON + index row")
    p.add_argument("--source", default="cli",
                   help="provenance tag (cli | auto-curate | backfill)")
    p.add_argument("--limit", type=int, help="cap number of runs (testing)")
    args = p.parse_args()

    if not (args.run_dir or args.task or args.all):
        p.error("specify a run_dir, --task, or --all")

    if args.run_dir:
        targets = [Path(args.run_dir)]
    else:
        if not args.log_root:
            p.error("--log-root is required with --task/--all")
        targets = iter_run_dirs(args.task, log_root=Path(args.log_root))
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
