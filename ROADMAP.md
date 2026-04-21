# Quil Roadmap
## Three-Agent System: Planner → Coder → Reviewer

> **Goal:** Consume GitHub issues from a project backlog and produce draft PRs with minimal human intervention.

---

## 1. System Overview

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│   PLANNER   │─────▶│    CODER    │─────▶│  REVIEWER   │
│             │      │             │      │             │
│ Reads issue │      │ Writes code │      │ Validates   │
│ Reads repo  │      │ Writes tests│      │ Approves or │
│ Emits plan  │      │ Commits     │      │ Rejects     │
└─────────────┘      └─────────────┘      └─────────────┘
       ▲                    ▲                    │
       │                    │                    │
       │              ┌─────┴─────┐              │
       │              │ REJECTION │◀─────────────┘
       │              │  LOOP     │
       │              └───────────┘
       │
  ┌────┴─────┐
  │  TICKET  │
  │  QUEUE   │
  │ (GitHub) │
  └──────────┘
```

### Flow Summary

1. A **trigger** (manual via `quil run <issue>`) pulls an eligible issue from GitHub.
2. The **Planner** reads the issue + repo state and produces a structured implementation plan.
3. A **human** reviews and approves the plan (all plans require approval).
4. The **Coder** receives the plan, creates a branch, writes code + tests, and commits.
5. The **orchestrator** runs local lint. If lint fails, the Coder gets fast feedback (up to 2 retries) without pushing.
6. On lint pass: push, open **draft PR** (triggers CI), wait for CI, then run **code review**.
7. The orchestrator assembles a deterministic verdict from sensors + review. Approve → PR ready for human review. Reject → back to Coder (max 3 full attempts).

---

## 2. Agent Definitions

### 2.1 Planner Agent

**Purpose:** Translate a GitHub issue into a concrete, scoped implementation plan that the Coder can execute without ambiguity.

#### Inputs
| Source | What |
|---|---|
| GitHub Issue | Title, body, labels, comments |
| Repository | CLAUDE.md, file tree, relevant source files, git log |
| Memory | Project memories, known test baseline (~68 failures), xfail patterns |

#### Process
1. Fetch issue via `gh issue view <number> --json title,body,labels,comments`
2. Cross-reference issue against current repo state (per CLAUDE.md rule: "cross reference the current state of the repo with the issue being examined")
3. Identify affected files by searching codebase (models, views, serializers, tests)
4. Classify ticket type → determines branch prefix (`feature/`, `bug/`, `chore/`)
5. Produce a structured plan document (see output format below)

#### Output — Plan Document (JSON)
```json
{
  "issue_number": 42,
  "issue_title": "...",
  "classification": "feature|bug|chore",
  "branch_name": "feature/issue-42-short-description",
  "affected_files": ["blueflow/views/asset.py", "blueflow/tests/test_asset.py"],
  "new_files": [],
  "plan_steps": [
    {"step": 1, "description": "...", "file": "...", "rationale": "..."}
  ],
  "test_strategy": "...",
  "risks": ["..."],
  "acceptance_criteria": ["..."],
  "estimated_complexity": "low|medium|high"
}
```

#### Done Criteria
- Plan is valid JSON matching the schema above
- Every `affected_file` exists in the repo (verified by glob)
- `branch_name` follows the convention in CLAUDE.md (`feature/`, `bug/`, `chore/`)
- At least one acceptance criterion is defined
- **ALL plans require human approval before proceeding** (enforced since `64671f6`)

#### Feedback Sources
- **Human:** Can approve, reject, or modify the plan before it reaches the Coder
- **Self-check:** Validates file paths exist, branch name format, JSON schema

---

### 2.2 Coder Agent

**Purpose:** Execute the plan — write code, write tests, and commit to a feature branch.

#### Inputs
| Source | What |
|---|---|
| Planner output | The plan document (JSON) |
| Repository | Full codebase access via Claude Code tools |
| CLAUDE.md | Coding conventions, architecture, test patterns |

#### Process
1. Create branch from `develop`: `git checkout -b <branch_name> develop`
2. For each `plan_step`, read the target file, make the change
3. Follow all CLAUDE.md conventions:
   - Plain function tests (no `class Test*`)
   - `APIClient` with `force_authenticate`
   - `_` for unused variables (not `dummy`)
   - No `from __future__ import annotations`
   - `[DEBUG]` prefix on any debug prints
4. Run `uv run ruff check . --fix` and `uv run ruff format .`
5. Commit with descriptive message (no co-author lines per CLAUDE.md)

#### Output
- A branch with one or more commits implementing the plan
- A summary of changes made (files modified, lines changed)

#### Done Criteria
- All plan steps addressed (or explicitly noted as deferred with rationale)
- `uv run ruff check .` passes with zero errors
- `uv run ruff format --check .` passes
- Code compiles (no syntax errors)
- Branch is committed and ready for review

#### Feedback Sources
- **Reviewer agent:** Rejection feedback with specific file:line references and instructions
- **Linter (Ruff):** Direct sensor — `select = ["ALL"]` provides comprehensive checks. Ruff output is already LLM-parseable.
- **Self-check:** Verifies all plan steps were addressed

#### Two-Tier Retry Loop (implemented)
- **Inner loop (lint):** If lint fails after the Coder runs, feedback is sent directly back to the Coder without pushing/CI/review. Up to 2 fast retries. Does not count toward max attempts.
- **Outer loop (full):** On rejection from Reviewer, Coder receives structured feedback, re-commits, and re-submits through the full pipeline. Maximum 3 full attempts — after that, the issue is labeled `agent-rejected` and the pipeline exits with code 1.
- Each attempt is a new commit (never amend)

---

### 2.3 Review Phase (implemented as hybrid: sensors + LLM)

**Note:** The original design had the Reviewer as a single LLM agent. The implementation splits it: lint, test, and CI checks are run **programmatically by the orchestrator**, and only code review is delegated to an LLM. The `_assemble_verdict()` function combines all signals deterministically. This removes LLM judgment from pass/fail sensor interpretation.

#### Sensor Pipeline (deterministic — `_single_attempt` in orchestrator)

**Phase 0: Local Lint (fast, pre-push)**
- `uv run ruff check <changed_files>` + `uv run ruff format --check <changed_files>`
- On failure → fast retry loop back to Coder (up to 2 retries, no push)
- Only changed files are checked (no baseline comparison needed)

**Phase 1: Push + Draft PR + CI**
1. Push branch to origin
2. Create draft PR (first attempt only — triggers `pull_request` CI event; retries trigger `synchronize`)
3. Poll for CI run (up to 120s for GitHub to register the workflow)
4. Wait for CI completion via `gh run watch`
5. Detect step failures via **check-run annotations** (needed because `continue-on-error: true` masks the run conclusion — see §5.4)
6. Parse test output from CI logs, compare against `~/.config/quil/baseline.json` — only NEW failures are blockers

**Phase 2: Code Review (LLM — `run_code_review` in agents.py)**
1. Read the diff: `git diff develop...HEAD`
2. Check against acceptance criteria from plan
3. Verify no security issues (OWASP top 10, especially in DRF serializers/views)
4. Verify no unintended changes outside plan scope
5. Returns structured JSON findings with severity levels

**Phase 3: Verdict Assembly (deterministic — `_assemble_verdict`)**
- Lint sensor failed → reject
- New test failures (not in baseline) → reject
- CI failed with no parseable test failures → reject (possible infra issue)
- Any blocker-severity code review finding → reject
- All clear → approve

#### Done Criteria
- Local lint passes (ruff check + format on changed files)
- No new test failures beyond known baseline (~88 tests)
- CI annotation check passes
- Zero blocker-severity findings in code review
- On approve: issue labeled `agent-pr-open`, comment posted with PR link

---

## 3. Orchestration & Tooling

### 3.1 Tools Required

#### Inside target repo (repo-level)

| Tool | Purpose | Status |
|---|---|---|
| `CLAUDE.md` | Agent guide / feedforward control | ✅ Done |
| `quil/prompts/conventions.md` | Machine-readable agent conventions | ✅ Done (covers §4.1–4.4) |
| `uv run ruff` | Lint + format sensor | ✅ Done |
| `uv run pytest` | Test sensor | ✅ Done |
| `conftest.py` fixtures | Test infrastructure | ✅ Done |
| `.github/workflows/test.yml` | CI test pipeline (PostgreSQL + xdist) | ✅ Done (uses `continue-on-error`, see §5.4) |
| `.github/workflows/lint.yml` | CI lint pipeline | ✅ Done (uses `continue-on-error`, see §5.4) |
| `~/.config/quil/baseline.json` | Known-failure baseline (~88 tests, ~764 lint) | ✅ Done — updated via `quil update-baseline` |
| Test output parsing | Machine-readable test results | ✅ Done — custom log parser (`parse_test_output`) instead of `pytest-json-report` (incompatible with xdist) |

#### Outside target repo (tooling layer)

| Tool | Purpose | Status |
|---|---|---|
| **Claude Code CLI** (`claude`) | Agent runtime for all three agents | ✅ Available |
| **GitHub CLI** (`gh`) | Issue fetching, PR creation, label management | ✅ Available |
| **Git** | Branch management, diffing, committing | ✅ Available |
| **Orchestrator** (`quil`) | Coordinates agents, manages state | ✅ Done — `quil/orchestrator.py` |
| **State store** | Track issue state transitions | ✅ Done — GitHub labels via `quil/state.py` |
| **Cron / scheduled trigger** | Automatic processing of `agent-ready` issues | ❌ Not yet — Phase 3 |

### 3.2 Orchestrator Design (implemented)

The orchestrator is a Python CLI (`quil/orchestrator.py`, entry point `quil`):

**Commands:**
- `quil run <issue_number>` — Full pipeline for one issue
- `quil list-eligible` — Show issues labeled `agent-ready`
- `quil setup-labels` — Create all `agent-*` labels on the repo (idempotent)
- `quil update-baseline` — Regenerate `baseline.json` from current test/lint state

**Pipeline flow (`quil run`):**

1. **Fetch** issue via `gh issue view`
2. **Dispatch** Planner: `claude --print -p "<prompt>" --allowedTools Read,Glob,Grep,Bash(git log:*)`
3. **Gate** on human approval (all plans, regardless of complexity)
4. **Code + lint inner loop:**
   - Dispatch Coder: `claude --print -p "<prompt>" --allowedTools Read,Glob,Grep,Edit,Write,Bash`
   - Run local lint on changed files
   - If lint fails → feed output back to Coder (up to 2 fast retries, no push)
5. **Push** branch, **create draft PR** (first attempt only, triggers CI)
6. **Wait for CI** (polls up to 120s for run to appear, then watches completion)
7. **Detect CI status** via check-run annotations (not run conclusion, due to `continue-on-error`)
8. **Dispatch** code review: `claude --print -p "<prompt>" --allowedTools Read,Glob,Grep`
9. **Assemble verdict** deterministically from lint + CI + test baseline + review findings
10. **On reject:** structured feedback back to Coder (max 3 full attempts)
11. **On approve:** label issue `agent-pr-open`, comment with PR link
12. **On exhaustion:** label issue `agent-rejected`, exit with code 1

#### State Transitions (tracked via GitHub labels)

```
agent-ready → agent-planning → agent-coding → agent-ci-pending → agent-reviewing → agent-pr-open
                                    ▲                                    │
                                    └────────── agent-rejected (max 3x) ─┘
                                    
agent-failed → (human intervention needed)
```

#### `gh run` Quick Reference for Orchestrator

```bash
gh run list --branch <branch> --limit 1 --json databaseId,status,conclusion
gh run watch <run-id> --exit-status        # Block until done, exit 1 on failure
gh run view <run-id> --log-failed          # ONLY failed step output (LLM-sized)
gh run view <run-id> --log --job <job-id>  # Full log for one job
gh run rerun <run-id> --failed             # Re-run only failed jobs
gh run view <run-id> --json jobs           # Structured job data as JSON
```

### 3.3 Agent Invocation

Each agent runs as a **separate Claude Code session** with a tailored system prompt. This provides isolation — a Coder crash doesn't corrupt the Planner's context.

```bash
# Planner
claude --print -p "You are the Planner agent. $(cat planner-prompt.md) Issue: $(gh issue view 42 --json title,body,labels,comments)"

# Coder (with worktree isolation)
claude -p "You are the Coder agent. $(cat coder-prompt.md) Plan: $(cat /tmp/plan-42.json)"

# Reviewer
claude --print -p "You are the Reviewer agent. $(cat reviewer-prompt.md) Branch: feature/issue-42-foo Diff: $(git diff develop...feature/issue-42-foo)"
```

---

## 4. CLAUDE.md Refactoring — DONE

All items from §4.1–4.5 have been implemented in `quil/prompts/conventions.md`:

- ✅ **§4.1 Machine-readable conventions** — File mapping, commit rules, test rules
- ✅ **§4.2 Acceptance test commands** — Validation commands section
- ✅ **§4.3 Scope boundaries** — "Do Not Modify" section
- ✅ **§4.4 Error message guides** — "Common Sensor Failures" table with ruff codes and test errors
- ✅ **§4.5 Separation** — Agent conventions live in `quil/prompts/conventions.md`, not in CLAUDE.md. Each agent prompt instructs the LLM to read it.

### Next pass: Code style conventions to add to `conventions.md`

These style rules are not yet in the conventions file. Add on next update:

1. **Guard clauses over deep nesting** — Prefer early returns / `continue` / `raise` to flatten control flow. Avoid nested if/else chains when a guard can exit early.
2. **Docstrings explain the high level** — Docstrings should describe what the current scope does at a high level. Detailed comments within the body should focus on complex algorithm explanations or the *why* behind nonstandard choices. Don't restate what the code obviously does.
3. **Inline comments for gotchas only** — Single-line comments should be reserved for explaining surprising behavior, gotchas, or weirdness. Don't comment obvious code.
4. **Pipeline style over mutation** — Prefer chained transformations (comprehensions, `map`/`filter`, method chaining) over mutating variables in loops. Build new values rather than modifying existing ones in place.

Consider backing each rule with 1-2 concrete examples from the codebase (few-shot examples are significantly more effective than prose rules for LLM style adherence).

---

## 5. Infrastructure — Status

### 5.1 CI Pipeline — DONE

Two separate workflows in `.github/workflows/`:
- **`test.yml`** — PostgreSQL 16 service, `uv run pytest -n auto --tb=no -q --no-header -ra` (parallel via xdist)
- **`lint.yml`** — `uv run ruff check .` + `uv run ruff format --check .`

Both use `continue-on-error: true` to tolerate baseline issues. The orchestrator detects actual failures via check-run annotations (see §5.4). TODO comments in both files mark this for removal once baselines are clean.

### 5.2 Test Baseline — DONE

`~/.config/quil/baseline.json` tracks ~88 known test failures and ~764 lint violations. Updated via `quil update-baseline`. The orchestrator loads this in `_assemble_verdict()` to distinguish new regressions from baseline noise.

**Future: per-repo baselines.** The baseline is currently a single global file. When quil operates across multiple repos, baselines should be keyed per repo (e.g. `~/.config/quil/baselines/<owner>-<repo>.json`) and resolved automatically from `detect_repo()`.

### 5.3 Machine-Readable Test Output — DONE (different approach)

`pytest-json-report` was found to be incompatible with `pytest-xdist` (workers crash serializing Django `WSGIRequest` objects through execnet). Instead, `quil/ci.py:parse_test_output()` parses pytest's `-q --tb=no -ra` output directly, extracting `FAILED` lines and the summary counts. This works for local runs; CI log parsing has a known prefix issue (see §5.4).

---

## 5.4 Known Operational Issues

#### No live visibility into running agents

All agent invocations use `subprocess.run(..., capture_output=True)`, which buffers all output until the process exits (up to 10 minutes for the Coder). There is no streaming, progress indicator, or way to tell if an agent is working, stuck, or hung waiting for a permission prompt.

**Impact:** An agent that triggers a Claude CLI permission gate (most likely the Coder, which has unrestricted `Bash` access) will silently block until the subprocess timeout kills it. The operator has no way to distinguish "thinking" from "hung."

**Recommended fixes:**
- Add `--no-user-input` to all `claude` invocations to prevent permission prompts from blocking
- Switch from `subprocess.run` to `subprocess.Popen` with line-by-line stdout streaming
- Maintain a stable symlink at `quil/.logs/current.log` that always points to the active agent's log file. The orchestrator updates the symlink each time it starts a new agent phase (planner, coder, code-review). The operator runs `tail -f quil/.logs/current.log` in a second terminal (or split pane) for live visibility. This is the lightest approach — no new dependencies, no TUI framework, works with any terminal setup.
- Prefix streamed lines with the agent name (e.g. `[planner]`, `[coder]`) so the `tail -f` output is unambiguous when the symlink swaps between phases

#### ~~[PRIORITY] Local lint failure does not short-circuit the pipeline~~ — FIXED (PR #93)

Implemented two-tier retry: `_code_and_lint()` runs an inner loop (up to `MAX_LINT_RETRIES=2` fast retries) that feeds lint output back to the Coder without pushing. Only when lint passes does the pipeline proceed to push/CI/review. Lint retries do not count toward `max_attempts`.

#### ~~Pipeline exhaustion exits silently with code 0~~ — FIXED (PR #93)

`_run_pipeline` now prints `"Pipeline rejected after N attempt(s). See issue #X for details."` to stderr and calls `sys.exit(1)` when the loop returns `None`.

#### Coder suppresses lint violations instead of fixing them

Observed on issue #65: the Coder added `DJ001` to the global ruff ignore list in `pyproject.toml` and added blanket `per-file-ignores` for `asset.py` (7 rules), `asset_manager.py` (13 rules), and `fieldmap.py` (5 rules). This silences violations project-wide or file-wide rather than fixing the underlying code, and degrades lint coverage for all future changes to those files.

**Root cause:** The Coder optimizes for "make ruff exit 0" under retry pressure. `# noqa` and config edits are the fastest path. Nothing in the prompt or the lint sensor distinguished "lint passes because violations are fixed" from "lint passes because violations are suppressed."

**Mitigations applied (PR #105):**
- Added `pyproject.toml` to the "Do Not Modify" list in `conventions.md`
- Added explicit instruction in `coder.md`: "Do not suppress lint violations by adding `# noqa` comments or editing ruff config. Fix the underlying code instead."

**Still possible failure modes:**
- Coder could add `# noqa` to individual lines (prompt rule may not hold under retry pressure)
- Coder could add `# type: ignore` or other suppression mechanisms not covered by the rule

**Mitigations applied (PR #4) — CLI-level deny rules:**
Added `quil/settings/coder.json` with `permissions.deny` rules that block `Edit` and `Write` to `**/test/**`, `**/tests/**`, and `**/pyproject.toml`. Passed to the coder invocation via `--settings`. This enforces restrictions at the Claude CLI permission layer — the tool call is denied before execution, so no amount of prompt creativity can bypass it. The pyproject.toml guard sensor described below is no longer the critical path, but remains a useful defense-in-depth addition.

**Optional next step — pyproject.toml guard sensor (defense-in-depth):**
Add a check in the orchestrator that rejects any diff touching ruff config in `pyproject.toml`. An automated agent should never alter the project's lint rules — that's a human decision. Implementation: after `get_changed_files()`, if `pyproject.toml` is in the list, fail immediately and feed back: "You modified pyproject.toml. Revert your changes to that file and fix the lint violations in the code instead." This would catch changes made via `Bash` (e.g. `sed`) that bypass the Edit/Write deny rules.

#### Lint sensor flags baseline violations on touched files

The lint inner loop runs `ruff check` on all changed files, but many files (e.g. `asset.py`) have pre-existing baseline violations like `DJ001` (`null=True` on `CharField`). When the Coder edits a file for legitimate reasons, ruff flags these old violations as failures. The Coder can't fix them without going out of scope, so it loops until the lint retry cap is hit.

**Recommended fix — Planner snapshots per-file lint baseline:**

Have the Planner run `ruff check --output-format json` on the affected files as part of planning. The plan JSON would include a `lint_baseline` field:

```json
{
  "lint_baseline": {
    "blueflow/models/asset.py": ["DJ001:37", "DJ001:39", "ERA001:145"],
    "blueflow/views/asset.py": ["ARG001:22"]
  }
}
```

The lint sensor then diffs against this snapshot — any violation not in the plan's baseline is new. This is better than filtering against the global `baseline.json` because:
- It's scoped to exactly the files in play
- It's always fresh (captured at plan time, not periodically updated)
- Ruff with `--output-format json` gives file + line + rule per violation, so the diff is trivial
- No changes needed to the global baseline maintenance workflow

#### Pipeline failure comments bloat the planner prompt on retries

When the orchestrator posts failure comments on an issue (`"Agent pipeline failed..."`, `"Agent pipeline rejected after 3 attempts..."`), those comments are included in the issue JSON passed to the Planner on the next run. The rejection comment includes raw lint output, which can be large. This caused a Planner timeout (300s) on issue #65's third run — the prompt was significantly larger than the first run due to accumulated failure comments.

**Recommended fix:**
- Filter out comments matching `"Agent pipeline"` (or authored by the bot) before passing issue context to the Planner
- Alternatively, minimize what gets posted to the issue — post a short summary with a link to the log file instead of inline lint output

#### CI log parsing fails due to `gh run view --log` line prefixes

`parse_test_output()` expects raw pytest output (`^FAILED path::test`), but `gh run view --log` prefixes every line with `<job>\t<step>\t<timestamp>`. This means:

- `FAILED` lines are not matched (never at line start)
- The summary regex partially breaks — it found `84 total` but `0 failed, 0 passed` for a run that actually had `68 failed, 249 passed`
- `--log-failed` returns nothing because `continue-on-error` masks step conclusions as "success"

**Current behavior:** The orchestrator logs a warning (`Could not retrieve failed logs`) and falls back to the full log, which then fails to parse. The CI pass/fail signal still works via annotation detection, so regressions are caught — but the orchestrator cannot report *which specific tests failed* from CI back to the Coder on rejection.

**Impact:** Low for now. Local sensor feedback (lint + test) provides the Coder with failure details. The CI sensor acts as a binary gate, not a detailed feedback source. This becomes more important if the pipeline ever skips local test runs and relies solely on CI.

**Recommended fix (when needed):**
- Preprocess CI log lines by stripping the `<job>\t<step>\t<timestamp>` prefix before passing to `parse_test_output()`
- A single regex like `r'^[^\t]+\t[^\t]+\t\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?'` applied per line would recover the raw pytest output

#### Planner output is unreadable when displayed for human approval

The Planner agent's raw output — displayed by `_gate_human_approval()` — contains `--output-format json` response wrapping, escape codes, and other artifacts that make the plan JSON difficult to read. The human approval gate is the most critical touchpoint in the pipeline and the plan needs to be clearly legible.

**Recommended fixes:**
- Parse and pretty-print only the extracted plan JSON (from `extract_json()`) in `_gate_human_approval()`, not the raw agent output
- Strip ANSI escape codes before display
- Consider a summary view that shows key fields (classification, branch, steps, risks) in a human-friendly format rather than raw JSON

### 5.5 Future: Interactive Debugging

#### `--step` flag: breakpoints between pipeline stages

Add a `--step` flag to `quil run` that pauses for confirmation between each major stage. Currently only the plan has a human gate (`_gate_human_approval`). With `--step`, the operator could inspect state and decide to continue, retry, or abort at each boundary:

```
Planner done -> [inspect plan] -> Continue to Coder? [y/n]
Coder done   -> [inspect diff] -> Continue to push/CI? [y/n]
CI done      -> [inspect results] -> Continue to review? [y/n]
Review done  -> [inspect verdict] -> Approve PR? [y/n]
```

#### `quil replay`: re-run a single stage from saved state

A command like `quil replay --stage coder --issue 65 --attempt 2` that loads the saved plan output from `.logs/issue-65/planner-attempt-1.txt`, extracts the plan JSON, and re-invokes just the Coder. This avoids re-running the Planner (and waiting for human approval) when debugging Coder behavior. Same pattern for replaying just the code review from a saved diff.

Requires that `save_output()` captures enough state to reconstruct inputs for each stage — it already saves raw output, but the extracted/parsed artifacts (plan dict, diff text) should also be persisted.

#### Ctrl-C as debug interrupt

A single Ctrl-C should drop into a debug/inspection prompt rather than killing the pipeline. A double Ctrl-C (within a short window) or Ctrl-D should hard-kill. Options for implementing this:

**Option A: SIGINT handler with timer window (simplest)**

Register a custom `signal.signal(signal.SIGINT, handler)`. On first SIGINT, set a flag and print `"Ctrl-C received — entering debug mode. Press Ctrl-C again within 2s to kill."`. If a second SIGINT arrives within the window, re-raise as `KeyboardInterrupt` for a hard exit. Otherwise, drop into an interactive prompt where the operator can:
- View current stage and elapsed time
- Inspect the last agent's raw output
- Abort the pipeline gracefully (label issue `agent-failed`)
- Continue execution

This is the pattern used by Django's `runserver`, pytest, and many CLI tools.

**Option B: Threading + stdin monitor**

Run the agent subprocess in a background thread. The main thread monitors stdin. Ctrl-C sends SIGINT which the handler catches; Ctrl-D sends EOF on stdin which the main thread detects. This gives two distinct signals but is more complex — stdin monitoring in a thread alongside subprocess management requires careful coordination.

**Option C: Signal + `atexit` hybrid**

Use SIGINT for the debug interrupt. Use `atexit.register()` to ensure cleanup (label transitions, issue comments) happens regardless of exit path. This doesn't give a second distinct signal but ensures the pipeline never exits dirty.

#### Stall detection: kill subprocess, preserve branch state

If the Coder hasn't made a meaningful change (file edit, git commit) within N minutes, `process.terminate()` the subprocess and raise an exception. The orchestrator should NOT clean up or transition labels — leave the branch exactly as-is for human inspection.

**Failed experiment (PRs #96-98, reverted):** Attempted stdout-based stall detection using `Popen` with a reader thread. Failed because Claude CLI's `--print` mode buffers all output at the application level until completion — zero bytes reach stdout/stderr while the agent is working. Tried `stdbuf -oL` to force line buffering but it has no effect because the CLI is not a C/libc process (likely Node.js or Rust). Both stdout and stderr logs were empty despite the agent actively editing files on disk.

**Correct approach (not yet implemented):** Monitor filesystem/git activity instead of stdout. Poll `git status` or check file mtimes periodically. If files are changing, the agent is working. This requires no cooperation from the CLI's output buffering. The `Popen` streaming change would also help if paired with `--output-format stream-json`, which may use a different buffering strategy than `--print`.

**Recommendation:** Option A. It's battle-tested, requires ~15 lines of code, and the "double Ctrl-C to kill" pattern is intuitive to anyone who's used a terminal. Ctrl-D for kill is less reliable because the agent subprocess may have stdin and the EOF may not propagate cleanly.

#### Coder context waste: cold starts and CLAUDE.md bloat

Every agent invocation is a fresh `claude --print` subprocess. Each one auto-discovers and loads the full CLAUDE.md chain (global `~/.claude/CLAUDE.md` + project CLAUDE.md + memory files), hooks, LSP, etc. On lint retries this is especially wasteful — the Coder re-reads every file, re-parses the plan, and re-orients before making a small fix. Issue #65's lint retry took ~10 minutes, most of which was context rebuilding.

Two CLI flags address this directly:

**`--bare` for faster cold starts:**
Skips hooks, LSP, plugin sync, auto-memory, CLAUDE.md auto-discovery. Quil already provides curated context via the prompt templates — it doesn't need the CLI's auto-discovery loading the full CLAUDE.md chain on top. Use `--append-system-prompt` to inject `conventions.md` content explicitly. Trade-off: `--bare` also skips pre-commit hooks, but quil runs ruff as an explicit sensor step so this is fine.

**`--resume <session-id>` for lint retries (biggest win):**
Instead of spawning a fresh subprocess on lint retry, resume the Coder's existing session. The Coder retains its full context — which files it read, what it changed, what it was thinking — and just gets the lint feedback appended. No re-reading files, no re-orientation.

Implementation sketch in `agents.py`:
```python
import uuid

# First coder call — establish session
session_id = str(uuid.uuid4())
cmd = [
    "claude", "--print", "--bare",
    "--session-id", session_id,
    "--model", "sonnet",
    "--append-system-prompt", conventions_text,
    "-p", prompt,
    "--allowedTools", "Read", "Glob", "Grep", "Edit", "Write", "Bash",
    "--max-budget-usd", "10",
]

# Lint retry — resume same session
cmd = [
    "claude", "--print",
    "--resume", session_id,
    "-p", f"Lint failed. Fix these issues:\n{lint_output}",
]
```

`_code_and_lint()` in `orchestrator.py` would need to thread `session_id` through the loop — generate it once before the loop, pass to `run_coder()`, and reuse on retries.

**Model tiering:**
- Planner: Opus — deep reasoning for plan quality. To be hardcoded via `--model opus` in `run_planner()`.
- Coder: Sonnet — follows a plan, faster output. ✅ Already hardcoded via `--model sonnet` in `run_coder()`.
- Code Review: Haiku — lightweight validation against diff + plan. To be hardcoded via `--model haiku` in `run_code_review()`.
- Coder lint retries: Sonnet or Haiku + `--effort low` (mechanical fixes)
- **Future:** Make model selection configurable (CLI flag or config file) rather than hardcoded, so operators can tune cost/quality per stage.

**Future: Orchestrator-managed git lifecycle:**
The coder currently handles branch creation, committing, and ruff via `Bash(git:*)` and `Bash(ruff:*)`. Ideally the orchestrator would own this entirely — create the branch before invoking the coder, commit after it exits, and run lint as a sensor. This would let us remove Bash from the coder completely. The blocker is that the coder runs as a single `--print` subprocess: we can't interrupt it mid-session to commit incrementally. Solving this requires a start/stop strategy — either `--resume` to pause and re-enter the session, `--output-format stream-json` with an event-driven orchestrator, or breaking the plan into one coder invocation per step. Worth exploring once the `--resume`-based lint retry approach (above) is proven.

**Other speed wins identified (2026-04-18):**
- Stop watching `lint.yml` in CI — local lint is already authoritative, the orchestrator ignores CI lint results anyway. Removes one full CI poll cycle.
- Short-circuit on empty diff — if `get_changed_files()` is empty after the lint loop (e.g. coder timed out), skip push/CI/review entirely.

---

## 6. Risk & Limitations

| Risk | Mitigation |
|---|---|
| Agent writes insecure code (SQLi, XSS in DRF views) | Reviewer Phase 2 explicitly checks OWASP; Ruff `S` rules catch common issues |
| Agent misunderstands issue scope | Planner must produce acceptance criteria; human gates high-complexity plans |
| Infinite rejection loop | Hard cap at 3 Coder attempts, then escalate to human |
| Agent modifies migrations or settings | Scope boundaries in CLAUDE.md (§4.3); Reviewer checks diff scope |
| Test baseline drift | Periodically update `known_failures.txt`; track in CI |
| Cost / token burn | Start with low-complexity tickets only; monitor token usage per ticket |
| Draft PR noise | Use `agent-pr` label; create a filtered view for human reviewers |

---

## 7. Phased Rollout

### Phase 1 — Manual trigger, human-in-the-loop — IN PROGRESS
- ~~Build orchestrator script~~ ✅
- ~~Write agent prompts (planner, coder, reviewer)~~ ✅
- ~~Set up GitHub Actions CI~~ ✅
- ~~Create known-failure baseline~~ ✅
- ~~Add lint short-circuit loop~~ ✅
- Run on 3-5 `chore/` tickets manually — **active, first run completed on issue #65**
- Human approves every plan, reviews every PR
- **Remaining:** Resolve operational issues in §5.4 (agent visibility, planner output readability, CI log parsing)
- **Goal:** Validate the three-agent flow works end-to-end

### Phase 2 — Semi-automated
- Add `agent-ready` label workflow
- Consider auto-approve for low-complexity plans (currently all plans require human approval)
- Implement `Popen` streaming + `current.log` symlink for live agent visibility
- Fix CI log prefix stripping for detailed test failure feedback
- Remove `continue-on-error` from CI once baselines are clean
- **Goal:** Reduce human touchpoints to PR review only

### Phase 3 — Scheduled automation
- Cron trigger processes `agent-ready` issues nightly
- Dashboard/summary of agent activity
- Expand to `bug/` and `feature/` tickets
- **Goal:** Agent processes backlog overnight, humans review PRs in the morning

---

## 8. Example End-to-End Flow

```
1. Human labels issue #73 "Add endpoint for bulk asset tag assignment" as `agent-ready`
   $ quil run 73

2. Orchestrator fetches issue, dispatches Planner
   -> Labels: `agent-planning`
   -> Planner identifies: views/asset.py, serializers/asset.py, tests/test_asset.py
   -> Outputs plan with 4 steps, classification: "feature", complexity: "medium"

3. Human reviews plan in terminal, approves
   -> "Approve this plan? [y/N]: y"

4. Coder receives plan
   -> Creates branch: feature/73-bulk-asset-tag-assignment
   -> Implements bulk tag endpoint, writes 3 test functions, runs ruff, commits
   -> Labels: `agent-coding`

5. Orchestrator runs local lint -> PASS
   -> Pushes branch, creates draft PR (triggers CI)
   -> Labels: `agent-ci-pending`
   -> Polls for CI run, waits for completion
   -> Checks annotations -> no failures
   -> Labels: `agent-reviewing`

6. Code review agent checks diff against plan
   -> No blocker findings
   -> Verdict assembled: APPROVE

7. Orchestrator marks PR ready
   -> Labels issue: `agent-pr-open`
   -> Comments: "Agent pipeline approved. Draft PR ready for review: <url>"

8. Human reviews and merges (or requests changes)
```
