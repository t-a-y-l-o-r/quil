"""Agent invocation via Claude CLI subprocess and output parsing."""

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from quil.stream import StreamConfig, StreamingProcess

logger = logging.getLogger(__name__)

HARNESS_DIR = Path(__file__).parent
PROMPTS_DIR = HARNESS_DIR / "prompts"
SETTINGS_DIR = HARNESS_DIR / "settings"
DEFAULT_LOG_DIR = HARNESS_DIR / ".logs"

PLANNER_TIMEOUT = 300
CODER_TIMEOUT = 600
CODE_REVIEW_TIMEOUT = 300
LINT_TIMEOUT = 60
MIGRATION_TIMEOUT = 120

DJANGO_SETTINGS_FOR_MIGRATIONS = "project.settings.test"

MAX_PR_TITLE_LENGTH = 70

RUFF_SUFFIXES = {".py", ".pyi", ".ipynb"}


def _ruff_targets(files: list[str]) -> list[str]:
    """Filter to files ruff can actually parse.

    Ruff treats explicit non-Python paths as Python source and emits
    invalid-syntax errors, which the coder then tries to "fix."
    """
    return [f for f in files if Path(f).suffix in RUFF_SUFFIXES]


@dataclass
class PlanResult:
    raw_output: str
    plan: dict | None


@dataclass
class CoderResult:
    raw_output: str
    branch: str


@dataclass
class CodeReviewResult:
    raw_output: str
    findings: list[dict] | None


@dataclass
class SensorResult:
    passed: bool
    output: str
    details: dict = field(default_factory=dict)


def _recover_stdout(exc: subprocess.TimeoutExpired) -> str:
    """Extract any partial stdout captured before a timeout."""
    out = exc.stdout or b""
    return out.decode() if isinstance(out, bytes) else out


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)


def extract_json(text: str) -> dict | None:
    """Extract a JSON object from agent output.

    Handles both ```json fenced blocks and raw JSON.
    Strips ANSI escape codes before parsing.
    """
    text = strip_ansi(text)
    match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(match.group(1))

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(match.group(0))

    return None


def load_prompt(name: str) -> str:
    """Load a prompt template from quil/prompts/{name}.md."""
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text()


def save_output(
    issue_number: int,
    agent: str,
    attempt: int | str,
    content: str,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> Path:
    """Save raw agent output to the log directory."""
    issue_dir = log_dir / f"issue-{issue_number}"
    issue_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{agent}-attempt-{attempt}.txt"
    output_path = issue_dir / filename
    output_path.write_text(content)
    logger.debug("Saved %s output to %s", agent, output_path)
    return output_path


def save_plan_json(
    issue_number: int,
    plan: dict,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> Path:
    """Save parsed plan dict as standalone JSON for downstream stages."""
    issue_dir = log_dir / f"issue-{issue_number}"
    issue_dir.mkdir(parents=True, exist_ok=True)
    path = issue_dir / "plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n")
    logger.debug("Saved plan JSON to %s", path)
    return path


def load_plan_json(
    issue_number: int,
    plan_file: Path | None = None,
    log_dir: Path = DEFAULT_LOG_DIR,
) -> dict:
    """Load a previously-saved plan JSON.

    Raises FileNotFoundError if the plan file does not exist,
    and json.JSONDecodeError if the file is not valid JSON.
    """
    path = plan_file or (log_dir / f"issue-{issue_number}" / "plan.json")
    if not path.exists():
        msg = (
            f"Plan file not found: {path}\n"
            f"Run 'quil plan {issue_number}' first, or pass --plan-file."
        )
        raise FileNotFoundError(msg)
    return json.loads(path.read_text())


def run_planner(
    issue_context: str,
    *,
    feedback: str | None = None,
    on_line: Callable[[str, str], None] | None = None,
    log_file: Path | None = None,
) -> PlanResult:
    """Invoke the Planner agent to produce an implementation plan.

    ``feedback`` is rendered into the prompt's ``{feedback_section}``
    slot so a re-prompt after a failed validation can carry forward
    targeted corrections (e.g. paths that need to move into
    ``restricted_overrides``).
    """
    template = load_prompt("planner")
    conventions = load_prompt("conventions")
    feedback_section = (
        f"\n\n## Prior-Attempt Feedback\n{feedback}\n" if feedback else ""
    )
    prompt = (
        template.replace("{issue}", issue_context)
        .replace("{conventions}", conventions)
        .replace("{feedback_section}", feedback_section)
    )

    if on_line is not None:
        # Streaming path: use stream-json for real-time event output
        cmd = [
            "claude",
            "--print",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            "Read",
            "Glob",
            "Grep",
            "Bash(git log:*)",
            "--max-budget-usd",
            "5",
        ]
        proc = StreamingProcess(
            cmd,
            "planner",
            StreamConfig(
                log_file=log_file,
                on_line=on_line,
                timeout=PLANNER_TIMEOUT,
            ),
        )
        try:
            text = proc.run()
        except subprocess.TimeoutExpired:
            logger.warning("Planner timed out after %ds", PLANNER_TIMEOUT)
            return PlanResult(raw_output=proc._partial_output(), plan=None)

        plan = extract_json(text)
        return PlanResult(raw_output=text, plan=plan)

    # Non-streaming path: original subprocess.run behavior
    try:
        result = subprocess.run(
            [
                "claude",
                "--print",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--allowedTools",
                "Read",
                "Glob",
                "Grep",
                "Bash(git log:*)",
                "--max-budget-usd",
                "5",
            ],
            capture_output=True,
            text=True,
            timeout=PLANNER_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("Planner timed out after %ds", PLANNER_TIMEOUT)
        raw = _recover_stdout(exc)
        return PlanResult(raw_output=raw, plan=None)

    raw = result.stdout

    # --output-format json wraps output in a metadata envelope with the
    # actual content in the "result" field as a string.  Unwrap it before
    # attempting to extract the plan JSON.
    text = raw
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        envelope = json.loads(raw)
        if isinstance(envelope, dict) and "result" in envelope:
            text = envelope["result"]

    plan = extract_json(text)
    return PlanResult(raw_output=raw, plan=plan)


CODER_PLAN_KEYS = ("plan_steps", "affected_files", "acceptance_criteria")


@dataclass
class CoderInvocation:
    """Optional runtime hooks for a coder/override-coder run."""

    cwd: str | None = None
    on_line: Callable[[str, str], None] | None = None
    log_file: Path | None = None


def run_coder(
    plan: dict,
    branch_name: str,
    feedback: str | None = None,
    *,
    invocation: CoderInvocation | None = None,
) -> CoderResult:
    """Invoke the Coder agent to implement the plan.

    Only the fields the Coder actually needs are forwarded:
    plan_steps, affected_files, and acceptance_criteria. Metadata
    fields (issue_number, risks, estimated_complexity, etc.) are
    used by the orchestrator and human approval gate but are not
    actionable for the Coder.
    """
    inv = invocation or CoderInvocation()
    coder_plan = {k: plan[k] for k in CODER_PLAN_KEYS if k in plan}
    plan_json = json.dumps(coder_plan)

    template = load_prompt("coder")
    conventions = load_prompt("conventions")
    feedback_section = f"\n\n## Reviewer Feedback\n{feedback}" if feedback else ""
    prompt = (
        template.replace("{plan}", plan_json)
        .replace("{branch_name}", branch_name)
        .replace("{feedback_section}", feedback_section)
        .replace("{conventions}", conventions)
    )

    cmd = [
        "claude",
        "--print",
        "-p",
        prompt,
        "--model",
        "sonnet",
        "--settings",
        str(SETTINGS_DIR / "coder.json"),
        "--allowedTools",
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "--max-budget-usd",
        "10",
    ]

    if inv.on_line is not None:
        cmd.extend(["--output-format", "stream-json", "--verbose"])
        proc = StreamingProcess(
            cmd,
            "coder",
            StreamConfig(
                log_file=inv.log_file,
                on_line=inv.on_line,
                timeout=CODER_TIMEOUT,
                cwd=inv.cwd,
            ),
        )
        try:
            raw = proc.run()
        except subprocess.TimeoutExpired:
            logger.warning("Coder timed out after %ds", CODER_TIMEOUT)
            raw = proc._partial_output()
            return CoderResult(raw_output=raw, branch=branch_name)
    else:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CODER_TIMEOUT,
                cwd=inv.cwd,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning("Coder timed out after %ds", CODER_TIMEOUT)
            raw = _recover_stdout(exc)
            return CoderResult(raw_output=raw, branch=branch_name)
        raw = result.stdout

    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=inv.cwd,
        check=False,
    )
    branch = branch_result.stdout.strip() or branch_name

    return CoderResult(raw_output=raw, branch=branch)


def _glob_to_regex(glob: str) -> re.Pattern:
    """Convert a path glob (with ``**`` support) to a compiled regex."""
    parts: list[str] = []
    i = 0
    while i < len(glob):
        if glob[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif glob[i : i + 3] == "/**":
            parts.append("(?:/.*)?")
            i += 3
        elif glob[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            parts.append("[^/]")
            i += 1
        elif glob[i] in ".+()[]{}|^$\\":
            parts.append(re.escape(glob[i]))
            i += 1
        else:
            parts.append(glob[i])
            i += 1
    return re.compile("^" + "".join(parts) + "$")


_PERMISSION_ENTRY_RE = re.compile(r"^(\w+)\((.+)\)$")


def restricted_path_globs() -> list[re.Pattern]:
    """Return compiled regexes for every path glob the Coder is denied.

    The orchestrator uses this to validate that the Planner has correctly
    routed restricted-path edits through ``restricted_overrides`` rather
    than silently listing them in ``affected_files``.
    """
    base = json.loads((SETTINGS_DIR / "coder.json").read_text())
    deny = base.get("permissions", {}).get("deny", [])
    seen: set[str] = set()
    out: list[re.Pattern] = []
    for entry in deny:
        match = _PERMISSION_ENTRY_RE.match(entry)
        if not match:
            continue
        glob = match.group(2)
        if glob in seen:
            continue
        seen.add(glob)
        out.append(_glob_to_regex(glob))
    return out


def _strip_denies_for_paths(deny: list[str], approved_paths: list[str]) -> list[str]:
    """Drop deny entries whose glob matches any approved path.

    Claude Code permission denies always beat allows, so the only way
    to authorize an edit to a previously-denied path is to remove the
    matching deny pattern entirely. Other deny entries are preserved.
    """
    kept: list[str] = []
    for entry in deny:
        match = _PERMISSION_ENTRY_RE.match(entry)
        if not match:
            kept.append(entry)
            continue
        pattern = _glob_to_regex(match.group(2))
        if any(pattern.match(p) for p in approved_paths):
            continue
        kept.append(entry)
    return kept


def _build_override_settings(approved_paths: list[str], dest: Path) -> Path:
    """Write a temp settings file with denies stripped for approved paths."""
    base = json.loads((SETTINGS_DIR / "coder.json").read_text())
    perms = base.setdefault("permissions", {})
    perms["deny"] = _strip_denies_for_paths(perms.get("deny", []), approved_paths)
    dest.write_text(json.dumps(base, indent=2))
    return dest


def changed_paths(cwd: str) -> set[str]:
    """Return all paths changed in working tree vs HEAD, including untracked."""
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    ).stdout.splitlines()
    return {p for p in (*tracked, *untracked) if p.strip()}


def hash_paths(cwd: str, paths: set[str]) -> dict[str, str]:
    """Hash file contents at ``paths`` for later scope comparison."""
    out: dict[str, str] = {}
    for p in paths:
        full = Path(cwd) / p
        if full.is_file():
            out[p] = hashlib.sha1(full.read_bytes()).hexdigest()
    return out


def detect_override_violations(
    *,
    pre_paths: set[str],
    pre_hashes: dict[str, str],
    cwd: str,
    approved: list[str],
) -> list[str]:
    """Return paths the override coder touched that weren't approved.

    Catches three cases: new files added outside approved, modifications
    to files that were already changed pre-override, and deletions of
    pre-existing changes.
    """
    approved_set = set(approved)
    post = changed_paths(cwd)
    post_hashes = hash_paths(cwd, post)
    violations: list[str] = []
    violations.extend(p for p in post - pre_paths if p not in approved_set)
    violations.extend(p for p in pre_paths & post \
            if p not in approved_set and pre_hashes.get(p) != post_hashes.get(p))
    violations.extend(p for p in pre_paths - post if p not in approved_set)
    return sorted(set(violations))


def run_override_coder(
    plan: dict,
    branch_name: str,
    confirmed_overrides: list[str],
    feedback: str | None = None,
    *,
    invocation: CoderInvocation | None = None,
) -> CoderResult:
    """Invoke a scoped Coder pass for human-approved restricted paths.

    Only runs when ``confirmed_overrides`` is non-empty. Generates a
    temporary settings file with deny patterns stripped for approved
    paths and uses a prompt template that instructs the agent to touch
    only those files. The orchestrator is responsible for snapshotting
    the working tree before the call and validating the post-run diff
    via ``detect_override_violations``.
    """
    if not confirmed_overrides:
        return CoderResult(raw_output="", branch=branch_name)

    inv = invocation or CoderInvocation()
    coder_plan = {k: plan[k] for k in CODER_PLAN_KEYS if k in plan}
    plan_json = json.dumps(coder_plan)

    template = load_prompt("override_coder")
    conventions = load_prompt("conventions")
    feedback_section = f"\n\n## Reviewer Feedback\n{feedback}" if feedback else ""
    approved_block = "\n".join(f"- {p}" for p in confirmed_overrides)
    prompt = (
        template.replace("{plan}", plan_json)
        .replace("{branch_name}", branch_name)
        .replace("{conventions}", conventions)
        .replace("{approved_paths}", approved_block)
        .replace("{feedback_section}", feedback_section)
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="quil-override-settings-",
        delete=False,
    ) as fh:
        settings_path = Path(fh.name)
    _build_override_settings(confirmed_overrides, settings_path)

    cmd = [
        "claude",
        "--print",
        "-p",
        prompt,
        "--model",
        "sonnet",
        "--settings",
        str(settings_path),
        "--allowedTools",
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "--max-budget-usd",
        "10",
    ]

    try:
        if inv.on_line is not None:
            cmd.extend(["--output-format", "stream-json", "--verbose"])
            proc = StreamingProcess(
                cmd,
                "override-coder",
                StreamConfig(
                    log_file=inv.log_file,
                    on_line=inv.on_line,
                    timeout=CODER_TIMEOUT,
                    cwd=inv.cwd,
                ),
            )
            try:
                raw = proc.run()
            except subprocess.TimeoutExpired:
                logger.warning("Override coder timed out after %ds", CODER_TIMEOUT)
                raw = proc._partial_output()
                return CoderResult(raw_output=raw, branch=branch_name)
        else:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=CODER_TIMEOUT,
                    cwd=inv.cwd,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                logger.warning("Override coder timed out after %ds", CODER_TIMEOUT)
                raw = _recover_stdout(exc)
                return CoderResult(raw_output=raw, branch=branch_name)
            raw = result.stdout
    finally:
        with contextlib.suppress(OSError):
            settings_path.unlink()

    return CoderResult(raw_output=raw, branch=branch_name)


def run_code_review(
    diff: str,
    plan_json: str,
    *,
    on_line: Callable[[str, str], None] | None = None,
    log_file: Path | None = None,
) -> CodeReviewResult:
    """Invoke the code review agent to analyze the diff against the plan.

    This agent only performs code review — no lint or test analysis.
    Sensor checks are handled programmatically by the orchestrator.
    """
    template = load_prompt("code_review")
    prompt = template.replace("{plan}", plan_json).replace("{diff}", diff)

    if on_line is not None:
        cmd = [
            "claude",
            "--print",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            "Read",
            "Glob",
            "Grep",
            "--max-budget-usd",
            "5",
        ]
        proc = StreamingProcess(
            cmd,
            "reviewer",
            StreamConfig(
                log_file=log_file,
                on_line=on_line,
                timeout=CODE_REVIEW_TIMEOUT,
            ),
        )
        try:
            raw = proc.run()
        except subprocess.TimeoutExpired:
            logger.warning("Code review timed out after %ds", CODE_REVIEW_TIMEOUT)
            return CodeReviewResult(raw_output=proc._partial_output(), findings=None)

        parsed = extract_json(raw)
        findings = parsed.get("findings") if parsed else None
        return CodeReviewResult(raw_output=raw, findings=findings)

    try:
        result = subprocess.run(
            [
                "claude",
                "--print",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--allowedTools",
                "Read",
                "Glob",
                "Grep",
                "--max-budget-usd",
                "5",
            ],
            capture_output=True,
            text=True,
            timeout=CODE_REVIEW_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("Code review timed out after %ds", CODE_REVIEW_TIMEOUT)
        raw = _recover_stdout(exc)
        return CodeReviewResult(raw_output=raw, findings=None)

    raw = result.stdout
    parsed = extract_json(raw)
    findings = parsed.get("findings") if parsed else None
    return CodeReviewResult(raw_output=raw, findings=findings)


def get_changed_files(
    cwd: str,
    base: str = "develop",
) -> list[str]:
    """Get the list of files changed relative to the base branch."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    return [f for f in result.stdout.strip().splitlines() if f.strip()]


def autofix_lint(cwd: str, files: list[str]) -> SensorResult:
    """Apply ruff's safe auto-fixes and re-format the given files.

    Runs ``ruff check --fix`` (auto-fixable rules — most importantly
    I001 isort) and ``ruff format`` (style normalization) on the
    target files. The orchestrator calls this between the coder/
    override-coder passes and the commit step so the committed code
    already reflects everything a project's pre-push hook would
    auto-fix; otherwise the hook rewrites the working tree post-push
    and leaves uncommitted drift.

    Returns a ``SensorResult`` whose ``passed`` is True when both
    commands exited 0; details carry each command's return code.
    """
    targets = _ruff_targets(files)
    if not targets:
        return SensorResult(
            passed=True,
            output="=== ruff autofix ===\nNo Python files to fix.\n",
            details={"skipped": True},
        )

    fix = subprocess.run(
        ["uv", "run", "ruff", "check", "--fix", *targets],
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT,
        cwd=cwd,
        check=False,
    )
    fmt = subprocess.run(
        ["uv", "run", "ruff", "format", *targets],
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT,
        cwd=cwd,
        check=False,
    )
    output = (
        f"=== ruff check --fix ===\n{fix.stdout}{fix.stderr}\n"
        f"=== ruff format ===\n{fmt.stdout}{fmt.stderr}"
    )
    return SensorResult(
        passed=fix.returncode == 0 and fmt.returncode == 0,
        output=output,
        details={"fix_rc": fix.returncode, "format_rc": fmt.returncode},
    )


def make_migrations(cwd: str, app: str, name: str) -> SensorResult:
    """Run Django ``makemigrations`` for ``app`` with the given ``name``.

    The Coder has no Bash, so the orchestrator owns this step. Called
    between the coder pass and the commit phase so any autogenerated
    migration file is staged into the same commit as the model edits
    that triggered it.

    Returns a ``SensorResult``: ``passed`` is True iff makemigrations
    exited 0; ``output`` carries combined stdout/stderr for feedback.
    """
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": DJANGO_SETTINGS_FOR_MIGRATIONS,
    }
    cmd = [
        "uv",
        "run",
        "python",
        "project/manage.py",
        "makemigrations",
        app,
        "--name",
        name,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=MIGRATION_TIMEOUT,
            cwd=cwd,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return SensorResult(
            passed=False,
            output=(
                f"makemigrations timed out after {MIGRATION_TIMEOUT}s\n"
                f"{_recover_stdout(exc)}"
            ),
            details={"timeout": True, "app": app, "name": name},
        )
    output = (result.stdout + result.stderr).strip()
    return SensorResult(
        passed=result.returncode == 0,
        output=output,
        details={"rc": result.returncode, "app": app, "name": name},
    )


def snapshot_lint(
    cwd: str,
    files: list[str],
) -> dict[tuple[str, str], int]:
    """Snapshot current lint violations as {(file, rule): count}.

    Uses ruff's JSON output for structured parsing. Line numbers are
    intentionally ignored — they shift when code is edited, so we
    compare per-file rule counts instead.
    """
    targets = _ruff_targets(files)
    if not targets:
        return {}

    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--output-format", "json", *targets],
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT,
        cwd=cwd,
        check=False,
    )

    counts: dict[tuple[str, str], int] = {}
    with contextlib.suppress(json.JSONDecodeError):
        violations = json.loads(result.stdout)
        for v in violations:
            key = (v.get("filename", ""), v.get("code", ""))
            counts[key] = counts.get(key, 0) + 1
    return counts


def _format_new_violations(violations: list[dict]) -> str:
    """Format new violations into human-readable lint output."""
    lines = []
    for v in violations:
        loc = v.get("location", {})
        lines.append(
            f"{v.get('filename', '?')}:{loc.get('row', '?')}:"
            f"{loc.get('column', '?')}: "
            f"{v.get('code', '?')} {v.get('message', '')}"
        )
    return "\n".join(lines)


def run_lint(
    cwd: str,
    changed_files: list[str] | None = None,
    baseline: dict[tuple[str, str], int] | None = None,
) -> SensorResult:
    """Run ruff check and format on changed files only.

    If baseline is provided, only violations exceeding the baseline
    counts are treated as failures. This prevents pre-existing
    violations from blocking the coder.
    """
    if changed_files is not None:
        # Filter out paths git reports as changed but no longer exist on disk
        # (e.g. files the orchestrator removed via `git rm`); ruff errors out
        # if asked to lint a path that doesn't exist.
        targets = [t for t in _ruff_targets(changed_files) if (Path(cwd) / t).exists()]
        if not targets:
            return SensorResult(
                passed=True,
                output="=== ruff ===\nNo Python files to lint.\n",
                details={"skipped": True, "changed_file_count": len(changed_files)},
            )
    else:
        targets = ["."]

    if baseline is not None:
        # Baseline-aware mode: use JSON output and diff against baseline
        check = subprocess.run(
            ["uv", "run", "ruff", "check", "--output-format", "json", *targets],
            capture_output=True,
            text=True,
            timeout=LINT_TIMEOUT,
            cwd=cwd,
            check=False,
        )

        after: dict[tuple[str, str], int] = {}
        all_violations: list[dict] = []
        with contextlib.suppress(json.JSONDecodeError):
            all_violations = json.loads(check.stdout)
            for v in all_violations:
                key = (v.get("filename", ""), v.get("code", ""))
                after[key] = after.get(key, 0) + 1

        # Find new violations: count increased beyond baseline
        new_keys = {k for k, count in after.items() if count > baseline.get(k, 0)}
        new_violations = [
            v
            for v in all_violations
            if (v.get("filename", ""), v.get("code", "")) in new_keys
        ]

        fmt = subprocess.run(
            ["uv", "run", "ruff", "format", "--check", *targets],
            capture_output=True,
            text=True,
            timeout=LINT_TIMEOUT,
            cwd=cwd,
            check=False,
        )

        check_passed = len(new_violations) == 0
        fmt_passed = fmt.returncode == 0

        check_out = "=== ruff check (new violations only) ===\n"
        if new_violations:
            check_out += _format_new_violations(new_violations)
        else:
            check_out += "No new violations.\n"
        fmt_out = f"=== ruff format ===\n{fmt.stdout}{fmt.stderr}"

        total = sum(after.values())
        baseline_total = sum(baseline.values())
        logger.info(
            "Lint baseline: %d total violations (%d baseline, %d new)",
            total,
            baseline_total,
            len(new_violations),
        )

        return SensorResult(
            passed=check_passed and fmt_passed,
            output=f"{check_out}\n{fmt_out}",
            details={
                "new_violation_count": len(new_violations),
                "total_violation_count": total,
                "baseline_violation_count": baseline_total,
                "format_rc": fmt.returncode,
            },
        )

    # Non-baseline mode: original behavior
    check = subprocess.run(
        ["uv", "run", "ruff", "check", *targets],
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT,
        cwd=cwd,
        check=False,
    )

    fmt = subprocess.run(
        ["uv", "run", "ruff", "format", "--check", *targets],
        capture_output=True,
        text=True,
        timeout=LINT_TIMEOUT,
        cwd=cwd,
        check=False,
    )

    passed = check.returncode == 0 and fmt.returncode == 0
    check_out = f"=== ruff check ===\n{check.stdout}{check.stderr}"
    fmt_out = f"=== ruff format ===\n{fmt.stdout}{fmt.stderr}"
    return SensorResult(
        passed=passed,
        output=f"{check_out}\n{fmt_out}",
        details={
            "check_rc": check.returncode,
            "format_rc": fmt.returncode,
        },
    )


def get_diff(cwd: str, base: str = "develop") -> str:
    """Get the diff between the current branch and the base branch."""
    result = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )
    return result.stdout


def format_pr_body(plan: dict, issue: dict) -> str:
    """Render a planner plan + issue as a human-and-LLM-readable PR body.

    Sections with no data are omitted. Raw JSON is preserved at the end so
    downstream tooling (and reviewers who want the canonical form) can still
    read it.
    """
    sections: list[str] = [f"Resolves #{issue['number']}"]

    classification = plan.get("classification")
    complexity = plan.get("estimated_complexity")
    branch_name = plan.get("branch_name")
    meta_parts: list[str] = []
    if classification:
        meta_parts.append(f"**{classification}**")
    if complexity:
        meta_parts.append(f"complexity: **{complexity}**")
    if branch_name:
        meta_parts.append(f"branch: `{branch_name}`")
    if meta_parts:
        sections.append("> " + " · ".join(meta_parts))

    summary = plan.get("issue_title") or issue.get("title")
    if summary:
        sections.append(f"## Summary\n\n{summary}")

    plan_steps = plan.get("plan_steps") or []
    if plan_steps:
        lines = ["## Plan", ""]
        for index, step in enumerate(plan_steps, start=1):
            number = step.get("step", index)
            description = step.get("description", "").strip()
            file = step.get("file")
            rationale = (step.get("rationale") or "").strip()
            lines.append(f"{number}. **{description}**")
            if file:
                lines.append(f"   - File: `{file}`")
            if rationale:
                lines.append(f"   - Why: {rationale}")
        sections.append("\n".join(lines))

    affected_files = plan.get("affected_files") or []
    if affected_files:
        files_block = "\n".join(f"- `{f}`" for f in affected_files)
        sections.append(f"## Affected files\n\n{files_block}")

    criteria = plan.get("acceptance_criteria") or []
    if criteria:
        crit_block = "\n".join(f"- [ ] {c}" for c in criteria)
        sections.append(f"## Acceptance criteria\n\n{crit_block}")

    risks = plan.get("risks") or []
    if risks:
        risks_block = "\n".join(f"- {r}" for r in risks)
        sections.append(f"## Risks\n\n{risks_block}")

    raw_json = json.dumps(plan, indent=2)
    sections.append(
        "---\n\n"
        "<details><summary>Raw plan (JSON)</summary>\n\n"
        f"```json\n{raw_json}\n```\n\n"
        "</details>"
    )

    return "\n\n".join(sections)


def create_draft_pr(
    repo: str,
    branch: str,
    issue: dict,
    plan: dict,
) -> str:
    """Create a draft PR and return its URL."""
    title = plan.get(
        "issue_title",
        issue.get("title", f"Resolve #{issue['number']}"),
    )
    if len(title) > MAX_PR_TITLE_LENGTH:
        title = title[: MAX_PR_TITLE_LENGTH - 3] + "..."

    body = format_pr_body(plan, issue)

    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            branch,
            "--base",
            "develop",
            "--title",
            title,
            "--body",
            body,
            "--draft",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
