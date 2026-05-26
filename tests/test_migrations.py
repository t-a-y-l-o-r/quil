"""Tests for orchestrator-driven Django migration generation.

Runnable two ways:
- Direct script:   python tests/test_migrations.py
- Pytest:          pytest tests/test_migrations.py
"""

import subprocess
from unittest.mock import MagicMock, patch

from . import exceptions
from quil.agents import (
    DJANGO_SETTINGS_FOR_MIGRATIONS,
    MIGRATION_TIMEOUT,
    SensorResult,
    make_migrations,
)
from quil.orchestrator import _apply_migrations


def _completed(rc: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a fake CompletedProcess for subprocess.run mocking."""
    mock = MagicMock()
    mock.returncode = rc
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


def test_make_migrations_invokes_manage_py_with_app_and_name() -> None:
    with patch("quil.agents.subprocess.run", return_value=_completed(0, "OK")) as run:
        result = make_migrations("/proj", "blueflow", "remove_pulse")

    assert result.passed is True
    assert result.details == {"rc": 0, "app": "blueflow", "name": "remove_pulse"}

    args, kwargs = run.call_args
    cmd = args[0]
    assert cmd[:4] == ["uv", "run", "python", "project/manage.py"]
    assert "makemigrations" in cmd
    assert "blueflow" in cmd
    assert "--name" in cmd
    assert cmd[cmd.index("--name") + 1] == "remove_pulse"
    assert kwargs["cwd"] == "/proj"
    assert kwargs["env"]["DJANGO_SETTINGS_MODULE"] == DJANGO_SETTINGS_FOR_MIGRATIONS


def test_make_migrations_failure_captures_combined_output() -> None:
    err = "ImproperlyConfigured: missing models"
    with patch(
        "quil.agents.subprocess.run", return_value=_completed(1, "noise\n", err)
    ):
        result = make_migrations("/proj", "blueflow", "x")

    assert result.passed is False
    assert result.details["rc"] == 1
    assert err in result.output
    assert "noise" in result.output


def test_make_migrations_timeout_returns_failure() -> None:
    timeout_exc = subprocess.TimeoutExpired(cmd=["x"], timeout=MIGRATION_TIMEOUT)
    with patch("quil.agents.subprocess.run", side_effect=timeout_exc):
        result = make_migrations("/proj", "blueflow", "x")

    assert result.passed is False
    assert result.details.get("timeout") is True
    assert str(MIGRATION_TIMEOUT) in result.output


def test_apply_migrations_returns_none_when_field_missing() -> None:
    assert _apply_migrations({}, "/proj") is None


def test_apply_migrations_returns_none_for_empty_array() -> None:
    assert _apply_migrations({"migrations": []}, "/proj") is None


def test_apply_migrations_returns_none_when_field_not_a_list() -> None:
    # Defensive: planner emits the wrong shape; treat as no-op rather than crash.
    assert _apply_migrations({"migrations": "blueflow"}, "/proj") is None


def test_apply_migrations_skips_entries_missing_app_or_name() -> None:
    # All entries malformed → no subprocess invocations, returns None.
    plan = {
        "migrations": [
            {"app": "blueflow"},
            {"name": "x"},
            "not-a-dict",
            {},
        ],
    }
    with patch(
        "quil.orchestrator.make_migrations", side_effect=AssertionError("called")
    ):
        assert _apply_migrations(plan, "/proj") is None


def test_apply_migrations_runs_each_valid_spec() -> None:
    plan = {
        "migrations": [
            {"app": "blueflow", "name": "a"},
            {"app": "core", "name": "b"},
        ],
    }
    pass_result = SensorResult(passed=True, output="ok", details={"rc": 0})
    with patch("quil.orchestrator.make_migrations", return_value=pass_result) as run:
        result = _apply_migrations(plan, "/proj")

    assert result is pass_result
    assert run.call_count == 2
    assert run.call_args_list[0].args == ("/proj", "blueflow", "a")
    assert run.call_args_list[1].args == ("/proj", "core", "b")


def test_apply_migrations_short_circuits_on_first_failure() -> None:
    plan = {
        "migrations": [
            {"app": "blueflow", "name": "a"},
            {"app": "core", "name": "b"},
        ],
    }
    fail = SensorResult(passed=False, output="boom", details={"rc": 1})
    with patch("quil.orchestrator.make_migrations", return_value=fail) as run:
        result = _apply_migrations(plan, "/proj")

    assert result is fail
    assert run.call_count == 1


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
