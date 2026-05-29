"""Self-contained tests for quil.ci.parse_test_output.

Runnable two ways:
- Direct script:   python tests/test_ci.py
- Pytest (if installed): pytest tests/

No external dependencies — uses plain assert.
"""

from quil.ci import parse_test_output

from . import exceptions


def test_summary_with_decoy_uv_install_line() -> None:
    """The bug from issue #18: an earlier `<N> <word> in <time>s` line
    in the CI log (uv install output) used to capture the regex first,
    leaving pytest counts at zero.
    """
    log = (
        "test\tSet up uv\t2026-05-01T19:55:01.0000000Z + uv sync\n"
        "test\tSet up uv\t2026-05-01T19:55:02.0000000Z Resolved 84 packages in 2.18s\n"
        "test\tRun tests\t2026-05-01T19:55:03.0000000Z FAILED tests/test_a.py::test_one - boom\n"
        "test\tRun tests\t2026-05-01T19:55:03.0000000Z FAILED tests/test_b.py::test_two\n"
        "test\tRun tests\t2026-05-01T19:55:04.0000000Z 2 failed, 5 passed in 1.23s\n"
    )
    report = parse_test_output(log)
    assert report.failed == 2, report
    assert report.passed == 5, report
    assert report.total == 7, report
    assert report.failed_tests == [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ], report


def test_full_pytest_summary_with_all_outcome_words() -> None:
    """Real-world summary shape from issue #81 attempt 3."""
    log = (
        "test\tRun tests\t2026-05-01T19:56:37Z FAILED tests/x.py::test_a\n"
        "test\tRun tests\t2026-05-01T19:56:37Z FAILED tests/x.py::test_b\n"
        "test\tRun tests\t2026-05-01T19:56:37Z "
        "68 failed, 254 passed, 4 skipped, 25 xfailed, "
        "2 warnings, 16 errors in 59.44s\n"
    )
    report = parse_test_output(log)
    assert report.failed == 68, report
    assert report.passed == 254, report
    assert report.errors == 16, report
    # total sums every count word in the summary
    assert report.total == 68 + 254 + 4 + 25 + 2 + 16, report


def test_clean_run_no_failures() -> None:
    log = "test\tRun tests\t2026-05-01T20:00:00Z 42 passed in 0.42s\n"
    report = parse_test_output(log)
    assert report.passed == 42, report
    assert report.failed == 0, report
    assert report.total == 42, report
    assert report.failed_tests == [], report


def test_no_summary_line_returns_zeros() -> None:
    """If pytest never emitted a summary (e.g. infra crash), counts stay
    at zero but failed_tests can still be populated from FAILED lines.
    """
    log = (
        "test\tRun tests\t2026-05-01T20:00:00Z FAILED tests/x.py::test_a\n"
        "test\tRun tests\t2026-05-01T20:00:01Z [crash]\n"
    )
    report = parse_test_output(log)
    assert report.failed == 0, report
    assert report.passed == 0, report
    assert report.failed_tests == ["tests/x.py::test_a"], report


def test_multiple_pytest_invocations_use_last() -> None:
    """If a CI step ran pytest twice, the final summary is the source
    of truth.
    """
    log = (
        "test\tRun tests\t2026-05-01T20:00:00Z 5 passed in 0.10s\n"
        "test\tRun tests\t2026-05-01T20:00:05Z 1 failed, 9 passed in 0.50s\n"
    )
    report = parse_test_output(log)
    assert report.failed == 1, report
    assert report.passed == 9, report
    assert report.total == 10, report


def test_decoy_lines_with_outcome_lookalike_words_do_not_match() -> None:
    """`Resolved 84 packages in 2.18s` and similar lines must not match."""
    log = (
        "build\tInstall\t2026-05-01T20:00:00Z Resolved 84 packages in 2.18s\n"
        "build\tInstall\t2026-05-01T20:00:01Z Compiled 17 modules in 4.50s\n"
        "test\tRun tests\t2026-05-01T20:00:02Z 3 passed in 0.20s\n"
    )
    report = parse_test_output(log)
    assert report.passed == 3, report
    assert report.total == 3, report


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
    if failures:
        raise exceptions.Exit(failures)
    print(f"\n{len(tests)} test(s) passed")


if __name__ == "__main__":
    _run_all()
