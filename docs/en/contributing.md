# Contributing

This document defines the **code comment conventions** and **documentation rules**
for this project.

Why this exists: after a period of fast iteration, newly written modules had comments
while the early large files (`apps/base/views.py`, `core/storage.py`,
`apps/admin/views.py`) had almost none, and `readme_en.md` drifted far behind the
implementation. Documentation that contradicts the code is worse than no documentation —
people write code against wrong conventions and deploy with wrong settings.

So these are hard requirements here, not suggestions.

---

## 1. Comment Conventions

### Core principle: comments explain *why*, not *what*

The code already says what it does. Restating it adds nothing, and — worse — such
comments **become lies** as soon as the code changes. The scarce value of a comment is
explaining what cannot be seen from the code:

- Why this approach, and not the obvious alternative
- Which traps were already hit, so nobody reverts into them
- Edge cases, side effects, preconditions callers must satisfy

### Module-level docstring: required for every file

Place it at the very top of the file (before `from __future__`). Triple quotes.

Cover three things, in any order, but **always answer "why"**:

1. **Problem** — what real problem this module solves (not `Provides XX features`)
2. **Approach** — the key design decision and why it was chosen
3. **Trade-offs** — what is deliberately *not* done, and why (most valuable, most often skipped)

See `core/cluster.py` and `core/scan.py` for the house style.

Bad:

```python
"""Views for files."""   # restates the filename; zero information
```

Good:

```python
"""Room cache: keep files sent inside a room on the server so they can be
fetched later.

## Problem
Direct transfer is P2P — both peers must be online at the same time. A recipient
who joins a minute late would never get the file.

## Approach
The sender caches the file server-side; the recipient pulls it after coming online.
Cache is scoped per room and expires by time.

## Deliberately not done
Files above roomCacheMaxSize are not cached — caching large files exhausts disk.
Those degrade to pure P2P, and the UI says so explicitly.
"""
```

### Function docstring: only what the caller cannot infer

Write about: edge cases (out-of-range, empty, concurrent behavior); side effects
(writes DB? emits events? consumes a download quota?); preconditions; unusual return
conventions (e.g. "returns None means X, not an error").

Do not write: a line-by-line restatement, or filler that repeats parameter names.

### Inline comments: only for the non-obvious

```python
# Deliberately not deleting the file here: instant upload (dedup) makes several
# records share one physical file — deleting it breaks the other records
```

Bad inline comments: `# loop over list` (the code says it), or a bare `# TODO: optimize`.

### When you change logic

**Update the matching comment in the same commit.** A comment that contradicts the
implementation is a landmine. Reviewers treat this as seriously as the logic itself.

---

## 2. Documentation Rules

### Documentation must match the implementation

When adding or changing routes, config keys, or env vars, update the docs **in the
same commit**:

| Change | Docs to update |
|--------|----------------|
| Add/change a route | `readme.md` API overview, `docs/api/index.md` |
| Add a config key | `readme.md` config section, `docs/guide/configuration.md` |
| Add an env var | `readme.md` env var table, `docs/guide/getting-started.md` |
| Add/change a model field | `apps/base/models.py` comment + the migration file |
| Deployment-related | `readme.md` multi-instance section, `docs/guide/getting-started.md` |

### Keep Chinese and English in sync

`readme.md` / `readme_en.md` and `docs/guide/*` / `docs/en/guide/*` must cover the
**same feature set**. The English docs once lagged badly — missing the inbox
(collection) and direct-transfer (WebRTC) sections entirely — which misleads
international users directly.

### Never document something you did not verify

Run every command, config value, and API example before writing it down.
A past incident: the readme recommended `WORKERS = CPU cores`, but room WebSocket
state lived in process memory at the time, so following that advice guaranteed
"only half the room receives messages" — and it is painfully hard to diagnose.

---

## 3. Verification

Even a "comments only" change must not be trusted by eye — a docstring placed
wrongly breaks strings, indentation, or syntax.

### Backend

```bash
# Import for real: py_compile cannot catch a missing import
python -c "import apps.base.views, core.storage, main"

pytest -q
```

### Frontend

```bash
npm run type-check   # vue-tsc
npm run build        # typecheck passing does NOT mean it builds
```

> `vue-tsc` and `@vue/compiler-sfc` enforce different rule sets. An `export` inside
> `<script setup>` produces no typecheck error at all, and fails only at build time.

### Docs

```bash
cd docs && npm run docs:build
```

---

## 4. Commit Convention

- Format: `type(scope): short summary`
- type: `feat` / `fix` / `docs` / `refactor` / `perf` / `test` / `chore`
- Body explains **why**, especially trade-offs and traps — that context does not
  fit in code but is exactly what you need months later
- Keep comment/doc changes in their own commits, separate from logic changes
