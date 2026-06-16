"""Aggregate routing memory into per-model markdown summaries.

Pure data rollup, no LLM. Each model gets ``by_model/<model>/``:
  - ``stats.md``  — whole-run aggregate (from index.jsonl): n runs, medal counts,
    mean cost/wall, error rate, per-task breakdown. Counts runs where this model
    was the uniform top-level model.
  - ``roles.md``  — per-(model, role) decomposition (from runs/*/*.json): how
    often the model played each role across ALL runs INCLUDING 'mixed' per-node
    ones, the tasks, mean per-role cost, and the medal mix of those runs. This
    is how a model's role-by-role capability surfaces from mixed routings.

LLM-level synthesis ("what's this model good at?") lives in the curated
``knowledge.jsonl`` cards alongside these; rollup only makes the raw signal
queryable.

Usage:
    python -m memory.routing.rollup
    python -m memory.routing.rollup --model claude-opus-4-7-...
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROUTING_ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROUTING_ROOT / "index.jsonl"
RUNS_DIR = ROUTING_ROOT / "runs"
BY_MODEL_DIR = ROUTING_ROOT / "by_model"

# Canonical role order for the per-role table.
ROLE_ORDER = (
    "setup", "splitter", "designer", "coder", "evaluator",
    "judge", "selector", "reviser", "aggregator",
)
_ROLE_SUFFIX_RE = re.compile(r"_(review|node)_\d+$|_\d+$")


def _load_index() -> list[dict]:
    if not INDEX_PATH.exists():
        return []
    rows = []
    for line in INDEX_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _safe_mean(values: list[float | int | None]) -> float | None:
    clean = [v for v in values if isinstance(v, (int, float))]
    if not clean:
        return None
    return statistics.fmean(clean)


def _safe_median(values: list[float | int | None]) -> float | None:
    clean = [v for v in values if isinstance(v, (int, float))]
    if not clean:
        return None
    return statistics.median(clean)


def _fmt(v, prec: int = 2, default: str = "—") -> str:
    if v is None:
        return default
    if isinstance(v, float):
        return f"{v:.{prec}f}"
    return str(v)


def _medal_counts(rows: list[dict]) -> dict[str, int]:
    out = {"gold": 0, "silver": 0, "bronze": 0, "none": 0, "ungraded": 0}
    for r in rows:
        m = (r.get("medal") or "ungraded")
        out[m] = out.get(m, 0) + 1
    return out


def _safe_name(model: str) -> str:
    return model.replace("/", "_")


def _render_model_md(model: str, rows: list[dict]) -> str:
    n = len(rows)
    medals = _medal_counts(rows)
    medaled = medals["gold"] + medals["silver"] + medals["bronze"]
    above_med = sum(1 for r in rows if r.get("above_median") is True)
    err_runs = sum(1 for r in rows if (r.get("errors") or 0) > 0)

    parts: list[str] = []
    parts.append(f"# Routing memory — `{model}`")
    parts.append("")
    parts.append(f"_{n} run(s) ingested._")
    parts.append("")
    parts.append("## Aggregate")
    parts.append("")
    parts.append("| metric | value |")
    parts.append("|---|---|")
    parts.append(f"| Runs | {n} |")
    parts.append(f"| Distinct tasks | {len({r.get('task') for r in rows})} |")
    parts.append(f"| Medals (gold/silver/bronze) | {medals['gold']} / {medals['silver']} / {medals['bronze']} |")
    parts.append(f"| Any medal rate | {_fmt(medaled / n * 100 if n else None, 1)}% |")
    parts.append(f"| No-medal / ungraded | {medals['none']} / {medals['ungraded']} |")
    parts.append(f"| Above-median rate | {_fmt(above_med / n * 100 if n else None, 1)}% |")
    parts.append(f"| Mean cost (USD) | {_fmt(_safe_mean([r.get('cost_usd') for r in rows]), 2)} |")
    parts.append(f"| Median cost (USD) | {_fmt(_safe_median([r.get('cost_usd') for r in rows]), 2)} |")
    parts.append(f"| Mean wall (s) | {_fmt(_safe_mean([r.get('wall_s') for r in rows]), 0)} |")
    parts.append(f"| Mean agent calls | {_fmt(_safe_mean([r.get('agent_calls') for r in rows]), 1)} |")
    parts.append(f"| Runs with errors | {err_runs} ({_fmt(err_runs / n * 100 if n else None, 1)}%) |")
    parts.append("")
    parts.append("## Per task")
    parts.append("")
    parts.append("| task | n | best medal | best score | mean cost (USD) | mean wall (s) | errors |")
    parts.append("|---|---|---|---|---|---|---|")

    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_task[r.get("task") or "?"].append(r)

    medal_rank = {"gold": 4, "silver": 3, "bronze": 2, "none": 1, "ungraded": 0}
    for task in sorted(by_task):
        trows = by_task[task]
        best_medal = max((r.get("medal") or "ungraded" for r in trows), key=lambda m: medal_rank.get(m, 0))
        scores = [r.get("score") for r in trows if isinstance(r.get("score"), (int, float))]
        best_score = max(scores) if scores else None
        mean_cost = _safe_mean([r.get("cost_usd") for r in trows])
        mean_wall = _safe_mean([r.get("wall_s") for r in trows])
        errs = sum(1 for r in trows if (r.get("errors") or 0) > 0)
        parts.append(
            f"| {task} | {len(trows)} | {best_medal} | {_fmt(best_score, 4)} | "
            f"{_fmt(mean_cost, 2)} | {_fmt(mean_wall, 0)} | {errs} |"
        )
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append("Generated by `python -m memory.routing.rollup`. Source: `memory/routing/index.jsonl`.")
    return "\n".join(parts) + "\n"


def _load_run_records() -> list[dict]:
    """Load every per-run JSON under runs/ (these carry model_role_map +
    cost.per_role, which the flat index does not)."""
    out = []
    if not RUNS_DIR.exists():
        return out
    for f in RUNS_DIR.glob("*/*/*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _base_role(role: str) -> str:
    """Collapse a suffixed role to its base (evaluator_review_0 -> evaluator)."""
    return _ROLE_SUFFIX_RE.sub("", role)


def _per_model_role_usage(records: list[dict]) -> dict[str, dict[str, dict]]:
    """{model: {base_role: {n_runs, tasks, role_cost_sum, medals}}}.

    Decomposes EVERY run (including 'mixed' per-node runs) by role, attributing
    each role to the model THAT role actually ran on (from model_role_map) and
    that role's cost (from cost.per_role). Counts once per (model, role) per run.
    Medals are the WHOLE-RUN outcome the model participated in — context, not a
    per-role causal claim (roles aren't separable; see E020)."""
    agg: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "tasks": set(), "cost": 0.0, "medals": Counter()})
    )
    for rec in records:
        rmap = rec.get("model_role_map") or {}
        per_role_cost = (rec.get("cost") or {}).get("per_role") or {}
        task = rec.get("task") or "?"
        medal = (rec.get("result") or {}).get("medal") or "ungraded"
        base_model: dict[str, str] = {}
        base_cost: dict[str, float] = defaultdict(float)
        for role, model in rmap.items():
            if not model:
                continue
            base = _base_role(role)
            base_model.setdefault(base, model)
            rc = per_role_cost.get(role) or per_role_cost.get(base) or {}
            base_cost[base] += float(rc.get("cost_usd") or 0.0)
        for base, model in base_model.items():
            cell = agg[model][base]
            cell["n"] += 1
            cell["tasks"].add(task)
            cell["cost"] += base_cost[base]
            cell["medals"][medal] += 1
    return agg


def _render_roles_md(model: str, roles: dict[str, dict]) -> str:
    parts = [
        f"# Per-role usage — `{model}`",
        "",
        "How often this model played each role (across ALL runs, including mixed "
        "per-node ones), the tasks, mean per-role cost, and the medal mix of the "
        "runs it was part of. Medals are the **whole-run** outcome the model took "
        "part in — context for the knowledge cards, NOT a per-role causal claim.",
        "",
        "| role | runs | distinct tasks | mean role cost (USD) | run medals (g/s/b/none/ung) |",
        "|---|---|---|---|---|",
    ]
    ordered = [r for r in ROLE_ORDER if r in roles] + sorted(set(roles) - set(ROLE_ORDER))
    for role in ordered:
        c = roles[role]
        m = c["medals"]
        medals = f"{m['gold']}/{m['silver']}/{m['bronze']}/{m['none']}/{m['ungraded']}"
        mean_cost = c["cost"] / c["n"] if c["n"] else None
        parts.append(
            f"| {role} | {c['n']} | {len(c['tasks'])} | {_fmt(mean_cost, 3)} | {medals} |"
        )
    parts += [
        "",
        "Tasks per role:",
    ]
    for role in ordered:
        parts.append(f"- **{role}**: {', '.join(sorted(roles[role]['tasks']))}")
    parts += ["", "---", "",
              "Generated by `python -m memory.routing.rollup`. Source: `memory/routing/runs/`."]
    return "\n".join(parts) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", help="rollup only this model")
    args = p.parse_args()

    rows = _load_index()
    if not rows:
        print("no rows in index.jsonl yet — run `python -m memory.routing.ingest ...` first", file=sys.stderr)
        return 1

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r.get("model") or "unknown"].append(r)

    targets = [args.model] if args.model else sorted(by_model)
    BY_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for model in targets:
        model_rows = by_model.get(model, [])
        if not model_rows:
            print(f"[skip] no rows for model {model}", file=sys.stderr)
            continue
        # Each model is a FOLDER: stats.md is the mechanical rollup; the curated
        # model-knowledge cards (knowledge.jsonl + card.md) live alongside it.
        model_dir = BY_MODEL_DIR / _safe_name(model)
        model_dir.mkdir(parents=True, exist_ok=True)
        out = model_dir / "stats.md"
        out.write_text(_render_model_md(model, model_rows))
        print(f"[ok] {out}  ({len(model_rows)} rows)")

    # Per-(model, role) decomposition: attributes every run — including 'mixed'
    # per-node ones — to the real model each role ran on. This is how a model's
    # role-by-role capability surfaces even when it never ran a whole config.
    role_usage = _per_model_role_usage(_load_run_records())
    role_targets = [args.model] if args.model else sorted(role_usage)
    for model in role_targets:
        roles = role_usage.get(model)
        if not roles:
            continue
        model_dir = BY_MODEL_DIR / _safe_name(model)
        model_dir.mkdir(parents=True, exist_ok=True)
        out = model_dir / "roles.md"
        out.write_text(_render_roles_md(model, roles))
        print(f"[ok] {out}  ({sum(c['n'] for c in roles.values())} role-uses)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
