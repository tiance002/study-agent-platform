# Codex project integrations

This project keeps MCP activation and Serena state in the repository while the
component programs live in Codex's own tool directory (`%CODEX_HOME%\tools`, or
`%USERPROFILE%\.codex\tools` when `CODEX_HOME` is unset). No package is added to
the system Python/Node installation or the system `PATH`.

## Components

- **Superpowers:** already available in this Codex environment; intentionally not
  copied or reinstalled here.
- **Serena:** `serena-agent==1.7.0`, installed in the Codex-owned Python
  environment at `tools/integrations/serena`. The tracked
  `.serena/project.yml` contains project settings; `.serena/project.local.yml`
  holds machine-only overrides. `SERENA_HOME` points to ignored
  `.serena/runtime`, which contains logs, caches, and global memories for this
  project only.
- **Context7:** `ctx7==0.5.11`, installed under
  `tools/integrations/context7`; the project `find-docs` skill is in
  `.agents/skills/find-docs` and invokes the Codex-owned CLI through
  `.codex/scripts/ctx7.ps1`.
- **Playwright:** `@playwright/mcp==0.0.82`, installed under
  `tools/integrations/playwright-mcp`. The MCP server uses Codex-bundled Node,
  a headless isolated Chromium context, and routes browser binaries, temporary
  data, profile/cache support files, and outputs into ignored
  `.codex/runtime/playwright`.

The JavaScript packages use separate pnpm projects and a Codex-owned pnpm
store. Serena uses its own locked Python environment. This keeps transitive
dependencies separate from each other and from the project application.

## Codex setup

Codex loads `.codex/config.toml` only for trusted projects. After setup, restart
the Codex task or app so it reads the project MCP entries. Check `/mcp` for
Serena and Playwright. Context7 is a skill plus CLI, so it does not appear in
the MCP server list.

Codex tool packages are outside the repository. Serena's state and Playwright's
Chromium binaries are project-local and ignored by Git. Pinned manifests and
lockfiles in `.codex/manifests`, MCP settings, skills, and
`.serena/project.yml` are project files.

## Setup

Run `.codex/scripts/install-integrations.ps1` in PowerShell to copy the pinned
manifests to `%CODEX_HOME%\tools\integrations` and install their locked
dependencies there. The project wrappers resolve the Codex tool directory from
`CODEX_HOME` (or the standard `.codex` location).

Run `.codex/scripts/verify-integrations.py` with the Python executable in the
Codex Serena environment to smoke-test both MCP servers against a local page.
Context7's CLI version can be checked with `.codex/scripts/ctx7.ps1 --version`;
documentation lookups require access to Context7's hosted service.
