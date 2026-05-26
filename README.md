# memory/routing — self-evolving per-role routing memory

The router decides which Anthropic model each sub-agent (setup / designer /
judge / coder / tuner / aggregator / manager) uses for a given task.
Its data substrate lives here.

The manager (LLM) **never reads raw `index.jsonl`** at runtime. It only
sees the small, curated artifacts under `policy/` and a top-k retrieval
of `task_memory/`.

## Layout

```
memory/routing/
├── README.md              # this file
├── index.jsonl            # raw per-run log (append-only, source of truth)
├── runs/<model>/<task>/<timestamp>.json   # per-run JSON (one file per ingest)
├── ingest.py              # populate runs/ + index.jsonl from a run dir
├── rollup.py              # produce derived views
│
├── policy/
│   └── routing_policy.md  # short, manager-facing routing policy
│
├── stats/
│   └── role_model_stats.json   # aggregate cost+outcome stats by (role, model)
│
├── evidence/
│   └── E###-<slug>.json   # one claim per file, with confidence + scope
│
├── task_memory/
│   └── <family>.json      # routing memory grouped by task family + fingerprint
│
├── experiments/
│   └── queue.jsonl        # candidate routings to try (one per line)
│
└── archive/
    └── analysis-v1-*.md   # superseded narrative docs
```

## Read order for the manager (LLM)

1. **`policy/routing_policy.md`** — always loaded into the manager's
   context. ~1 page; short decision rules + the current safe defaults.
2. **`task_memory/<matched_family>.json`** — top-k retrieved by task
   fingerprint (modality / size bucket / metric type). Manager sees only
   the matched entries, not every family.
3. **`stats/role_model_stats.json`** — only loaded if the policy can't
   decide and the manager needs raw cost/quality numbers.

`evidence/*.json`, `experiments/queue.jsonl`, and `index.jsonl` are
**audit + automation surfaces**, not LLM context. They feed the policy
update process, not the manager.

## Evidence card schema

Every claim in the system traces back to one or more evidence files:

```json
{
  "id": "E001",
  "claim": "<one-sentence factual statement>",
  "support": {"task": "...", "model": "...", "runs": ["<ts>", ...], "n": N, "outcomes": "...", "avg_cost_usd": ...},
  "scope":         "<what this claim covers — task / family / role>",
  "limitations":   "<what it does NOT cover, why we shouldn't over-generalize>",
  "confidence":    "high | medium | low",
  "last_updated":  "YYYY-MM-DD",
  "policy_promoted": true | false
}
```

Promotion rule (rule 10 in the original spec): a hypothesis becomes part
of `routing_policy.md` only when it is either
(a) confirmed by ≥2 independent successful runs **OR**
(b) explicitly approved by a human.
Single-data-point hypotheses live in `experiments/queue.jsonl`, not policy.

## Task family fingerprint

`task_memory/*.json` is keyed by **family** (e.g. `text-classification`,
`image-classification-small`). Each family declares a `fingerprint`:

```json
"fingerprint": {
  "modality": "text | image | tabular | audio | multimodal",
  "data_size_bucket": "tiny<10MB | small<100MB | medium<1GB | large",
  "metric_family": "auc | log_loss | accuracy | mae | iou | bleu | rmse"
}
```

The router computes a fingerprint for the incoming task (deterministically
from task_mapping.json + the prepared/ data dir size + the metric name)
and matches it against family fingerprints. Top-k by exact-match-count is
returned to the manager. Missing fields fall through to the global
fallback in `routing_policy.md`.

## Update flow

1. A run finishes → `ingest.py` appends to `index.jsonl` and writes
   `runs/<model>/<task>/<ts>.json`.
2. A cron / hook (TBD) runs `python -m memory.routing.rollup` which
   regenerates `stats/role_model_stats.json` from `index.jsonl`.
3. New evidence is hand-authored under `evidence/` (or auto-generated
   for purely statistical claims). Each has `policy_promoted: false`
   by default.
4. A human reviews evidence and either promotes it (sets `true` and
   updates `policy/routing_policy.md` to reference it) or leaves it
   pending.

This keeps the LLM-facing surface stable, auditable, and small.


## v2 vs v3 role surface

This memory was started on `qi_memory_router_v2` (8 roles: setup, designer,
judge, coder, **tuner**, reviser, aggregator, **manager**). main-v3
refactored the agent surface to 9 roles: setup, **data_split**, designer,
coder, **evaluator**, judge, **selector**, reviser, aggregator. Three roles
were dropped (tuner, manager, ...) and three were added (data_split,
evaluator, selector). See `evidence/E009` for the full mapping and which
historical evidence is still valid vs legacy-only.

The router (`engine/routing/router.py:ROUTED_ROLES`) is sourced from
`domain/role.py § Role` enum on v3.
