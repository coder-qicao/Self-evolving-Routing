# Routing Policy v1

**Last updated:** 2026-05-21
**Audience:** the manager agent. Read this first.
**Substrate:** `memory/routing/evidence/`, `task_memory/`, `stats/`.

The router decides which Anthropic model each sub-agent (setup / designer /
judge / coder / tuner / aggregator / manager) uses for the current task.
The goal is to **minimize cost** subject to **not regressing medal tier**.

---

## Decision rules (in priority order)

1. **Task-specific override.** If the current task has an entry under
   `task_memory/<family>.json § known_overrides`, use it verbatim.
   See E001 for an example.

2. **Family default.** Compute the task fingerprint
   (modality, data-size bucket, metric family). Look up the matching
   `task_memory/<family>.json § default_routing`. Use it.

3. **Global fallback.** If the task does not match any known family,
   use **`claude-sonnet-4-6` for every role.** Rationale: across the
   6 graded tasks we have data on, sonnet matches opus's medal tier on
   4 of them at 2–3× lower cost (E003); on the other 2 the picture is
   muddied by n=1 (E004). Sonnet is the safest first guess.

4. **Manager model is special.** The accounting shows manager $0 across
   all observed runs (E005). Until we cross-check against API logs,
   pick the manager model based on **reasoning quality**, not cost.
   Default: same as the rest of the routing (no cache penalty for
   uniform model selection).

---

## Promoted task-specific routings

| Task | Routing | Evidence | Confidence |
|---|---|---|---|
| `detecting-insults-in-social-commentary` | all roles → `claude-haiku-4-5` | E001 | high |

(only one entry today — the bar to add a row here is ≥ 2 confirmed gold runs OR explicit human approval, per the promotion rule.)

---

## Cost-saving priority

If you need to downgrade exactly one role to stay under budget,
downgrade in this order (highest impact first):

1. **tuner** (54% of total cost — E002)
2. **coder** (31% of total cost — E002)
3. **judge** (7%)
4. designer / setup / aggregator — together ~8%, downgrading any of
   them barely moves the bill

---

## Hypotheses NOT in policy yet

These are tracked but require validation before promotion. See
`experiments/queue.jsonl`.

- **H1**: On hard image tasks (leaf-classification), opus-tuner alone may
  bridge bronze → silver while coder/designer can stay on sonnet. Evidence
  E004 (n=1, low confidence). Test X002.
- **H2**: On stylometric/literary tasks (spooky-author), opus-coder is what
  drives bronze versus none. Evidence E004 (n=1, low confidence). Test X003.
- **H3**: For jigsaw-toxic (129MB, both sonnet & opus = none), neither
  model is the bottleneck — it's pipeline budget. Test X004 (raise
  `--run-budget-minutes`).

---

## When to update this file

- A new evidence card promoted (`policy_promoted: true`) → add a row.
- An existing promoted routing failed twice in production → demote it
  back to a hypothesis and re-run experiments.
- Never edit this file based on a single new data point. The point of
  this layer is **stability** for the manager.
