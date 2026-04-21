# Quil

Quil is a harness engineering CLI that automates GitHub issue resolution through a three-agent pipeline: **Planner**, **Coder**, and **Reviewer**. It consumes issues from a project backlog and produces draft PRs with minimal human intervention.

## Installation

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
make install
```

This installs `quil` as a system-wide CLI tool via `uv tool install`.

## Usage

### Full pipeline

Run the complete Planner -> Coder -> Reviewer pipeline for a GitHub issue:

```bash
quil run <issue_number>
```

The pipeline fetches the issue, generates an implementation plan (with human approval), writes code, runs lint and CI, performs code review, and opens a draft PR.

### Individual stages

Each stage can be run in isolation for debugging and iterative development:

```bash
# Run only the Planner — saves plan.json for downstream stages
quil plan <issue_number>

# Run only the Coder — loads a saved plan, creates branch, writes code
quil code <issue_number>

# Run only the Reviewer — loads plan, diffs branch, displays findings
quil review <issue_number>
```

### Utilities

```bash
# List issues labeled agent-ready
quil list-eligible

# Create all agent-* labels on the GitHub repo
quil setup-labels

# Regenerate the known-failure baseline from current test/lint state
quil update-baseline
```

## Pipeline stages

1. **Planner** reads the GitHub issue and repo state, then produces a structured JSON implementation plan. All plans require human approval.
2. **Coder** executes the plan on a feature branch with an inner lint retry loop (up to 2 fast retries before pushing).
3. **Reviewer** combines deterministic sensors (lint, tests, CI) with an LLM code review to produce a pass/fail verdict. On rejection, structured feedback goes back to the Coder (max 3 full attempts).

## State tracking

Issue progress is tracked via GitHub labels:

```
agent-ready -> agent-planning -> agent-coding -> agent-ci-pending -> agent-reviewing -> agent-pr-open
```

See [ROADMAP.md](ROADMAP.md) for full architecture details.
