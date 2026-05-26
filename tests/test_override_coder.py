"""Tests for the restricted-overrides approval flow and override coder.

Runnable two ways:
- Direct script:   python tests/test_override_coder.py
- Pytest:          pytest tests/test_override_coder.py

No external dependencies — uses plain assert.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from . import exceptions
from quil.agents import (
    _build_override_settings,
    _glob_to_regex,
    _strip_denies_for_paths,
    changed_paths,
    detect_override_violations,
    hash_paths,
)
from quil.orchestrator import (
    _format_plan_summary,
    _gate_human_approval,
)


def test_glob_double_star_matches_root() -> None:
    """**/foo must match a top-level path with no parent dirs."""
    pat = _glob_to_regex("**/pyproject.toml")
    assert pat.match("pyproject.toml")
    assert pat.match("nested/pyproject.toml")
    assert not pat.match("notpyproject.toml")


def test_glob_double_star_directory_match() -> None:
    """**/tests/** must match anything under any tests dir, incl. top level."""
    pat = _glob_to_regex("**/tests/**")
    assert pat.match("tests/foo.py")
    assert pat.match("quil/tests/foo.py")
    assert pat.match("a/b/tests/c/d.py")
    assert not pat.match("not_tests/foo.py")
    assert not pat.match("foo.py")


def test_strip_denies_removes_only_matching_patterns() -> None:
    deny = [
        "Edit(**/tests/**)",
        "Edit(**/pyproject.toml)",
        "Edit(**/test/**)",
        "Write(**/tests/**)",
        "Write(**/pyproject.toml)",
        "Write(**/test/**)",
    ]
    stripped = _strip_denies_for_paths(deny, ["tests/foo.py"])
    assert "Edit(**/tests/**)" not in stripped
    assert "Write(**/tests/**)" not in stripped
    assert "Edit(**/pyproject.toml)" in stripped
    assert "Edit(**/test/**)" in stripped


def test_strip_denies_multiple_paths_drop_each_match() -> None:
    deny = [
        "Edit(**/tests/**)",
        "Edit(**/pyproject.toml)",
        "Write(**/tests/**)",
        "Write(**/pyproject.toml)",
    ]
    stripped = _strip_denies_for_paths(deny, ["tests/foo.py", "pyproject.toml"])
    assert stripped == []


def test_strip_denies_preserves_unmatched_entries() -> None:
    deny = ["Edit(**/tests/**)", "SomeOther(**/pyproject.toml)"]
    stripped = _strip_denies_for_paths(deny, ["tests/foo.py"])
    assert "Edit(**/tests/**)" not in stripped
    assert "SomeOther(**/pyproject.toml)" in stripped


def test_build_override_settings_writes_stripped_file() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
        dest = Path(fh.name)
    try:
        _build_override_settings(["tests/foo.py"], dest)
        loaded = json.loads(dest.read_text())
        deny = loaded["permissions"]["deny"]
        assert all("tests" not in d for d in deny)
        # Other denies (pyproject) are preserved
        assert any("pyproject" in d for d in deny)
    finally:
        dest.unlink(missing_ok=True)


def _run_in_repo(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _make_repo(root: Path) -> str:
    cwd = str(root)
    _run_in_repo(["git", "init", "-q"], cwd)
    _run_in_repo(["git", "config", "user.email", "t@example.com"], cwd)
    _run_in_repo(["git", "config", "user.name", "t"], cwd)
    (root / "src.py").write_text("print('hi')\n")
    _run_in_repo(["git", "add", "."], cwd)
    _run_in_repo(["git", "commit", "-q", "-m", "init"], cwd)
    return cwd


def test_changed_paths_includes_tracked_and_untracked() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = _make_repo(root)
        (root / "src.py").write_text("print('changed')\n")
        (root / "new.py").write_text("x = 1\n")
        paths = changed_paths(cwd)
        assert paths == {"src.py", "new.py"}


def test_detect_violations_catches_new_unapproved_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = _make_repo(root)
        # Pre-state: src.py modified.
        (root / "src.py").write_text("print('changed')\n")
        pre = changed_paths(cwd)
        pre_h = hash_paths(cwd, pre)
        # Override coder creates an unapproved file.
        (root / "rogue.py").write_text("evil = 1\n")
        violations = detect_override_violations(
            pre_paths=pre,
            pre_hashes=pre_h,
            cwd=cwd,
            approved=["tests/foo.py"],
        )
        assert violations == ["rogue.py"]


def test_detect_violations_catches_modified_pre_existing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = _make_repo(root)
        (root / "src.py").write_text("print('changed by main')\n")
        pre = changed_paths(cwd)
        pre_h = hash_paths(cwd, pre)
        # Override coder re-edits src.py — it isn't in approved list.
        (root / "src.py").write_text("print('changed by override')\n")
        violations = detect_override_violations(
            pre_paths=pre,
            pre_hashes=pre_h,
            cwd=cwd,
            approved=["tests/foo.py"],
        )
        assert violations == ["src.py"]


def test_detect_violations_allows_approved_edits() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = _make_repo(root)
        # Pre-state: src.py modified by main coder.
        (root / "src.py").write_text("print('main')\n")
        pre = changed_paths(cwd)
        pre_h = hash_paths(cwd, pre)
        # Override coder creates approved test file.
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        violations = detect_override_violations(
            pre_paths=pre,
            pre_hashes=pre_h,
            cwd=cwd,
            approved=["tests/test_x.py"],
        )
        assert violations == []


def test_format_plan_summary_includes_overrides() -> None:
    plan = {
        "issue_number": 42,
        "issue_title": "Fix lint",
        "classification": "bug",
        "branch_name": "bug/42-fix-lint",
        "affected_files": ["quil/foo.py"],
        "restricted_overrides": [
            {"path": "tests/test_foo.py", "reason": "needs S101 fix"},
            {"path": "pyproject.toml", "reason": "allow assert in tests"},
        ],
        "plan_steps": [],
    }
    summary = _format_plan_summary(plan)
    assert "Restricted overrides requested" in summary
    assert "tests/test_foo.py" in summary
    assert "needs S101 fix" in summary
    assert "pyproject.toml" in summary


def test_gate_approves_with_no_overrides() -> None:
    plan = {"issue_title": "x", "restricted_overrides": []}
    with patch("click.confirm", side_effect=[True]):
        approved, confirmed = _gate_human_approval(plan)
    assert approved is True
    assert confirmed == []


def test_gate_collects_per_file_then_overall() -> None:
    plan = {
        "issue_title": "x",
        "restricted_overrides": [
            {"path": "tests/a.py", "reason": "r1"},
            {"path": "tests/b.py", "reason": "r2"},
        ],
    }
    # Per-file y, y; overall y.
    with patch("click.confirm", side_effect=[True, True, True]):
        approved, confirmed = _gate_human_approval(plan)
    assert approved is True
    assert confirmed == ["tests/a.py", "tests/b.py"]


def test_gate_per_file_rejection_aborts_pipeline() -> None:
    plan = {
        "issue_title": "x",
        "restricted_overrides": [
            {"path": "tests/a.py", "reason": "r1"},
            {"path": "tests/b.py", "reason": "r2"},
        ],
    }
    # First per-file rejected → abort, no further prompts consumed.
    with patch("click.confirm", side_effect=[False]):
        approved, confirmed = _gate_human_approval(plan)
    assert approved is False
    assert confirmed == []


def test_gate_overall_rejection_aborts() -> None:
    plan = {
        "issue_title": "x",
        "restricted_overrides": [{"path": "tests/a.py", "reason": "r"}],
    }
    # Per-file y; overall n.
    with patch("click.confirm", side_effect=[True, False]):
        approved, confirmed = _gate_human_approval(plan)
    assert approved is False
    assert confirmed == []


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
