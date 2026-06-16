# claude-sonnet-4-6 — capability profile

**Tier:** mid. **Use as:** the default for coder/designer on non-hard tasks.

- ✅ **Good:** coder/designer on easy-to-medium tasks — matches opus tier at 2-3x less (E003); designer+coder on tabular-regression golds nomad cheaply (E029).
- ❌ **Insufficient:** coder on the v3 detecting strict-CSV schema gate — sonnet-all still invalid (E016/E020). Hard schema gates need opus.

Rule of thumb: sonnet is the workhorse for quality roles; escalate to opus only where a schema gate or hard task is shown to defeat it. See knowledge.jsonl + stats.md.

**Per-role (roles.md):** designer is its strongest broad role (8 runs / 6 families); coder viable on medium tasks (gold+silver on 4 tasks).
