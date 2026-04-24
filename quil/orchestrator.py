"""CLI entry point for the quil orchestrator."""

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import click

from quil.agents import (
    CODE_REVIEW_TIMEOUT,
    CODER_TIMEOUT,
    PLANNER_TIMEOUT,
    SensorResult,
    create_draft_pr,
    get_changed_files,
    get_diff,
    load_plan_json,
    push_branch,
    run_code_review,
    run_coder,
    run_lint,
    run_planner,
    snapshot_lint,
    save_output,
    save_plan_json,
)
from quil.display import OutputWindow, WindowAwareHandler
from quil.ci import (
    CIResult,
    TestReport,
    get_failed_logs,
    get_run_log,
    parse_test_output,
    wait_for_ci,
)
from quil.state import (
    comment_on_issue,
    derive_branch_name,
    detect_repo,
    fetch_issue,
    list_eligible,
    setup_labels,
    transition,
)

logger = logging.getLogger("quil")

BASELINE_PATH = Path.home() / ".config" / "quil" / "baseline.json"


@dataclass
class Verdict:
    """Deterministic verdict assembled from sensors + code review."""

    approved: bool
    reasons: list[str] = field(default_factory=list)
    ci_run_id: int | None = None
    findings: list[dict] = field(default_factory=list)
    pr_url: str | None = None


def _setup_logging(
    log_dir: Path,
    issue_number: int,
    window: OutputWindow | None = None,
) -> None:
    """Configure console + file logging."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"issue-{issue_number}" / "orchestrator.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
    )

    if window is not None:
        console: logging.Handler = WindowAwareHandler(window)
    else:
        console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(console)
    logger.addHandler(file_handler)


@click.group()
def cli() -> None:
    """Quil — harness engineering CLI."""


@cli.command("setup-labels")
def setup_labels_cmd() -> None:
    """Create all agent-* labels on the GitHub repo (idempotent)."""
    repo = detect_repo()
    click.echo(f"Setting up labels on {repo}...")
    setup_labels(repo)
    click.echo("Labels created successfully.")


@cli.command("list-eligible")
def list_eligible_cmd() -> None:
    """List issues labeled agent-ready."""
    repo = detect_repo()
    issues = list_eligible(repo)

    if not issues:
        click.echo("No eligible issues found.")
        return

    click.echo(f"{'#':<6} Title")
    click.echo("-" * 60)
    for issue in issues:
        click.echo(f"{issue['number']:<6} {issue['title']}")


@cli.command("plan")
@click.argument("issue_number", type=int)
@click.option(
    "--log-dir",
    type=click.Path(path_type=Path),
    default=Path(__file__).parent / ".logs",
    help="Directory for agent output logs.",
)
@click.option(
    "--no-approval",
    is_flag=True,
    default=False,
    help="Skip the interactive approval prompt.",
)
def plan_cmd(issue_number: int, *, log_dir: Path, no_approval: bool) -> None:
    """Run only the Planner stage for a GitHub issue."""
    window = OutputWindow()
    _setup_logging(log_dir, issue_number, window=window)
    window.activate()
    repo = detect_repo()

    logger.info("Fetching issue #%d from %s", issue_number, repo)
    issue = fetch_issue(repo, issue_number)
    logger.info("Issue: %s", issue["title"])

    logger.info("Starting Planner agent...")
    issue_context = json.dumps(issue, indent=2)
    stream_log = log_dir / f"issue-{issue_number}" / "planner-stream.log"
    window.start("planner")
    plan_result = run_planner(
        issue_context,
        on_line=window.update_line,
        log_file=stream_log,
    )
    window.stop()
    save_output(issue_number, "planner", 1, plan_result.raw_output, log_dir)

    if plan_result.plan is None:
        click.echo("Planner produced no valid JSON plan.", err=True)
        sys.exit(1)

    plan_path = save_plan_json(issue_number, plan_result.plan, log_dir)
    logger.info("Plan saved to %s", plan_path)

    if not no_approval:
        if not _gate_human_approval(plan_result.plan, window=window):
            click.echo("Plan not approved.")
            sys.exit(1)
        click.echo("Plan approved.")
    else:
        window.deactivate()
        click.echo(_format_plan_summary(plan_result.plan))


@cli.command("code")
@click.argument("issue_number", type=int)
@click.option(
    "--plan-file",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to a plan JSON file. Default: .logs/issue-{N}/plan.json",
)
@click.option(
    "--log-dir",
    type=click.Path(path_type=Path),
    default=Path(__file__).parent / ".logs",
    help="Directory for agent output logs.",
)
@click.option(
    "--max-lint-retries",
    default=2,
    help="Maximum lint retry attempts.",
)
@click.option(
    "--feedback",
    default=None,
    help="Initial feedback string to pass to the Coder.",
)
def code_cmd(
    issue_number: int,
    plan_file: Path | None,
    *,
    log_dir: Path,
    max_lint_retries: int,
    feedback: str | None,
) -> None:
    """Run only the Coder stage for a GitHub issue."""
    window = OutputWindow()
    _setup_logging(log_dir, issue_number, window=window)
    window.activate()

    try:
        plan = load_plan_json(issue_number, plan_file, log_dir)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc

    repo = detect_repo()
    issue = fetch_issue(repo, issue_number)
    branch_name = derive_branch_name(issue)
    cwd = str(Path.cwd())

    logger.info("Branch: %s", branch_name)

    lint_result = _code_and_lint(
        issue_number,
        plan,
        branch_name,
        attempt=1,
        feedback=feedback,
        cwd=cwd,
        log_dir=log_dir,
        window=window,
    )

    window.deactivate()

    if lint_result.passed:
        changed = get_changed_files(cwd)
        click.echo(f"\nCoder finished. Branch: {branch_name}")
        click.echo(f"Changed files ({len(changed)}):")
        for f in changed:
            click.echo(f"  {f}")
        click.echo("\nLint: PASS")
    else:
        click.echo("Lint: FAIL (after retries)", err=True)
        click.echo(lint_result.output[:500], err=True)
        sys.exit(1)


@cli.command("review")
@click.argument("issue_number", type=int)
@click.option(
    "--plan-file",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to a plan JSON file. Default: .logs/issue-{N}/plan.json",
)
@click.option(
    "--log-dir",
    type=click.Path(path_type=Path),
    default=Path(__file__).parent / ".logs",
    help="Directory for agent output logs.",
)
@click.option(
    "--base-branch",
    default="develop",
    help="Branch to diff against.",
)
def review_cmd(
    issue_number: int,
    plan_file: Path | None,
    *,
    log_dir: Path,
    base_branch: str,
) -> None:
    """Run only the Reviewer stage for a GitHub issue."""
    window = OutputWindow()
    _setup_logging(log_dir, issue_number, window=window)
    window.activate()

    try:
        plan = load_plan_json(issue_number, plan_file, log_dir)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc

    plan_json = json.dumps(plan, indent=2)
    cwd = str(Path.cwd())
    diff = get_diff(cwd, base=base_branch)

    if not diff.strip():
        window.deactivate()
        click.echo(
            f"No changes found between HEAD and {base_branch}. Nothing to review."
        )
        return

    logger.info("Starting code review agent...")
    stream_log = log_dir / f"issue-{issue_number}" / "review-stream.log"
    window.start("reviewer")
    review_result = run_code_review(
        diff=diff,
        plan_json=plan_json,
        on_line=window.update_line,
        log_file=stream_log,
    )
    window.stop()
    save_output(issue_number, "code-review", 1, review_result.raw_output, log_dir)

    if review_result.findings:
        findings_path = log_dir / f"issue-{issue_number}" / "review-findings.json"
        findings_path.parent.mkdir(parents=True, exist_ok=True)
        findings_path.write_text(json.dumps(review_result.findings, indent=2) + "\n")
        logger.info("Findings saved to %s", findings_path)

    window.deactivate()

    if not review_result.findings:
        click.echo("\nNo findings. Code looks good.")
        return

    click.echo(f"\n--- Code Review: {len(review_result.findings)} finding(s) ---")
    for finding in review_result.findings:
        severity = finding.get("severity", "info")
        file = finding.get("file", "?")
        line = finding.get("line", "?")
        msg = finding.get("message", "")
        click.echo(f"  [{severity}] {file}:{line} -- {msg}")

    blockers = [f for f in review_result.findings if f.get("severity") == "blocker"]
    click.echo(f"\nBlockers: {len(blockers)}")
    if blockers:
        click.echo("Verdict: REJECT")
        sys.exit(1)
    else:
        click.echo("Verdict: APPROVE (no blockers)")


@cli.command("update-baseline")
@click.option(
    "--test-timeout",
    default=300,
    help="Timeout in seconds for pytest run.",
)
@click.option(
    "--lint-timeout",
    default=60,
    help="Timeout in seconds for ruff check.",
)
def update_baseline_cmd(test_timeout: int, lint_timeout: int) -> None:
    """Regenerate baseline.json from current test and lint results."""
    click.echo("Running pytest to capture test failures...")
    test_result = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "-q",
            "--tb=no",
            "--no-header",
            "-ra",
        ],
        capture_output=True,
        text=True,
        timeout=test_timeout,
        check=False,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "project.settings.test"},
    )

    test_report = parse_test_output(test_result.stdout)
    click.echo(
        f"  {test_report.failed} failed, "
        f"{test_report.passed} passed, "
        f"{test_report.total} total"
    )

    click.echo("Running ruff check to capture lint violations...")
    lint_result = subprocess.run(
        ["uv", "run", "ruff", "check", ".", "--output-format", "json"],
        capture_output=True,
        text=True,
        timeout=lint_timeout,
        check=False,
    )

    lint_violations = 0
    lint_summary: dict[str, int] = {}
    if lint_result.stdout.strip():
        violations = json.loads(lint_result.stdout)
        lint_violations = len(violations)
        for v in violations:
            code = v.get("code", "unknown")
            lint_summary[code] = lint_summary.get(code, 0) + 1

    # Sort summary by count descending for readability
    lint_summary = dict(sorted(lint_summary.items(), key=lambda x: x[1], reverse=True))

    click.echo(f"  {lint_violations} lint violations across {len(lint_summary)} rules")

    now = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    baseline = {
        "_description": (
            "Known pre-existing failures and lint violations "
            f"as of {now}. The orchestrator uses this to "
            "distinguish new regressions from baseline noise. "
            "Update by running: quil update-baseline"
        ),
        "_generated_from": "pytest -q --tb=no + ruff check --output-format json",
        "test_failures": sorted(test_report.failed_tests),
        "lint_violations": lint_violations,
        "lint_summary": lint_summary,
    }

    BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
    click.echo(f"Baseline written to {BASELINE_PATH}")


@cli.command()
@click.argument("issue_number", type=int)
@click.option("--max-attempts", default=3, help="Max coder/reviewer cycles.")
@click.option(
    "--log-dir",
    type=click.Path(path_type=Path),
    default=Path(__file__).parent / ".logs",
    help="Directory for agent output logs.",
)
def run(
    issue_number: int,
    max_attempts: int,
    *,
    log_dir: Path,
) -> None:
    """Run the full agent pipeline for a GitHub issue."""
    window = OutputWindow()
    _setup_logging(log_dir, issue_number, window=window)
    window.activate()
    repo = detect_repo()

    try:
        _run_pipeline(
            repo,
            issue_number,
            max_attempts=max_attempts,
            log_dir=log_dir,
            window=window,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(1)
    except Exception as exc:
        logger.exception("Pipeline failed with unexpected error")
        _fail(repo, issue_number, None, f"Unexpected error: {exc}")
        sys.exit(1)
    finally:
        window.deactivate()


def _run_pipeline(
    repo: str,
    issue_number: int,
    *,
    max_attempts: int,
    log_dir: Path,
    window: OutputWindow | None = None,
) -> None:
    """Execute the Planner -> Coder -> CI -> Review pipeline."""
    issue = _phase_fetch(repo, issue_number)
    plan = _phase_plan(repo, issue_number, issue, log_dir, window=window)
    if plan is None:
        return

    if not _gate_human_approval(plan, window=window):
        _fail(
            repo,
            issue_number,
            "agent-planning",
            "Plan rejected by human.",
        )
        return

    branch_name = derive_branch_name(issue)
    logger.info("Branch: %s", branch_name)

    pr_url = _phase_code_review_loop(
        repo,
        issue_number,
        issue,
        plan,
        branch_name=branch_name,
        max_attempts=max_attempts,
        log_dir=log_dir,
        window=window,
    )

    if pr_url:
        _phase_approve_pr(repo, issue_number, pr_url)
    else:
        click.echo(
            f"\nPipeline rejected after {max_attempts} attempt(s). "
            f"See issue #{issue_number} for details.",
            err=True,
        )
        sys.exit(1)


def _phase_fetch(repo: str, issue_number: int) -> dict:
    """Fetch the issue from GitHub."""
    logger.info("Fetching issue #%d from %s", issue_number, repo)
    issue = fetch_issue(repo, issue_number)
    logger.info("Issue: %s", issue["title"])
    return issue


def _phase_plan(
    repo: str,
    issue_number: int,
    issue: dict,
    log_dir: Path,
    *,
    window: OutputWindow | None = None,
) -> dict | None:
    """Run the Planner agent and return the plan, or None on failure."""
    logger.info("Starting Planner agent...")
    transition(repo, issue_number, "agent-ready", "agent-planning")

    issue_context = json.dumps(issue, indent=2)
    stream_log = log_dir / f"issue-{issue_number}" / "planner-stream.log"

    if window:
        window.start("planner", timeout=PLANNER_TIMEOUT)
    plan_result = run_planner(
        issue_context,
        on_line=window.update_line if window else None,
        log_file=stream_log if window else None,
    )
    if window:
        window.stop()

    save_output(
        issue_number,
        "planner",
        1,
        plan_result.raw_output,
        log_dir,
    )

    if plan_result.plan is None:
        _fail(
            repo,
            issue_number,
            "agent-planning",
            "Planner produced no valid JSON plan.",
        )
        return None

    save_plan_json(issue_number, plan_result.plan, log_dir)
    classification = plan_result.plan.get("classification")
    logger.info("Plan received. Classification: %s", classification)
    return plan_result.plan


def _format_plan_summary(plan: dict) -> str:
    """Format a plan dict as a human-readable summary."""
    lines: list[str] = []

    issue = plan.get("issue_number", "?")
    title = plan.get("issue_title", "Untitled")
    lines.append(f"  Issue:        #{issue} — {title}")

    classification = plan.get("classification", "unknown")
    complexity = plan.get("estimated_complexity", "unknown")
    lines.append(f"  Type:         {classification} ({complexity} complexity)")

    branch = plan.get("branch_name", "unknown")
    lines.append(f"  Branch:       {branch}")

    affected = plan.get("affected_files", [])
    if affected:
        lines.append(f"  Files:        {len(affected)} affected")
        for f in affected:
            lines.append(f"                  {f}")

    steps = plan.get("plan_steps", [])
    if steps:
        lines.append("")
        lines.append("  Steps:")
        for s in steps:
            num = s.get("step", "?")
            desc = s.get("description", "")
            target = s.get("file", "")
            prefix = f"    {num}. "
            if target:
                lines.append(f"{prefix}{desc} [{target}]")
            else:
                lines.append(f"{prefix}{desc}")

    risks = plan.get("risks", [])
    if risks:
        lines.append("")
        lines.append("  Risks:")
        for r in risks:
            lines.append(f"    - {r}")

    criteria = plan.get("acceptance_criteria", [])
    if criteria:
        lines.append("")
        lines.append("  Acceptance:")
        for c in criteria:
            lines.append(f"    - {c}")

    return "\n".join(lines)


def _gate_human_approval(
    plan: dict,
    window: OutputWindow | None = None,
) -> bool:
    """Display a human-readable plan summary and prompt for approval."""
    summary = _format_plan_summary(plan)
    if window:
        window.show_plan(summary)
    else:
        click.echo(summary)
    approved = click.confirm("Approve this plan?")
    if window:
        window.resume_layout()
    return approved


def _phase_code_review_loop(
    repo: str,
    issue_number: int,
    issue: dict,
    plan: dict,
    *,
    branch_name: str,
    max_attempts: int,
    log_dir: Path,
    window: OutputWindow | None = None,
) -> str | None:
    """Run the Coder/CI/Review loop. Returns the PR URL if approved."""
    plan_json = json.dumps(plan, indent=2)
    feedback: str | None = None
    cwd = str(Path.cwd())
    pr_url: str | None = None

    for attempt in range(1, max_attempts + 1):
        logger.info("=== Attempt %d/%d ===", attempt, max_attempts)

        from_label = "agent-planning" if attempt == 1 else "agent-reviewing"
        verdict = _single_attempt(
            repo,
            issue_number,
            issue,
            plan,
            plan_json,
            branch_name=branch_name,
            attempt=attempt,
            from_label=from_label,
            feedback=feedback,
            cwd=cwd,
            log_dir=log_dir,
            window=window,
        )

        if verdict.pr_url:
            pr_url = verdict.pr_url

        if verdict.approved:
            return pr_url

        feedback = "\n".join(verdict.reasons)
        if verdict.findings:
            feedback += "\n\nCode review findings:\n"
            for finding in verdict.findings:
                feedback += (
                    f"- [{finding.get('severity')}] "
                    f"{finding.get('file', '?')}:"
                    f"{finding.get('line', '?')} "
                    f"{finding.get('message', '')}\n"
                )

        logger.info(
            "Rejected (attempt %d): %s",
            attempt,
            feedback[:200],
        )

        if attempt == max_attempts:
            transition(
                repo,
                issue_number,
                "agent-reviewing",
                "agent-rejected",
            )
            msg = (
                f"Agent pipeline rejected after {max_attempts} "
                f"attempts.\n\nLast feedback:\n{feedback}"
            )
            comment_on_issue(repo, issue_number, msg)
            return None

    return None


MAX_LINT_RETRIES = 2


def _checkout_branch(branch_name: str, cwd: str) -> None:
    """Create or switch to the feature branch."""
    result = subprocess.run(
        ["git", "checkout", "-b", branch_name, "develop"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        # Branch already exists (e.g. retry) — switch to it
        subprocess.run(
            ["git", "checkout", branch_name],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=True,
        )


def _commit_changes(plan: dict, cwd: str) -> bool:
    """Stage and commit all changes with --no-verify.

    Returns True if a commit was created, False if there was nothing to commit.
    """
    subprocess.run(
        ["git", "add", "-A"],
        cwd=cwd,
        check=True,
    )

    # Check if there's anything to commit
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=cwd,
        check=False,
    )
    if status.returncode == 0:
        logger.info("No changes to commit.")
        return False

    title = plan.get("issue_title", "Implement plan")
    message = f"{title}\n\nAutomated commit by quil coder agent."
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", message],
        cwd=cwd,
        check=True,
    )
    return True


def _code_and_lint(
    issue_number: int,
    plan: dict,
    branch_name: str,
    *,
    attempt: int,
    feedback: str | None,
    cwd: str,
    log_dir: Path,
    window: OutputWindow | None = None,
) -> SensorResult:
    """Run the Coder then lint, retrying lint failures locally.

    This inner loop gives the Coder fast feedback on lint issues
    without burning a full CI round trip.  Lint retries do NOT
    count toward the outer ``max_attempts`` limit.

    The orchestrator owns the git lifecycle: it creates/switches to the
    branch before invoking the coder, and commits changes after the
    coder finishes.  The coder has no Bash access.

    Returns the final lint SensorResult (passed or not).
    """
    _checkout_branch(branch_name, cwd)

    # Snapshot pre-existing lint violations before the coder touches anything.
    # Only new violations (count increased per file+rule) will be failures.
    plan_files = plan.get("affected_files", [])
    existing_files = [f for f in plan_files if Path(f).exists()]
    baseline = snapshot_lint(cwd, existing_files) if existing_files else {}
    if baseline:
        logger.info(
            "Lint baseline: %d pre-existing violations across %d files",
            sum(baseline.values()),
            len({k[0] for k in baseline}),
        )

    for lint_try in range(1 + MAX_LINT_RETRIES):
        suffix = f" (lint retry {lint_try})" if lint_try > 0 else ""
        logger.info(
            "Starting Coder agent (attempt %d%s)...",
            attempt,
            suffix,
        )
        stream_log = (
            log_dir
            / f"issue-{issue_number}"
            / f"coder-stream-{attempt}-{lint_try}.log"
        )

        if window:
            window.start("coder", timeout=CODER_TIMEOUT)
        coder_result = run_coder(
            plan,
            branch_name,
            feedback=feedback,
            cwd=cwd,
            on_line=window.update_line if window else None,
            log_file=stream_log if window else None,
        )
        if window:
            window.stop()

        save_output(
            issue_number,
            "coder",
            attempt if lint_try == 0 else f"{attempt}-lint{lint_try}",
            coder_result.raw_output,
            log_dir,
        )

        _commit_changes(plan, cwd)

        changed_files = get_changed_files(cwd)
        logger.info("Running lint on %d changed files...", len(changed_files))
        lint_result = run_lint(cwd, changed_files=changed_files, baseline=baseline)
        logger.info(
            "Lint: %s",
            "PASS" if lint_result.passed else "FAIL",
        )

        if lint_result.passed:
            return lint_result

        if lint_try < MAX_LINT_RETRIES:
            logger.info(
                "Lint failed — sending feedback to Coder "
                "(fast retry %d/%d, no push/CI)...",
                lint_try + 1,
                MAX_LINT_RETRIES,
            )
            feedback = (
                "Lint failed on your changes. Fix these issues "
                "before proceeding:\n\n"
                f"{lint_result.output[:1000]}"
            )

    return lint_result


def _single_attempt(
    repo: str,
    issue_number: int,
    issue: dict,
    plan: dict,
    plan_json: str,
    *,
    branch_name: str,
    attempt: int,
    from_label: str,
    feedback: str | None,
    cwd: str,
    log_dir: Path,
    window: OutputWindow | None = None,
) -> Verdict:
    """Run one code + CI + review cycle. Returns a Verdict."""
    # --- Code + lint inner loop ---
    transition(repo, issue_number, from_label, "agent-coding")
    lint_result = _code_and_lint(
        issue_number,
        plan,
        branch_name,
        attempt=attempt,
        feedback=feedback,
        cwd=cwd,
        log_dir=log_dir,
        window=window,
    )

    if not lint_result.passed:
        # Inner lint loop exhausted — reject without burning a
        # full CI round trip. The attempt still counts because
        # the Coder failed to produce clean code.
        logger.warning(
            "Lint still failing after %d retries, skipping CI/review.",
            MAX_LINT_RETRIES,
        )
        return Verdict(
            approved=False,
            reasons=[
                f"Lint failed after {MAX_LINT_RETRIES} "
                f"fast retries (no push/CI):\n"
                f"{lint_result.output[:500]}"
            ],
        )

    # --- Push + draft PR (first attempt) + CI ---
    logger.info("Pushing branch %s...", branch_name)
    push_branch(cwd, branch_name)

    # Open the draft PR before waiting for CI so that the
    # pull_request event triggers the workflow. On retries the
    # PR already exists and new pushes fire the synchronize event.
    pr_url: str | None = None
    if attempt == 1:
        logger.info("Creating draft PR to trigger CI...")
        pr_url = create_draft_pr(repo, branch_name, issue, plan)
        logger.info("Draft PR: %s", pr_url)

    transition(
        repo,
        issue_number,
        "agent-coding",
        "agent-ci-pending",
    )

    logger.info("Waiting for CI...")
    ci = wait_for_ci(repo, branch_name)

    # --- Parse test results from the Test workflow specifically ---
    test_report = TestReport()
    if ci.test_run_id:
        test_log = get_failed_logs(repo, ci.test_run_id)
        if not test_log:
            test_log = get_run_log(repo, ci.test_run_id)
        test_report = parse_test_output(test_log)
        logger.info(
            "Tests: %d passed, %d failed, %d errors (of %d total)",
            test_report.passed,
            test_report.failed,
            test_report.errors,
            test_report.total,
        )
        if test_log:
            save_output(
                issue_number,
                "ci-logs",
                attempt,
                test_log,
                log_dir,
            )
    else:
        logger.warning("No Test workflow run found — cannot parse test results")

    # --- Code review (LLM — diff + plan only) ---
    transition(
        repo,
        issue_number,
        "agent-ci-pending",
        "agent-reviewing",
    )
    diff = get_diff(cwd)
    logger.info("Starting code review agent (attempt %d)...", attempt)
    review_stream_log = (
        log_dir / f"issue-{issue_number}" / f"review-stream-{attempt}.log"
    )

    if window:
        window.start("reviewer", timeout=CODE_REVIEW_TIMEOUT)
    review_result = run_code_review(
        diff=diff,
        plan_json=plan_json,
        on_line=window.update_line if window else None,
        log_file=review_stream_log if window else None,
    )
    if window:
        window.stop()

    save_output(
        issue_number,
        "code-review",
        attempt,
        review_result.raw_output,
        log_dir,
    )

    # --- Assemble verdict (deterministic) ---
    verdict = _assemble_verdict(
        lint_result=lint_result,
        test_report=test_report,
        ci=ci,
        findings=review_result.findings,
    )
    verdict.pr_url = pr_url
    return verdict


def _load_baseline() -> dict:
    """Load the known-failure baseline from ~/.config/quil/baseline.json."""
    if not BASELINE_PATH.exists():
        logger.warning("No baseline file found at %s", BASELINE_PATH)
        return {}
    return json.loads(BASELINE_PATH.read_text())


def _assemble_verdict(
    *,
    lint_result: SensorResult,
    test_report: TestReport,
    ci: CIResult,
    findings: list[dict] | None,
) -> Verdict:
    """Build a deterministic verdict from sensors + code review."""
    reasons: list[str] = []
    baseline = _load_baseline()
    known_failures = set(baseline.get("test_failures", []))

    # Lint sensor (scoped to changed files only, so no baseline needed)
    if not lint_result.passed:
        reasons.append(
            f"Lint failed (ruff check rc={lint_result.details.get('check_rc')}, "
            f"ruff format rc={lint_result.details.get('format_rc')})\n"
            f"{lint_result.output[:500]}"
        )

    # Test sensor — only flag NEW failures not in baseline
    new_failures = [t for t in test_report.failed_tests if t not in known_failures]
    if new_failures:
        reasons.append(
            f"{len(new_failures)} NEW test failure(s) "
            f"(not in baseline):\n" + "\n".join(new_failures[:20])
        )
    if test_report.failed_tests and not new_failures:
        logger.info(
            "All %d test failures are in the known baseline — not blocking.",
            len(test_report.failed_tests),
        )

    # Test CI failed but no test failures parsed (infra issue)
    if not ci.test_passed and not test_report.failed_tests:
        reasons.append(
            f"Test CI failed (run {ci.test_run_id}) — no test "
            f"failures parsed, possible infra issue"
        )

    # Lint CI failed — informational only, local lint is authoritative
    if not ci.lint_passed:
        logger.info(
            "Lint CI failed (run %d) — local lint sensor is authoritative.",
            ci.lint_run_id,
        )

    # Code review findings
    blockers = [f for f in (findings or []) if f.get("severity") == "blocker"]
    reasons.extend(
        f"Blocker: {b.get('file', '?')}:"
        f"{b.get('line', '?')} — "
        f"{b.get('message', 'no message')}"
        for b in blockers
    )

    approved = len(reasons) == 0
    return Verdict(
        approved=approved,
        reasons=reasons,
        ci_run_id=ci.test_run_id,
        findings=findings or [],
    )


def _phase_approve_pr(
    repo: str,
    issue_number: int,
    pr_url: str,
) -> None:
    """Mark the draft PR as approved by the agent pipeline."""
    logger.info("Pipeline approved — PR ready for human review.")
    transition(
        repo,
        issue_number,
        "agent-reviewing",
        "agent-pr-open",
    )
    comment_on_issue(
        repo,
        issue_number,
        f"Agent pipeline approved. Draft PR ready for review: {pr_url}",
    )

    click.echo(f"\nDraft PR ready for review: {pr_url}")


def _fail(
    repo: str,
    issue_number: int,
    from_label: str | None,
    message: str,
) -> None:
    """Transition to agent-failed and comment on the issue."""
    logger.error("FAILED: %s", message)
    try:
        if from_label:
            transition(
                repo,
                issue_number,
                from_label,
                "agent-failed",
            )
        comment_on_issue(
            repo,
            issue_number,
            f"Agent pipeline failed: {message}",
        )
    except Exception:
        logger.exception(
            "Failed to update issue state during error handling",
        )


if __name__ == "__main__":
    cli()
