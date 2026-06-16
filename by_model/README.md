# `by_model/` — per-model capability knowledge

The **core asset** of routing memory is *what each model is good and bad at*,
per role / node / condition. The router's whole job is "pick the cheapest model
that's good enough for THIS role on THIS kind of task" — that decision is only as
good as the per-model knowledge here. As we add new models (and pin a model
library), this is where their performance profile accumulates.

This is the **model-centric** view of the same facts the `evidence/E###.json`
cards record task-centrically: every knowledge card cites the evidence/experiment
that backs it.

## Layout — one folder per model

```
by_model/<model-id>/
  card.md           # human-readable profile: tier, cost band, where it shines / fails
  knowledge.jsonl   # the atomic claims — one model-knowledge card per line (below)
  stats.md          # AUTO-GENERATED mechanical rollup (python -m memory.routing.rollup) — do not hand-edit
```

`<model-id>` is the full SDK id (e.g. `claude-opus-4-8`). `unknown/` holds
mixed-routing runs (no single model).

## Model-knowledge card schema (one JSON object per line in `knowledge.jsonl`)

```json
{
  "id": "MK-opus48-coder-schema-gate",
  "model": "claude-opus-4-8",
  "role": "coder",                 // a Role (setup/splitter/designer/coder/evaluator/judge/selector/reviser/aggregator) or "any"
  "scope": "strict-CSV submission schema gate (exact column names)",
  "verdict": "required",           // required | good | sufficient | insufficient | mixed | unknown
  "claim": "Only opus-coder (or opus-everywhere) produces a VALID submission on v3 detecting; cheaper coders all emit a malformed CSV.",
  "evidence_refs": ["E020", "E026"],
  "confidence": "high",            // high | medium | low
  "last_updated": "2026-06-05"
}
```

- **role** + **scope** = the *condition*. The router reads cards across all
  models and inverts them: "for `coder` on a schema-gate task → haiku=insufficient,
  sonnet=insufficient, opus=required → use opus."
- **verdict**: `required` (only this tier works), `good`/`sufficient` (holds the
  tier here), `insufficient` (drops the tier / fails), `mixed` (task-dependent),
  `unknown` (no data yet).
- **evidence_refs**: the `E###` cards (and/or experiment ids) that back the claim.
  Two-way link — evidence is task-indexed, these are model-indexed.

## How it connects to the rest of the loop

```
experiment (test_router.sh / pinned YAML)
   → run + grade + ingest  → runs/ + index.jsonl + stats.md (mechanical)
   → analyst writes evidence/E###.json (task-centric outcome)
   → distill into by_model/<model>/knowledge.jsonl (model-centric: this role/condition verdict)
   → router reads the model cards to choose the cheapest sufficient model per role
```

## Maintenance workflow (for the experiment agent)

1. After an experiment yields an evidence card, add/update the corresponding
   model-knowledge card(s) here — one per (model, role, condition) the run informs.
2. Obey the n≥2 reliability law before raising a verdict to `high` confidence.
3. When a NEW model is added: create `by_model/<model-id>/`, seed `knowledge.jsonl`
   (start everything `unknown`), and let experiments fill it.
4. Re-run `python -m memory.routing.rollup` to refresh `stats.md`.

## Forward-looking: model library

The set of `<model-id>` folders here IS the model library the router selects
from. Adding a model = adding a folder; retiring one = archiving it. The router's
candidate set should be derivable from this directory.
