# Agent Conventions

Rules in this file are consumed by quil (Planner, Coder, Reviewer).

## File Mapping

When a change touches one domain, the affected files follow this pattern:

- Model changes → `<repo>/models/<model>.py`
- View changes → `<repo>/views/<view>.py`
- Serializer changes → `<repo>/serializers/<serializer>.py`
- Test for any file `<repo>/<module>/foo.py` → `<repo>/tests/test_<module>_foo.py`

## Commit Rules

- Never commit to `develop` or `main` — always branch first
- Branch prefix must match classification: `feature/`, `bug/`, `chore/`
- No co-author lines in commit messages

## Test Rules

- Plain functions only — no `class Test*`
- Use fixtures from conftest; never instantiate `APIClient` directly
- Use `force_authenticate`, not session/cookie auth
- `_` for unused unpacked variables (not `dummy` or `_dummy`)
- No `from __future__ import annotations`
- Known baseline failures exist (~88 tests) — do not treat as regressions

## Do Not Modify

These paths require human review. The Planner must declare any genuinely-needed edit in `restricted_overrides` so the human can approve it per-file; without that approval the Coder is denied edits to these paths:

- `<repo>/migrations/` — never edit migration files. The orchestrator runs `makemigrations` after the Coder finishes and stages the result; do not declare migration paths in `restricted_overrides` or any other plan field
- `project/settings/` — settings changes require human review
- `pyproject.toml` — never modify linter config, dependencies, or project metadata
- `conftest.py` (root) — shared test infrastructure; changes affect all tests
- `**/tests/**` — test files and shared test helpers; even adding a new test or fixing a lint violation in a test file requires an override entry
- Any file outside `<repo>/` without explicit plan approval

## Validation Commands (Orchestrator/Reviewer only)

These are run by the orchestrator and reviewer — **not** by the Coder agent. The Coder must not run lint or tests.

```bash
uv run ruff check .              # Must exit 0
uv run ruff format --check .     # Must exit 0
DJANGO_SETTINGS_MODULE=project.settings.test uv run pytest <changed_test_files> -v  # No new failures
```

## Common Sensor Failures

| Ruff Code | Meaning | Fix |
|---|---|---|
| `S101` | `assert` used outside tests | Move to a test file or use a conditional `raise` |
| `N802` | Function name not lowercase | Rename the function and update all call sites |
| `ERA001` | Commented-out code | Delete the dead code |
| `ARG001` | Unused function argument | Prefix with `_` or remove if safe |
| `F821` | Undefined name | Add the missing import or define the variable |

| Test Error | Meaning | Fix |
|---|---|---|
| `waffle switch "core" not active` | Test hit an API without the waffle switch | Use a client fixture (`auth_client`, etc.) — they auto-enable the switch |
| `relation "X" does not exist` | Missing migration or wrong DB state | Run `pytest --create-db` to rebuild, or check migration dependencies |
