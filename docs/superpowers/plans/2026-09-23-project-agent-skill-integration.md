# Project Agent Skill Integration Implementation Plan

> **For agentic workers:** Implement this authorized documentation and tooling change inline. Keep existing unrelated worktree changes intact.

**Goal:** Integrate the fast-delivery development guidance as a discoverable Study Plan project skill with clear usage and maintenance rules.

**Architecture:** Put executable Agent guidance under `.agents/skills/`, keep the existing design-contract knowledge system under `docs/skills/`, and publish human-facing operating guidance under `docs/agent-skills.md`. Add a lightweight structural/link checker to CI so project skills remain discoverable and internally consistent.

**Tech Stack:** Markdown, Python 3.11+, PyYAML, GitHub Actions.

## Global Constraints

- Preserve the current uncommitted project changes; only add the integration artifacts and narrowly update CI.
- Do not modify application behavior, permissions, runtime skill registries, or production data.
- Preserve the source skill's risk controls while removing time-sensitive assumptions tied to the initial MVP stage.
- Do not run the application test suite for this documentation-only change.

---

### Task 1: Add the project development skill and focused references

**Files:**
- Create: `.agents/skills/study-plan-fast-development/SKILL.md`
- Create: `.agents/skills/study-plan-fast-development/references/triage-and-debug.md`
- Create: `.agents/skills/study-plan-fast-development/references/optimization.md`

- [ ] Write a concise skill entrypoint with project-specific discovery triggers, task framing, risk priorities, validation selection, and links to conditional references.
- [ ] Move detailed failure-budget and AI/performance evaluation practices into references loaded only for debugging or tuning work.
- [ ] Keep instructions stage-neutral and state that a skill changes guidance, not authorization.

### Task 2: Document use and maintenance

**Files:**
- Create: `docs/agent-skills.md`

- [ ] Document the skill's functions, suitable and unsuitable situations, automatic and explicit invocation, completed integration steps, and maintenance cadence.
- [ ] Explain the separation between `.agents/skills/` and the existing `docs/skills/` contract index.
- [ ] Define a review checklist and a small-change update process tied to observed failures or project-stage changes.

### Task 3: Add a structural maintenance gate

**Files:**
- Create: `tools/skills/check_agent_skills.py`
- Modify: `.github/workflows/ci.yml`

- [ ] Check that each project skill has valid YAML frontmatter, a unique kebab-case name matching its directory, a non-empty description, and resolvable local Markdown links.
- [ ] Add the checker as a separate CI step without changing the existing design-knowledge manifest checker.

### Task 4: Validate the integration artifacts

**Files:**
- Check: all files above.

- [ ] Run `python tools/skills/check_agent_skills.py` and require a successful result.
- [ ] Run `git diff --check` and inspect the resulting diff and status to confirm only intended files were added or modified.
- [ ] Do not run application tests; this change does not alter product code.
