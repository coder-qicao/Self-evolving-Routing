# Routing Policy v2

**Last updated:** 2026-06-03
**Audience:** the **LLM router** (`engine/routing/LLMRouter`) — read this first —
and the humans/agents curating `policy.json` + `task_memory/`.
**Substrate:** `memory/routing/evidence/` (E001–E024), `task_memory/`, `stats/`.

The router decides which Anthropic model each of the **9 v3 sub-agent roles**
(setup / splitter / designer / coder / evaluator / judge / selector / reviser /
aggregator) uses for the current task (E009 — the old v2 "tuner" role is gone).
The goal is to pick, for each role, the **cheapest model that still lands the
best medal tier the task can reach** — the **minimum viable model**.
**"Completes" means EARNS A MEDAL, not merely a valid submission**: a valid
no-medal run forfeits the whole run's cost for zero result (e.g. spooky:
sonnet=none vs opus=bronze, E004 → opus is the minimum viable there). opus is
materially more capable than sonnet/haiku on hard reasoning + code, so unless a
cheaper model is shown to reach the SAME tier, use the stronger model on the
quality-critical roles (coder, designer). Cheap models are for easy tasks
(proven, e.g. E001/E003) and non-generating roles (E002) — not a reflex. Aim
for cheapest-that-earns-the-tier — **not** cheapest outright, **not** blanket opus.

---

## Decision rules (in priority order)

0. **Anchor on what actually RAN, not on per-role guesses.** The ground truth is
   `index.jsonl` + `runs/<model>/<task>/*.json` — each row is one WHOLE routing
   config and its real outcome (medal / cost / valid_submission). Prefer the
   **cheapest whole config a real run shows COMPLETED** this task (or its closest
   kind). `stats/role_model_stats.json` is a post-hoc decomposition, not proof
   any per-role mix works — roles are NOT freely separable (E020: every
   non-uniform routing on detecting was invalid; only the whole opus-everywhere
   config completed). Deviate a single role from a proven config only where a run
   isolated that role.

1. **Task-specific override.** If the task has an entry under
   `policy.json § tasks[<task>]` or `task_memory/<family>.json §
   known_overrides`, use it verbatim. See E001.

2. **Family default.** Compute the task fingerprint (modality, data-size
   bucket, metric family). Look up `task_memory/<family>.json §
   default_routing`. Use it.

3. **UNSEEN task → apply the generalizable principles below.** This is the
   critical path: a task that matches no known family must STILL be routed
   well. The static router falls back to a single global model; the LLM
   router instead reasons from the principles in the next section plus the
   task instruction and a peek at the actual data.

4. **Manager/orchestration model.** Accounting shows the manager/orchestrator
   reports ~$0 across all runs (E005) — pick it for reasoning quality, not
   cost.

---

## Generalizable principles for UNSEEN tasks

These are distilled cross-task patterns (not task-name lookups), so they
transfer to a task we have never run. Apply them in order; each cites the
evidence it generalizes from. **Default stance: the minimum viable model — the
cheapest model evidence shows still completes the task at tier for this *kind*
of task.** Downgrade a role where it's proven safe (P2, E002); keep or raise
capability only where evidence shows the cheaper model FAILS to complete (P3/P4).
Not cheapest-at-all-costs (a too-weak model forfeits the run), not
capability-maxxing (wasted money) — the cheapest that gets the job done.

### P1 — Where the money is: the coder dominates; most roles are nearly free
The coder is by far the most expensive role; setup / splitter / judge /
selector / reviser / aggregator together are a small slice, and the
manager is ~$0 (E002, E005). **Consequence:** to cut cost, move the *coder*
down a tier first; demoting the cheap roles barely moves the bill and risks
breaking things for no savings. To spend up for quality, spend on the coder
first.

### P2 — Easy task → cheapest model everywhere is the right first bet
On low-difficulty tasks (small data, standard/tabular metric, well-trodden
problem shape) the cheapest model matches the expensive ones' medal tier:
haiku golded detecting at ~$5 (E001); sonnet matched opus on 3–4 of 6 tasks
at 2–3× lower cost (E003); haiku silvered a tabular-regression task (E007).
**Rule:** for an easy-looking unseen task, route **haiku (or sonnet)
everywhere** and only escalate if it misses tier or fails validation.

### P3 — Hard task → opus on the *bottleneck* role, not everywhere
On hard tasks (deep image classification, stylometry, tight gold thresholds)
a more capable model does bridge a medal tier (E004), but **opus-everywhere
is wasteful** — the lift comes from opus on the right role:
- leaf: opus on {coder, aggregator} gave a valid submission at **60% less**
  than opus-everywhere (E022); opus-everywhere + bigger search reached
  above_median (E023).
- but isolating opus to a *non-bottleneck* role does nothing: opus-tuner-only
  (E008) and opus-designer-only / opus-aggregator-only on detecting (E018,
  E019) all failed.
**Rule:** escalate the coder first; add the output/submission-writing role
second; leave the rest cheap. Verify the chosen role is actually the
bottleneck before paying for it.

### P4 — The submission-SCHEMA gate is real, and its severity is task-shaped
Some tasks reject a structurally-wrong submission regardless of score, and
whether cheap models clear that gate depends on the **output format**:
- **Strict tabular CSV with exact column names** is brittle: on detecting
  (needs an exact `Comment` column), *every* routing without opus-coder —
  haiku-all, sonnet-all, +sonnet/opus on designer/aggregator in any combo —
  produced an invalid submission; only opus-coder or opus-everywhere passed
  (E010, E013, E014, E015, E016, E018, E019, E020, E024).
- **Loose per-file output** (e.g. per-image PNGs) is forgiving: even
  haiku-aggregator produced a valid submission on denoising (E021).
**Rule:** if the task's submission is a CSV keyed on exact column names /
ID conventions, treat the **coder** as schema-critical and give it a capable
model (sonnet→opus) up front. If the output is free-form per-file artifacts,
cheap models are usually fine on shape.

### P5 — Capability and search-budget are independent levers
On a hard task, raising model capability alone may not bridge a tier if the
search is too shallow, and vice-versa: opus-coder + bigger search still
missed leaf above_median under a tight budget (E012), while opus-everywhere
**with doubled search** (num_designs=8, max_nodes=16, budget=180m) did reach
it (E023). **Rule:** when a hard task underperforms, consider raising
`search.*` budget, not just the model tier.

### P6 — Trust tiers only at n≥2; some tasks are variance-dominated
A single good run can be luck. The denoising silver (E011) did **not**
reproduce on a second identical run (E017); across n=3 the task gave
silver/none/none (E021). **Rule:** do not promote or believe a medal tier
from one run on a task whose metric is tight or generative; require a
confirmation run before acting.

### P7 — Opus on multiple roles costs WALL-CLOCK, not just dollars
Splitting opus across two roles pushed a leaf run past the 2h wall-clock
before it could produce a submission (X214, inconclusive). **Rule:** when
escalating multiple roles to opus, raise `budget.pipeline_budget_minutes`
accordingly or expect timeouts.

### Putting it together (unseen-task starting routing)
1. Classify the task: modality, data size, metric, **submission format**,
   apparent difficulty (peek at the data).
2. For each role pick the cheapest model evidence shows still **completes** the
   task: cheap for easy tasks and non-generating roles (P2/E001/E003, E002 —
   judge/selector/etc.); more capable only where the cheaper model is shown to
   FAIL to complete (P3/P4 — e.g. the coder on a hard / strict-submission task).
3. If submission is strict-CSV → the **coder** is the schema gate; keep it
   capable (sonnet→opus, P4).
4. If the task looks hard (P3) → opus on coder, then the submission-writer;
   give search room (P5) and wall-clock room (P7).
5. Never trust the first run's tier on a tight/generative metric (P6).
6. When in doubt, weigh BOTH failure modes: a too-weak model forfeits the run
   (worst outcome), a too-strong model just overspends. Pick the cheapest model
   you're confident still completes the task. *(Planned: factor remaining
   budget — when the budget is tight, lean to a leaner model; see "Budget-aware
   selection" in the per-node-routing spec.)*

---

## Promoted task-specific routings

| Task | Routing | Evidence | Confidence |
|---|---|---|---|
| `detecting-insults-in-social-commentary` | all roles → `claude-haiku-4-5` | E001 | high |

> ⚠️ Caveat: E001 is the **v2** result. On v3 the same routing fails the
> schema gate (E010, E020) — v3 detecting needs opus-coder or opus-everywhere
> for a valid submission. This row is retained as the v2 baseline but should
> NOT be applied to a v3 run until re-confirmed. The bar to add/keep a row
> here is ≥2 confirmed gold runs OR explicit human approval.

---

## Cost-saving priority (v3 role surface)

To downgrade exactly one role to stay under budget, in highest-impact order:

1. **coder** — by far the largest share of total cost (E002). But see P4:
   on strict-CSV-schema tasks the coder is *also* the schema gate, so
   downgrading it can flip a valid submission to invalid. Downgrade the
   coder only on easy / loose-output tasks.
2. **judge / evaluator** — moderate share.
3. setup / splitter / designer / selector / reviser / aggregator — together
   a small slice; downgrading any barely moves the bill (and the manager is
   ~$0, E005).

---

## Open hypotheses (not in policy yet)

Tracked in `experiments/queue_v3.jsonl`; validate before promotion.

- **H-leaf**: opus on {coder, aggregator} is the minimum routing that gives a
  valid leaf submission (E022, n needs ≥2). Probe a confirmation run.
- **H-denoise-variance**: denoising tier is variance-dominated; no routing is
  reliably silver (E017, E021). Needs a variance study, not more routings.
- **H-unseen-generalization**: the P1–P7 principles above are the routing
  brain for tasks with no family entry — validate them by routing NEW tasks
  (text #2, tabular #2) the policy has never seen and checking the LLM
  router's first guess lands the tier.

---

## When to update this file

- A new evidence card promoted (`policy_promoted: true`) → add a row.
- A promoted routing failed twice in production → demote to a hypothesis.
- A generalizable principle (P1–P7) contradicted by ≥2 new tasks → revise it
  and cite the counter-evidence.
- Never edit based on a single data point — this layer is **stability** for
  the router (P6).
