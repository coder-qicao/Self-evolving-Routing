# claude-opus-4-8 — capability profile

**Tier:** capable ceiling. **Use as:** the coder where cheaper models drop the tier or fail a schema gate.

- ✅ **Required:** coder on the v3 detecting strict-CSV schema gate — only opus-coder/everywhere yields a valid submission (E020/E026).
- ✅ **Good:** denoising opus-everywhere = GOLD, n=3, ~$10.4 (don't per-node-downgrade — E032); leaf opus on coder+aggregator = valid, ~60% cheaper than all-opus (E022/E023/E042).
- 💡 Spend opus on the bottleneck role (usually coder, + the submission-writer), not everywhere, unless evidence shows the family needs all-opus (denoising).

See knowledge.jsonl + stats.md.

**Per-role (roles.md):** primary role is coder (11 runs / 4 tasks, denoising gold); designer pays off on planning-heavy tasks (text-norm silver).
