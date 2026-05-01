"""CI run monitoring and test output parsing."""

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CI_WATCH_TIMEOUT = 900  # 15 minutes max wait for CI
CI_POLL_INTERVAL = 10  # seconds between polls when waiting for run to appear
CI_POLL_MAX_WAIT = 120  # seconds to wait for a run to appear after push


@dataclass
class TestReport:
    """Structured test results parsed from pytest console output."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    failed_tests: list[str] = field(default_factory=list)


def _run_gh(
    args: list[str],
    *,
    check: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _poll_for_run(
    repo: str,
    branch: str,
    *,
    workflow: str | None = None,
) -> int:
    """Poll until a CI run appears for the branch, then return its ID.

    When *workflow* is given (e.g. ``"test.yml"``), only runs from
    that workflow are considered.
    """
    deadline = time.monotonic() + CI_POLL_MAX_WAIT

    while True:
        cmd = [
            "run",
            "list",
            "--repo",
            repo,
            "--branch",
            branch,
            "--limit",
            "1",
            "--json",
            "databaseId,status",
        ]
        if workflow:
            cmd.extend(["--workflow", workflow])

        result = _run_gh(cmd, check=True)
        runs = json.loads(result.stdout)
        if runs:
            return runs[0]["databaseId"]

        if time.monotonic() >= deadline:
            label = f" (workflow={workflow})" if workflow else ""
            msg = (
                f"No CI runs found for branch {branch}{label} after {CI_POLL_MAX_WAIT}s"
            )
            raise RuntimeError(msg)

        logger.info(
            "No CI runs yet, retrying in %ds...",
            CI_POLL_INTERVAL,
        )
        time.sleep(CI_POLL_INTERVAL)


@dataclass
class CIResult:
    """Aggregated result from all CI workflow runs."""

    test_run_id: int | None = None
    lint_run_id: int | None = None
    test_passed: bool = True
    lint_passed: bool = True


def wait_for_ci(repo: str, branch: str) -> CIResult:
    """Wait for both Test and Lint CI runs on a branch to complete.

    Returns a ``CIResult`` with per-workflow run IDs and pass/fail
    status.  Test output should be parsed from ``test_run_id`` only.

    GitHub Actions may take several seconds to register workflow
    runs after a push, so this function polls until each run appears
    (up to CI_POLL_MAX_WAIT seconds) before watching them.
    """
    logger.info("Waiting for CI on branch %s...", branch)
    result = CIResult()

    for workflow, attr in [("test.yml", "test"), ("lint.yml", "lint")]:
        try:
            run_id = _poll_for_run(repo, branch, workflow=workflow)
        except RuntimeError:
            logger.warning("No %s run found for branch %s", workflow, branch)
            continue

        logger.info(
            "Found %s run %d, waiting for completion...",
            workflow,
            run_id,
        )
        _run_gh(
            [
                "run",
                "watch",
                str(run_id),
                "--repo",
                repo,
                "--exit-status",
            ],
            check=False,
            timeout=CI_WATCH_TIMEOUT,
        )

        passed = get_ci_status(repo, run_id)
        setattr(result, f"{attr}_run_id", run_id)
        setattr(result, f"{attr}_passed", passed)
        logger.info(
            "%s: %s (run %d)",
            workflow,
            "PASS" if passed else "FAIL",
            run_id,
        )

    return result


def get_ci_status(repo: str, run_id: int) -> bool:
    """Check if a CI run's steps actually passed.

    Our CI workflows use ``continue-on-error: true`` to avoid
    hard-failing on pre-existing baseline lint/test issues.  This
    means the run *conclusion* is always ``"success"``.  We detect
    actual step failures by checking for **failure-level annotations**
    on the check runs, which GitHub creates automatically when a
    ``continue-on-error`` step exits non-zero.

    TODO: Once baseline lint violations and test failures are resolved,
    remove ``continue-on-error: true`` from the workflow YAML files
    (.github/workflows/test.yml, .github/workflows/lint.yml) and
    simplify this function back to:
        return data.get("conclusion") == "success"
    """
    # Get jobs via the REST API — includes check_run_url for each job
    result = _run_gh(
        [
            "api",
            f"repos/{repo}/actions/runs/{run_id}/jobs",
        ],
        check=True,
    )
    data = json.loads(result.stdout)

    for job in data.get("jobs", []):
        check_run_url = job.get("check_run_url", "")
        if not check_run_url:
            continue
        check_run_id = check_run_url.rstrip("/").rsplit("/", 1)[-1]

        ann_result = _run_gh(
            [
                "api",
                f"repos/{repo}/check-runs/{check_run_id}/annotations",
            ],
            check=False,
        )
        if ann_result.returncode != 0:
            continue

        annotations = json.loads(ann_result.stdout)
        failures = [a for a in annotations if a.get("annotation_level") == "failure"]
        if failures:
            logger.info(
                "CI run %d: %d failure annotation(s) from "
                "continue-on-error step failures (job %s)",
                run_id,
                len(failures),
                job.get("name", "?"),
            )
            return False

    return True


def get_failed_logs(repo: str, run_id: int) -> str:
    """Get the failed step logs from a CI run.

    Returns LLM-sized output from only the failed steps.
    Falls back to empty string if no failed logs available.
    """
    result = _run_gh(
        [
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--log-failed",
        ],
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        logger.warning(
            "Could not retrieve failed logs for run %d",
            run_id,
        )
        return ""

    return result.stdout


def get_run_log(repo: str, run_id: int) -> str:
    """Get the full log for a CI run.

    Used when ``--log-failed`` returns nothing (common with
    ``continue-on-error: true`` steps).
    """
    result = _run_gh(
        [
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--log",
        ],
        check=False,
    )
    return result.stdout


_GH_LOG_PREFIX = re.compile(
    r"^[^\t]+\t[^\t]+\t\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?",
)

# pytest summary lines always begin with a real outcome word. Anchoring on
# this set prevents the regex from latching onto unrelated "<N> <word> in
# <time>s" lines earlier in the log (e.g. uv's "Resolved 84 packages in
# 2.18s"), which previously caused the summary to be parsed as
# total=84/passed=0/failed=0.
_PYTEST_OUTCOME_WORDS = (
    r"(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
)
_PYTEST_SUMMARY_RE = re.compile(
    rf"(\d+\s+{_PYTEST_OUTCOME_WORDS}(?:,\s*\d+\s+\w+)*)\s+in\s+[\d.]+s"
)


def _strip_gh_log_prefixes(output: str) -> str:
    """Strip ``gh run view --log`` line prefixes.

    Each line from ``gh run view --log`` is prefixed with
    ``<job><tab><step><tab><ISO-timestamp>``.  This strips that
    prefix so downstream parsers see raw pytest output.
    """
    return "\n".join(_GH_LOG_PREFIX.sub("", line) for line in output.splitlines())


def parse_test_output(output: str) -> TestReport:
    """Parse pytest's -q --tb=no output into a TestReport.

    Expects output shaped like:
        FAILED path/to/test.py::test_name
        FAILED path/to/test.py::test_other - Error...
        68 failed, 249 passed, 4 skipped, 25 xfailed in 11.52s

    Also handles ``gh run view --log`` output where each line
    is prefixed with ``<job><tab><step><tab><timestamp>``.

    This replaces pytest-json-report, which is incompatible with
    pytest-xdist (xdist workers crash serializing Django WSGIRequest
    objects through execnet when tests fail).
    """
    output = _strip_gh_log_prefixes(output)

    failed_tests = [
        match.group(1)
        for match in re.finditer(
            r"^FAILED\s+(\S+)",
            output,
            re.MULTILINE,
        )
    ]

    # Parse the summary line: "68 failed, 249 passed, ..."
    total = 0
    passed = 0
    failed = 0
    errors = 0

    # Take the last match: pytest summary always sits at the end of the
    # log, and a single CI step may emit multiple invocations.
    summary_matches = list(_PYTEST_SUMMARY_RE.finditer(output))
    if summary_matches:
        summary_text = summary_matches[-1].group(1)
        for count_match in re.finditer(r"(\d+)\s+(\w+)", summary_text):
            count = int(count_match.group(1))
            label = count_match.group(2)
            if label == "passed":
                passed = count
            elif label == "failed":
                failed = count
            elif label in ("error", "errors"):
                errors = count
            total += count

    return TestReport(
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        failed_tests=failed_tests,
    )
