# claude-haiku-4-5 — capability profile

**Tier:** cheapest. **Use as:** the floor for *non-generating* roles.

- ✅ **Good:** judge / selector / evaluator / setup / splitter (evaluate/select, don't write code) — E002, E029.
- ✅ **OK:** aggregator on *loose* per-file submissions (denoising) — E021.
- ❌ **Insufficient:** coder on strict-CSV schema gates (v3 detecting → invalid) — E010/E020; coder on hard image — E012.
- ⚠️ v2 haiku-everywhere golds were v2-only; v3 added a schema gate haiku can't clear.

Rule of thumb: spend haiku on the cheap evaluative roles; never on the coder of a hard or strict-schema task. See knowledge.jsonl + stats.md.
