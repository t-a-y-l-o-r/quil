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
    MIGRATION_TIMEOUT,
    PLANNER_TIMEOUT,
    CoderInvocation,
    SensorResult,
    autofix_lint,
    changed_paths,
    create_draft_pr,
    detect_override_violations,
    get_changed_files,
    get_diff,
    hash_paths,
    load_plan_json,
    make_migrations,
    restricted_path_globs,
    run_code_review,
    run_coder,
    run_lint,
    run_override_coder,
    run_planner,
    save_output,
    save_plan_json,
    snapshot_lint,
)
from quil.ci import (
    CIResult,
    TestReport,
    get_failed_logs,
    get_run_log,
    parse_test_output,
    wait_for_ci,
)
from quil.display import OutputWindow, WindowAwareHandler
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
    # Label the issue actually carries when this verdict is returned,
    # so the outer loop can pass the right from_label to the next
    # transition without assuming the happy path was taken.
    current_label: str = "agent-reviewing"


@dataclass
class IssueContext:
    """Shared orchestration state threaded through the attempt phases."""

    repo: str
    issue_number: int
    issue: dict
    plan: dict
    branch_name: str
    cwd: str
    log_dir: Path
    window: OutputWindow | None = None
    confirmed_overrides: list[str] = field(default_factory=list)


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

    plan, _, persistent = _run_planner_validated(
        issue_context,
        issue_number=issue_number,
        log_dir=log_dir,
        window=window,
    )

    if plan is None:
        click.echo("Planner produced no valid JSON plan.", err=True)
        sys.exit(1)

    plan_path = save_plan_json(issue_number, plan, log_dir)
    logger.info("Plan saved to %s", plan_path)
    if persistent:
        logger.error(
            "Planner left %d restricted path(s) misclassified after retry: %s",
            len(persistent),
            ", ".join(persistent),
        )

    if not no_approval:
        approved, confirmed_overrides = _gate_human_approval(
            plan, window=window, classification_warnings=persistent
        )
        if not approved:
            click.echo("Plan not approved.")
            sys.exit(1)
        click.echo("Plan approved.")
        if confirmed_overrides:
            click.echo(
                "Confirmed restricted overrides: " + ", ".join(confirmed_overrides)
            )
    else:
        window.deactivate()
        click.echo(_format_plan_summary(plan))
        if persistent:
            click.echo(
                "\n⚠ WARNING: planner did not classify these restricted "
                "paths as overrides (after one retry):",
                err=True,
            )
            for p in persistent:
                click.echo(f"  - {p}", err=True)


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
        IssueContext(
            repo=repo,
            issue_number=issue_number,
            issue=issue,
            plan=plan,
            branch_name=branch_name,
            cwd=cwd,
            log_dir=log_dir,
            window=window,
        ),
        attempt=1,
        feedback=feedback,
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
    plan, classification_warnings = _phase_plan(
        repo, issue_number, issue, log_dir, window=window
    )
    if plan is None:
        return

    approved, confirmed_overrides = _gate_human_approval(
        plan,
        window=window,
        classification_warnings=classification_warnings,
    )
    if not approved:
        _fail(
            repo,
            issue_number,
            "agent-planning",
            "Plan rejected by human.",
        )
        return

    branch_name = derive_branch_name(issue)
    logger.info("Branch: %s", branch_name)
    if confirmed_overrides:
        logger.info(
            "Restricted overrides confirmed by human: %s",
            ", ".join(confirmed_overrides),
        )

    pr_url = _phase_code_review_loop(
        IssueContext(
            repo=repo,
            issue_number=issue_number,
            issue=issue,
            plan=plan,
            branch_name=branch_name,
            cwd=str(Path.cwd()),
            log_dir=log_dir,
            window=window,
            confirmed_overrides=confirmed_overrides or [],
        ),
        max_attempts,
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


def _plan_referenced_paths(plan: dict) -> list[str]:
    """Collect every path referenced in a plan outside ``restricted_overrides``.

    Covers ``affected_files``, ``delete_files``, and the ``file`` field
    of each entry in ``plan_steps``. Order is preserved and duplicates
    are collapsed.
    """
    paths: list[str] = []
    seen: set[str] = set()

    def add(path: object) -> None:
        if not isinstance(path, str) or not path:
            return
        if path in seen:
            return
        seen.add(path)
        paths.append(path)

    for entry in plan.get("affected_files") or []:
        add(entry)
    for entry in plan.get("delete_files") or []:
        add(entry)
    for step in plan.get("plan_steps") or []:
        if isinstance(step, dict):
            add(step.get("file"))
    return paths


def _validate_plan_classification(plan: dict) -> list[str]:
    """Return paths that match a restricted glob but aren't declared as overrides.

    The Coder is denied edits to anything in :func:`restricted_path_globs`.
    Any such path that the Planner lists in ``affected_files`` /
    ``delete_files`` / ``plan_steps`` instead of in
    ``restricted_overrides`` will silently fail the run, so the
    orchestrator validates this up front.
    """
    globs = restricted_path_globs()
    if not globs:
        return []
    declared = {
        entry.get("path")
        for entry in plan.get("restricted_overrides") or []
        if isinstance(entry, dict) and entry.get("path")
    }
    violations: list[str] = []
    for path in _plan_referenced_paths(plan):
        if path in declared:
            continue
        if any(g.match(path) for g in globs):
            violations.append(path)
    return violations


def _format_classification_feedback(violations: list[str]) -> str:
    """Build a feedback string telling the Planner how to fix misclassified paths."""
    bulleted = "\n".join(f"  - {p}" for p in violations)
    return (
        "The previous plan placed the following restricted paths in "
        "`affected_files`, `delete_files`, or `plan_steps`. Each is denied "
        "to the Coder by default and MUST be moved into the "
        "`restricted_overrides` array with a one-line `reason`. Remove "
        "them from `affected_files`/`delete_files` and from any "
        "`plan_steps[].file` entry that targets them.\n\n"
        f"{bulleted}\n\n"
        "Re-emit the full plan JSON with these paths correctly classified."
    )


def _run_planner_validated(
    issue_context: str,
    *,
    issue_number: int,
    log_dir: Path,
    window: OutputWindow | None,
) -> tuple[dict | None, str, list[str]]:
    """Run the planner, validate restricted-path classification, retry once.

    Returns ``(plan, raw_output, persistent_violations)``. If the second
    attempt still misclassifies paths, the violations are returned so
    the caller can warn the human. The plan is still returned so the
    human can approve or reject it themselves.
    """

    def _invoke(attempt: int, feedback: str | None) -> tuple[dict | None, str]:
        stream_log = log_dir / f"issue-{issue_number}" / f"planner-stream-{attempt}.log"
        if window:
            window.start("planner", timeout=PLANNER_TIMEOUT)
        result = run_planner(
            issue_context,
            feedback=feedback,
            on_line=window.update_line if window else None,
            log_file=stream_log if window else None,
        )
        if window:
            window.stop()
        save_output(issue_number, "planner", attempt, result.raw_output, log_dir)
        return result.plan, result.raw_output

    plan, raw = _invoke(attempt=1, feedback=None)
    if plan is None:
        return None, raw, []

    violations = _validate_plan_classification(plan)
    if not violations:
        return plan, raw, []

    logger.warning(
        "Planner misclassified %d restricted path(s); re-prompting: %s",
        len(violations),
        ", ".join(violations),
    )
    feedback = _format_classification_feedback(violations)
    plan2, raw2 = _invoke(attempt=2, feedback=feedback)
    if plan2 is None:
        # Retry produced no valid JSON — fall back to the first plan and
        # surface the original violations to the human.
        return plan, raw, violations

    persistent = _validate_plan_classification(plan2)
    return plan2, raw2, persistent


def _phase_plan(
    repo: str,
    issue_number: int,
    issue: dict,
    log_dir: Path,
    *,
    window: OutputWindow | None = None,
) -> tuple[dict | None, list[str]]:
    """Run the Planner agent and return ``(plan, persistent_violations)``.

    ``persistent_violations`` is non-empty only if the planner failed
    to classify restricted paths correctly even after a re-prompt; the
    caller is responsible for warning the human at the approval gate.
    """
    logger.info("Starting Planner agent...")
    transition(repo, issue_number, "agent-ready", "agent-planning")

    issue_context = json.dumps(issue, indent=2)

    plan, _, persistent = _run_planner_validated(
        issue_context,
        issue_number=issue_number,
        log_dir=log_dir,
        window=window,
    )

    if plan is None:
        _fail(
            repo,
            issue_number,
            "agent-planning",
            "Planner produced no valid JSON plan.",
        )
        return None, []

    save_plan_json(issue_number, plan, log_dir)
    classification = plan.get("classification")
    logger.info("Plan received. Classification: %s", classification)
    if persistent:
        logger.error(
            "Planner left %d restricted path(s) misclassified after retry: %s",
            len(persistent),
            ", ".join(persistent),
        )
    return plan, persistent


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

    affected: list[str] = plan.get("affected_files", [])
    if affected:
        lines.append(f"  Files:        {len(affected)} affected")
        lines.extend(map(lambda x: f"                  {x}", affected))

    overrides = plan.get("restricted_overrides", [])
    if overrides:
        lines.append("")
        lines.append("  ⚠ Restricted overrides requested:")
        for o in overrides:
            path = o.get("path", "?") if isinstance(o, dict) else str(o)
            reason = o.get("reason", "") if isinstance(o, dict) else ""
            lines.append(f"    - {path}")
            if reason:
                lines.append(f"        reason: {reason}")

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

    risks: list[str] = plan.get("risks", [])
    if risks:
        lines.append("")
        lines.append("  Risks:")
        lines.extend(map(lambda x: f"    - {x}", risks))

    criteria: list[str] = plan.get("acceptance_criteria", [])
    if criteria:
        lines.append("")
        lines.append("  Acceptance:")
        lines.extend(map(lambda x: f"    - {x}" , criteria))

    return "\n".join(lines)


def _gate_human_approval(
    plan: dict,
    window: OutputWindow | None = None,
    classification_warnings: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Display the plan summary and gate the run on human approval.

    If the plan declares ``restricted_overrides``, the human is prompted
    to approve each one individually before being asked to approve the
    plan as a whole. Any rejection — per-file or overall — aborts the
    pipeline. Returns ``(approved, confirmed_overrides)`` where
    ``confirmed_overrides`` is the list of override paths the human
    explicitly authorized for editing.

    ``classification_warnings`` lists paths the orchestrator detected as
    restricted but the planner failed to declare in
    ``restricted_overrides``, even after a corrective re-prompt. They
    are surfaced to the human before approval so they can reject the
    plan or override-and-proceed knowingly.
    """
    summary = _format_plan_summary(plan)
    if classification_warnings:
        warning_lines = [
            "",
            "  ⚠ WARNING: planner did not classify these restricted "
            "paths as overrides (after one retry):",
        ]
        warning_lines.extend(f"    - {p}" for p in classification_warnings)
        warning_lines.append(
            "    The Coder will be denied edits to these paths. "
            "Reject the plan or accept knowing the run will likely fail."
        )
        summary = summary + "\n" + "\n".join(warning_lines)
    if window:
        window.show_plan(summary)
    else:
        click.echo(summary)

    confirmed: list[str] = []
    overrides = plan.get("restricted_overrides") or []
    for entry in overrides:
        path = entry.get("path") if isinstance(entry, dict) else None
        reason = entry.get("reason", "") if isinstance(entry, dict) else ""
        if not path:
            continue
        prompt = f"Allow edit to restricted path {path}?"
        if reason:
            prompt = f"{prompt}\n  reason: {reason}\n"
        if not click.confirm(prompt, default=False):
            click.echo(f"Override rejected for {path} — aborting.")
            if window:
                window.resume_layout()
            return False, []
        confirmed.append(path)

    approved = click.confirm("Approve this plan?")
    if window:
        window.resume_layout()
    if not approved:
        return False, []
    return True, confirmed


def _phase_code_review_loop(
    ctx: IssueContext,
    max_attempts: int,
) -> str | None:
    """Run the Coder/CI/Review loop. Returns the PR URL if approved."""
    plan_json = json.dumps(ctx.plan, indent=2)
    feedback: str | None = None
    pr_url: str | None = None
    next_from_label = "agent-planning"

    for attempt in range(1, max_attempts + 1):
        logger.info("=== Attempt %d/%d ===", attempt, max_attempts)

        verdict = _single_attempt(
            ctx,
            plan_json=plan_json,
            attempt=attempt,
            from_label=next_from_label,
            feedback=feedback,
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
                ctx.repo,
                ctx.issue_number,
                verdict.current_label,
                "agent-rejected",
            )
            msg = (
                f"Agent pipeline rejected after {max_attempts} "
                f"attempts.\n\nLast feedback:\n{feedback}"
            )
            comment_on_issue(ctx.repo, ctx.issue_number, msg)
            return None

        next_from_label = verdict.current_label

    return None


MAX_LINT_RETRIES = 2


def _run_git_streaming(
    cmd: list[str],
    *,
    cwd: str,
    window: OutputWindow | None,
    label: str = "git",
    check: bool = True,
) -> tuple[int, str]:
    """Run a git command, streaming stdout+stderr into the stream box.

    Falls back to plain ``subprocess.run`` (no capture) when no window is
    provided so non-TTY runs keep their normal behavior.

    Returns ``(returncode, full_output)``.
    """
    if window is None:
        result = subprocess.run(cmd, cwd=cwd, check=check)
        return result.returncode, ""

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\n")
        lines.append(line)
        window.update_line(label, line)
    rc = process.wait()
    output = "\n".join(lines)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd, output=output)
    return rc, output


def _checkout_branch(
    branch_name: str,
    cwd: str,
    window: OutputWindow | None = None,
) -> None:
    """Create or switch to the feature branch."""
    if window:
        window.start("git")
    try:
        rc, _ = _run_git_streaming(
            ["git", "checkout", "-b", branch_name, "develop"],
            cwd=cwd,
            window=window,
            check=False,
        )
        if rc != 0:
            # Branch already exists (e.g. retry) — switch to it
            _run_git_streaming(
                ["git", "checkout", branch_name],
                cwd=cwd,
                window=window,
                check=True,
            )
    finally:
        if window:
            window.stop()


def _apply_deletions(
    plan: dict,
    cwd: str,
    window: OutputWindow | None = None,
) -> list[str]:
    """Run ``git rm`` for files the plan marked for deletion.

    The coder has no Bash and cannot delete files. The plan declares
    ``delete_files`` and the orchestrator removes them here, after the
    coder writes content but before the commit phase stages everything.

    Returns the list of paths actually removed (existed in the working
    tree). Missing or already-deleted paths are skipped silently.
    """
    targets = plan.get("delete_files") or []
    removed: list[str] = []
    if not targets:
        return removed

    if window:
        window.start("git")
    try:
        for path in targets:
            full = Path(cwd) / path
            if not full.exists():
                logger.debug("delete_files: %s already absent, skipping", path)
                continue
            _run_git_streaming(
                ["git", "rm", "-f", path],
                cwd=cwd,
                window=window,
                check=True,
            )
            removed.append(path)
        if removed:
            logger.info(
                "Removed %d file(s) per plan.delete_files: %s",
                len(removed),
                ", ".join(removed),
            )
    finally:
        if window:
            window.stop()
    return removed


def _apply_migrations(
    plan: dict,
    cwd: str,
    window: OutputWindow | None = None,
) -> SensorResult | None:
    """Run ``makemigrations`` for each migration the plan requires.

    The coder has no Bash and cannot run ``manage.py``. The plan declares
    each needed migration as ``{"app": "...", "name": "..."}`` in the
    ``migrations`` array; this runs ``makemigrations`` for every entry
    before the commit phase so any autogenerated file is staged in the
    same commit as the model edits that triggered it.

    Returns ``None`` when the plan declares no migrations or only
    malformed entries. Returns the first failing ``SensorResult`` on
    error so the caller can surface it to the coder. Returns the last
    successful result when every entry passed (the boolean-only
    ``passed`` flag is what callers use; the details are logged).
    """
    specs = plan.get("migrations") or []
    if not isinstance(specs, list) or not specs:
        return None

    last: SensorResult | None = None
    for spec in specs:
        if not isinstance(spec, dict):
            logger.warning(
                "plan.migrations entry is not an object; skipping: %r",
                spec,
            )
            continue
        app = spec.get("app")
        name = spec.get("name")
        if not app or not name:
            logger.warning(
                "plan.migrations entry missing 'app' or 'name'; skipping: %r",
                spec,
            )
            continue

        logger.info("Running makemigrations for app=%s name=%s", app, name)
        if window:
            window.start("makemigrations", timeout=MIGRATION_TIMEOUT)
        try:
            result = make_migrations(cwd, app, name)
        finally:
            if window:
                window.stop()

        if result.passed:
            logger.info("makemigrations succeeded for %s/%s.", app, name)
            last = result
        else:
            logger.error(
                "makemigrations failed for %s/%s (rc=%s):\n%s",
                app,
                name,
                result.details.get("rc"),
                result.output[:500],
            )
            return result

    return last


def _commit_changes(
    plan: dict,
    cwd: str,
    window: OutputWindow | None = None,
) -> bool:
    """Stage and commit all changes with --no-verify.

    Returns True if a commit was created, False if there was nothing to commit.
    """
    if window:
        window.start("git")
    try:
        _run_git_streaming(
            ["git", "add", "-A"],
            cwd=cwd,
            window=window,
            check=True,
        )

        # Check if there's anything to commit (no output, just rc)
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
        _run_git_streaming(
            ["git", "commit", "--no-verify", "-m", message],
            cwd=cwd,
            window=window,
            check=True,
        )
        return True
    finally:
        if window:
            window.stop()


_PORCELAIN_PREFIX_LEN = 3  # `XY ` — two-char status code + space


def _dirty_paths(cwd: str) -> list[str]:
    """Return paths git considers modified, deleted, or untracked.

    Used to detect when a hook silently rewrote files between commit
    and push — the symptom is a non-empty working tree after the
    orchestrator believes the branch is fully synced with the remote.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    paths: list[str] = []
    for raw in result.stdout.splitlines():
        if len(raw) >= _PORCELAIN_PREFIX_LEN:
            paths.extend(r.strip() for r in raw[_PORCELAIN_PREFIX_LEN])
    return paths


def _push_branch(
    branch: str,
    cwd: str,
    window: OutputWindow | None = None,
) -> None:
    """Push the branch to origin, streaming git output.

    Uses ``--no-verify`` to skip pre-push hooks. Without this, a hook
    that auto-fixes files (e.g. ``ruff check --fix`` via pre-commit)
    will rewrite the working tree between commit and push, leaving
    uncommitted changes that disagree with what was pushed.
    """
    if window:
        window.start("git")
    try:
        _run_git_streaming(
            ["git", "push", "--no-verify", "-u", "origin", branch],
            cwd=cwd,
            window=window,
            check=True,
        )
    finally:
        if window:
            window.stop()


def _run_override_pass(
    ctx: IssueContext,
    *,
    attempt: int,
    lint_try: int,
    feedback: str | None,
) -> SensorResult | None:
    """Run the override coder pass and verify it stayed in scope.

    Returns ``None`` on success. If the override coder edited any path
    outside the human-approved list, the violations are logged and a
    failed ``SensorResult`` is returned so the outer attempt is marked
    as failed without committing the bad state.
    """
    pre_paths = changed_paths(ctx.cwd)
    pre_hashes = hash_paths(ctx.cwd, pre_paths)

    suffix = f"-lint{lint_try}" if lint_try > 0 else ""
    stream_log = (
        ctx.log_dir
        / f"issue-{ctx.issue_number}"
        / f"override-coder-stream-{attempt}{suffix}.log"
    )
    if ctx.window:
        ctx.window.start("override-coder", timeout=CODER_TIMEOUT)
    override_result = run_override_coder(
        ctx.plan,
        ctx.branch_name,
        ctx.confirmed_overrides,
        feedback,
        invocation=CoderInvocation(
            cwd=ctx.cwd,
            on_line=ctx.window.update_line if ctx.window else None,
            log_file=stream_log if ctx.window else None,
        ),
    )
    if ctx.window:
        ctx.window.stop()

    save_output(
        ctx.issue_number,
        "override-coder",
        attempt if lint_try == 0 else f"{attempt}-lint{lint_try}",
        override_result.raw_output,
        ctx.log_dir,
    )

    violations = detect_override_violations(
        pre_paths=pre_paths,
        pre_hashes=pre_hashes,
        cwd=ctx.cwd,
        approved=ctx.confirmed_overrides,
    )
    if violations:
        logger.error(
            "Override coder touched %d out-of-scope path(s): %s",
            len(violations),
            ", ".join(violations),
        )
        return SensorResult(
            passed=False,
            output=(
                "Override coder edited paths outside the human-approved "
                "list:\n" + "\n".join(violations)
            ),
            details={"override_violations": violations},
        )
    return None


def _code_and_lint(
    ctx: IssueContext,
    *,
    attempt: int,
    feedback: str | None,
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
    _checkout_branch(ctx.branch_name, ctx.cwd, window=ctx.window)

    # Snapshot pre-existing lint violations before the coder touches anything.
    # Only new violations (count increased per file+rule) will be failures.
    plan_files = ctx.plan.get("affected_files", [])
    existing_files = [f for f in plan_files if Path(f).exists()]
    baseline = snapshot_lint(ctx.cwd, existing_files) if existing_files else {}
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
            ctx.log_dir
            / f"issue-{ctx.issue_number}"
            / f"coder-stream-{attempt}-{lint_try}.log"
        )

        if ctx.window:
            ctx.window.start("coder", timeout=CODER_TIMEOUT)
        coder_result = run_coder(
            ctx.plan,
            ctx.branch_name,
            feedback,
            invocation=CoderInvocation(
                cwd=ctx.cwd,
                on_line=ctx.window.update_line if ctx.window else None,
                log_file=stream_log if ctx.window else None,
            ),
        )
        if ctx.window:
            ctx.window.stop()

        save_output(
            ctx.issue_number,
            "coder",
            attempt if lint_try == 0 else f"{attempt}-lint{lint_try}",
            coder_result.raw_output,
            ctx.log_dir,
        )

        if ctx.confirmed_overrides:
            override_result = _run_override_pass(
                ctx,
                attempt=attempt,
                lint_try=lint_try,
                feedback=feedback,
            )
            if override_result is not None:
                return override_result

        # Generate Django migrations the plan requires. Run before
        # autofix so any autogenerated file is also auto-fixed and
        # included in the same commit as the model edits.
        migration_result = _apply_migrations(ctx.plan, ctx.cwd, window=ctx.window)

        # Apply auto-fixable lint rules (I001 isort, etc.) before
        # committing so the commit already matches what any pre-push
        # hooks would produce — otherwise the hook rewrites the working
        # tree post-push and leaves uncommitted drift.
        autofix_targets = changed_paths(ctx.cwd)
        if autofix_targets:
            autofix_result = autofix_lint(ctx.cwd, sorted(autofix_targets))
            if not autofix_result.passed:
                logger.warning(
                    "ruff auto-fix exited non-zero (fix_rc=%s, format_rc=%s); "
                    "continuing — lint sensor will catch any remaining issues.",
                    autofix_result.details.get("fix_rc"),
                    autofix_result.details.get("format_rc"),
                )

        _apply_deletions(ctx.plan, ctx.cwd, window=ctx.window)
        _commit_changes(ctx.plan, ctx.cwd, window=ctx.window)

        # Surface migration failures via the same retry loop that
        # handles lint, so the coder can fix the underlying model
        # issues. Final failure short-circuits the function.
        if migration_result is not None and not migration_result.passed:
            if lint_try < MAX_LINT_RETRIES:
                logger.info(
                    "makemigrations failed — sending feedback to Coder "
                    "(retry %d/%d, no push/CI)...",
                    lint_try + 1,
                    MAX_LINT_RETRIES,
                )
                feedback = (
                    "Django makemigrations failed. The model changes are "
                    "inconsistent or invalid — fix them before proceeding. "
                    "Do NOT author the migration file yourself; the "
                    "orchestrator runs makemigrations after each "
                    "iteration.\n\n"
                    f"{migration_result.output[:1000]}"
                )
                continue
            return migration_result

        changed_files = get_changed_files(ctx.cwd)
        logger.info("Running lint on %d changed files...", len(changed_files))
        lint_result = run_lint(ctx.cwd, changed_files=changed_files, baseline=baseline)
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
    ctx: IssueContext,
    *,
    plan_json: str,
    attempt: int,
    from_label: str,
    feedback: str | None,
) -> Verdict:
    """Run one code + CI + review cycle. Returns a Verdict."""
    # --- Code + lint inner loop ---
    transition(ctx.repo, ctx.issue_number, from_label, "agent-coding")
    lint_result = _code_and_lint(
        ctx,
        attempt=attempt,
        feedback=feedback,
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
            current_label="agent-coding",
        )

    # --- Push + draft PR (first attempt) + CI ---
    logger.info("Pushing branch %s...", ctx.branch_name)
    _push_branch(ctx.branch_name, ctx.cwd, window=ctx.window)

    dirty = _dirty_paths(ctx.cwd)
    if dirty:
        msg = (
            "Working tree is not clean after push — committed code "
            "disagrees with the working tree. A hook likely rewrote "
            "files after commit. Dirty paths:\n  " + "\n  ".join(dirty)
        )
        logger.error(msg)
        raise RuntimeError(msg)

    # Open the draft PR before waiting for CI so that the
    # pull_request event triggers the workflow. On retries the
    # PR already exists and new pushes fire the synchronize event.
    pr_url: str | None = None
    if attempt == 1:
        logger.info("Creating draft PR to trigger CI...")
        pr_url = create_draft_pr(ctx.repo, ctx.branch_name, ctx.issue, ctx.plan)
        logger.info("Draft PR: %s", pr_url)

    transition(
        ctx.repo,
        ctx.issue_number,
        "agent-coding",
        "agent-ci-pending",
    )

    logger.info("Waiting for CI...")
    ci = wait_for_ci(ctx.repo, ctx.branch_name)

    # --- Parse test results from the Test workflow specifically ---
    test_report = TestReport()
    if ci.test_run_id:
        test_log = get_failed_logs(ctx.repo, ci.test_run_id)
        if not test_log:
            test_log = get_run_log(ctx.repo, ci.test_run_id)
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
                ctx.issue_number,
                "ci-logs",
                attempt,
                test_log,
                ctx.log_dir,
            )
    else:
        logger.warning("No Test workflow run found — cannot parse test results")

    # --- Code review (LLM — diff + plan only) ---
    transition(
        ctx.repo,
        ctx.issue_number,
        "agent-ci-pending",
        "agent-reviewing",
    )
    diff = get_diff(ctx.cwd)
    logger.info("Starting code review agent (attempt %d)...", attempt)
    review_stream_log = (
        ctx.log_dir / f"issue-{ctx.issue_number}" / f"review-stream-{attempt}.log"
    )

    if ctx.window:
        ctx.window.start("reviewer", timeout=CODE_REVIEW_TIMEOUT)
    review_result = run_code_review(
        diff=diff,
        plan_json=plan_json,
        on_line=ctx.window.update_line if ctx.window else None,
        log_file=review_stream_log if ctx.window else None,
    )
    if ctx.window:
        ctx.window.stop()

    save_output(
        ctx.issue_number,
        "code-review",
        attempt,
        review_result.raw_output,
        ctx.log_dir,
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
