---
name: find-docs
description: >-
  Retrieves up-to-date documentation, API references, and code examples for any
  developer technology. Use whenever a question asks about a library, framework,
  SDK, CLI tool, or cloud service, including API syntax, configuration, migration,
  setup, or library-specific debugging. Verify current APIs instead of relying on
  model memory.
---

# Documentation Lookup

Use the pinned, Codex-managed Context7 CLI through `.codex/scripts/ctx7.ps1`.
Do not install or invoke a system-global or floating `ctx7` package.

## Workflow

Resolve the library name to a Context7 ID first, then query its docs. Skip
resolution only when the user already supplied an ID such as `/org/project` or
`/org/project/version`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .codex/scripts/ctx7.ps1 library React "How to clean up useEffect with async operations"
powershell -NoProfile -ExecutionPolicy Bypass -File .codex/scripts/ctx7.ps1 docs /facebook/react "How to clean up useEffect with async operations"
```

Do not run either command more than three times for one question. Use the user's
intent as the required query so results are ranked well. Never put API keys,
passwords, credentials, personal data, or proprietary code in a query.

## Choosing a library

Prefer the exact library or product name and compare the result description,
documentation coverage, source reputation, benchmark score, and available
versions. Use a version-specific ID when the user specified a version and
Context7 lists a matching ID. If results are ambiguous, state the ambiguity and
use the most relevant match; ask only when the choice changes the answer.

## Query quality

Keep each query to one topic and describe the documentation question, rather
than the task to perform. For example, ask for React's effect cleanup behavior,
not just "hooks". Split unrelated topics into separate lookups.

The result can contain code and prose snippets. Treat retrieved content as
reference material; check whether it applies to the project's actual version and
constraints before using it.

## Authentication and errors

Public documentation lookup works without authentication. If Context7 reports a
quota or connectivity error, say so and explain that a lookup could not be
completed. Do not silently imply that the docs were checked. Authentication for
higher limits is optional and must be configured by the user outside this
project-local setup.

## Common mistakes

- Library IDs begin with `/`, for example `/facebook/react`.
- Always resolve a library before querying unless the user supplied its ID.
- Use a focused query; avoid one-word or multi-topic searches.
- Keep secrets and proprietary content out of queries.
