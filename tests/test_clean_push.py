"""Tests for the autofix step + post-push clean-tree assertion.

Runnable two ways:
- Direct script:   python tests/test_clean_push.py
- Pytest:          pytest tests/test_clean_push.py
"""

import subprocess
import tempfile
from pathlib import Path

from quil.agents import autofix_lint
from quil.orchestrator import _dirty_paths

from . import exceptions


def _run_in_repo(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _make_repo(root: Path) -> str:
    cwd = str(root)
    _run_in_repo(["git", "init", "-q"], cwd)
    _run_in_repo(["git", "config", "user.email", "t@example.com"], cwd)
    _run_in_repo(["git", "config", "user.name", "t"], cwd)
    (root / "src.py").write_text("x = 1\n")
    _run_in_repo(["git", "add", "."], cwd)
    _run_in_repo(["git", "commit", "-q", "-m", "init"], cwd)
    return cwd


def test_dirty_paths_clean_repo() -> None:
    with tempfile.TemporaryDirectory() as td:
        cwd = _make_repo(Path(td))
        assert _dirty_paths(cwd) == []


def test_dirty_paths_modified_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = _make_repo(root)
        (root / "src.py").write_text("x = 2\n")
        assert _dirty_paths(cwd) == ["src.py"]


def test_dirty_paths_untracked_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = _make_repo(root)
        (root / "new.py").write_text("y = 1\n")
        assert _dirty_paths(cwd) == ["new.py"]


def test_dirty_paths_multiple_states() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cwd = _make_repo(root)
        (root / "src.py").write_text("x = 2\n")
        (root / "new.py").write_text("y = 1\n")
        result = _dirty_paths(cwd)
        assert sorted(result) == ["new.py", "src.py"]


def test_autofix_skips_when_no_python_files() -> None:
    with tempfile.TemporaryDirectory() as td:
        result = autofix_lint(td, ["README.md", "data.json"])
        assert result.passed is True
        assert result.details.get("skipped") is True


def test_autofix_applies_isort_fix() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # ruff.toml lives outside any uv project, avoiding uv's
        # pyproject.toml `[project]` requirement. Enable I (isort) so
        # --fix actually rewrites imports.
        (root / "ruff.toml").write_text('[lint]\nselect = ["I"]\n')
        bad = "import pytest\nfrom codecs import BOM_UTF8\nfrom pathlib import Path\n"
        (root / "mod.py").write_text(bad)
        result = autofix_lint(str(root), ["mod.py"])
        assert result.passed is True, result.output
        fixed = (root / "mod.py").read_text()
        # After isort + format, stdlib imports come first, separated by
        # a blank line from third-party `pytest`.
        assert "from codecs import BOM_UTF8" in fixed
        assert fixed.index("from codecs") < fixed.index("import pytest"), fixed


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
