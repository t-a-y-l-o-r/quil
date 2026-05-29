"""Tests for orchestrator-side validation of plan path classification.

Runnable two ways:
- Direct script:   python tests/test_plan_validation.py
- Pytest:          pytest tests/test_plan_validation.py
"""

from quil.agents import restricted_path_globs
from quil.orchestrator import (
    _format_classification_feedback,
    _plan_referenced_paths,
    _validate_plan_classification,
)

from . import exceptions


def test_restricted_path_globs_loads_from_coder_settings() -> None:
    globs = restricted_path_globs()
    assert globs, "expected coder.json deny entries to produce globs"
    # Sanity: at least one glob should match a tests path.
    assert any(g.match("tests/foo.py") for g in globs)


def test_plan_referenced_paths_collects_from_all_fields() -> None:
    plan = {
        "affected_files": ["a.py", "b.py", "a.py"],
        "delete_files": ["c.py"],
        "plan_steps": [
            {"file": "d.py"},
            {"file": "a.py"},
            {"file": ""},
            {},
        ],
    }
    paths = _plan_referenced_paths(plan)
    assert paths == ["a.py", "b.py", "c.py", "d.py"]


def test_validate_clean_plan_returns_no_violations() -> None:
    plan = {
        "affected_files": ["quil/foo.py", "quil/bar.py"],
        "restricted_overrides": [],
    }
    assert _validate_plan_classification(plan) == []


def test_validate_catches_test_path_in_affected_files() -> None:
    plan = {
        "affected_files": ["quil/foo.py", "tests/test_foo.py"],
        "restricted_overrides": [],
    }
    assert _validate_plan_classification(plan) == ["tests/test_foo.py"]


def test_validate_allows_declared_override() -> None:
    plan = {
        "affected_files": ["quil/foo.py"],
        "restricted_overrides": [
            {"path": "tests/test_foo.py", "reason": "needs S101 fix"},
        ],
    }
    # Path is declared as override; does not appear in affected_files;
    # validator should report no violations.
    assert _validate_plan_classification(plan) == []


def test_validate_catches_path_referenced_only_in_steps() -> None:
    plan = {
        "affected_files": ["quil/foo.py"],
        "delete_files": [],
        "plan_steps": [
            {"step": 1, "file": "quil/foo.py", "description": "edit"},
            {"step": 2, "file": "tests/test_foo.py", "description": "test"},
        ],
        "restricted_overrides": [],
    }
    assert _validate_plan_classification(plan) == ["tests/test_foo.py"]


def test_validate_catches_pyproject_in_delete_files() -> None:
    plan = {
        "affected_files": [],
        "delete_files": ["pyproject.toml"],
        "restricted_overrides": [],
    }
    assert _validate_plan_classification(plan) == ["pyproject.toml"]


def test_validate_collapses_duplicates_across_fields() -> None:
    plan = {
        "affected_files": ["tests/test_foo.py"],
        "delete_files": ["tests/test_foo.py"],
        "plan_steps": [{"file": "tests/test_foo.py"}],
        "restricted_overrides": [],
    }
    assert _validate_plan_classification(plan) == ["tests/test_foo.py"]


def test_validate_handles_missing_fields_gracefully() -> None:
    # Plan with no affected_files / plan_steps / restricted_overrides at all.
    assert _validate_plan_classification({}) == []


def test_format_feedback_includes_each_violation() -> None:
    feedback = _format_classification_feedback(
        ["tests/a.py", "tests/b.py", "pyproject.toml"]
    )
    assert "tests/a.py" in feedback
    assert "tests/b.py" in feedback
    assert "pyproject.toml" in feedback
    assert "restricted_overrides" in feedback
    assert "Re-emit" in feedback



def _run_all() -> None:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERR   {fn.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        raise exceptions.Exit(failures)
    print(f"\n{len(tests)} test(s) passed")


if __name__ == "__main__":
    _run_all()
