# Working with a coding agent

The repository keeps shared operating instructions in `AGENTS.md`. Codex and Cursor read
that file directly; `CLAUDE.md` imports it so Claude Code receives the same context.
Keeping one source avoids three instruction files drifting apart.

## First prompt

Replace the objective and paste this as the first message from the repository root:

```text
Read AGENTS.md and README.md, then inspect git status and the relevant code/tests before
acting. Objective: <describe the outcome>.

Work autonomously through implementation and verification. Preserve unrelated local
changes and private data. Keep ingestion cache-first, point-in-time safe and explicit
about missing data. Update the appropriate docs when public behavior changes. Run focused
tests while iterating and the repository quality gates before handing off. End with a
short self-review: files changed, checks run, remaining data-quality limits, and anything
that still needs my decision. Do not commit, push, tag or publish unless I explicitly ask.
```

## Useful starting tasks

- `Audit the current corpus coverage and explain the largest trustworthy gap.`
- `Implement issue #N, including migration, tests and external-clone reproducibility.`
- `Review the local diff for data leakage, provenance loss and migration regressions.`
- `Regenerate and verify a portable corpus export without calling paid APIs.`

## Tool-specific notes

- Codex discovers root `AGENTS.md` at session start; restart the session after changing
  instructions.
- Claude Code loads `CLAUDE.md`, whose `@AGENTS.md` import points to the shared guide.
- Cursor supports root `AGENTS.md`; no legacy `.cursorrules` file is necessary.

Agents must not receive secrets in prompts. Put provider credentials only in the ignored
locations documented in [Data and corpus](data-and-corpus.md).
