"""GitHub label state machine and gh CLI wrappers for quil."""

import json
import re
import subprocess

LABELS: list[tuple[str, str, str]] = [
    ("agent-ready", "0E8A16", "Issue is eligible for agent processing"),
    ("agent-planning", "FBCA04", "Planner agent is running"),
    ("agent-coding", "FBCA04", "Coder agent is running"),
    ("agent-ci-pending", "FBCA04", "Waiting for CI to complete"),
    ("agent-reviewing", "FBCA04", "Code review agent is running"),
    ("agent-pr-open", "1D76DB", "Draft PR created, awaiting human review"),
    ("agent-rejected", "D93F0B", "Rejected after max review attempts"),
    ("agent-failed", "D93F0B", "Pipeline failed — needs human intervention"),
]

VALID_TRANSITIONS: dict[str | None, list[str]] = {
    None: ["agent-planning"],
    "agent-ready": ["agent-planning"],
    "agent-planning": ["agent-coding", "agent-failed"],
    # Self-loop on agent-coding: when the inner lint loop exhausts its
    # retries, _single_attempt short-circuits before push/CI/review and
    # the issue stays on agent-coding. The next outer attempt restarts
    # coding from that same label, which is a no-op transition.
    # agent-rejected is also reachable directly from agent-coding for
    # the case where the final outer attempt ended on a lint short-circuit.
    "agent-coding": [
        "agent-coding",
        "agent-ci-pending",
        "agent-failed",
        "agent-rejected",
    ],
    "agent-ci-pending": ["agent-reviewing", "agent-failed"],
    "agent-reviewing": [
        "agent-coding",
        "agent-pr-open",
        "agent-rejected",
        "agent-failed",
    ],
}


def _run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def detect_repo() -> str:
    """Detect the GitHub owner/repo from the current directory."""
    result = _run_gh(
        [
            "repo",
            "view",
            "--json",
            "nameWithOwner",
            "-q",
            ".nameWithOwner",
        ]
    )
    return result.stdout.strip()


def setup_labels(repo: str) -> None:
    """Create all agent labels on the repo (idempotent via --force)."""
    for name, color, description in LABELS:
        _run_gh(
            [
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ]
        )


def fetch_issue(repo: str, number: int) -> dict:
    """Fetch issue details as a dict."""
    result = _run_gh(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,comments",
        ]
    )
    return json.loads(result.stdout)


def list_eligible(repo: str) -> list[dict]:
    """List issues labeled agent-ready."""
    result = _run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            "agent-ready",
            "--json",
            "number,title",
        ]
    )
    return json.loads(result.stdout)


def get_issue_labels(repo: str, number: int) -> set[str]:
    """Get the set of label names on an issue."""
    result = _run_gh(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "labels",
            "-q",
            ".labels[].name",
        ]
    )
    return {line.strip() for line in result.stdout.strip().splitlines() if line.strip()}


def transition(
    repo: str,
    issue: int,
    from_label: str | None,
    to_label: str,
) -> None:
    """Swap labels on an issue, validating the transition is allowed."""
    allowed = VALID_TRANSITIONS.get(from_label, [])
    if to_label not in allowed:
        msg = f"Invalid transition: {from_label} -> {to_label}"
        raise ValueError(msg)

    if from_label is not None:
        current_labels = get_issue_labels(repo, issue)
        if from_label not in current_labels:
            msg = f"Issue #{issue} does not have label '{from_label}'"
            raise ValueError(msg)
        # Self-loop: issue is already on the target label, nothing to do.
        if from_label == to_label:
            return

    args = [
        "issue",
        "edit",
        str(issue),
        "--repo",
        repo,
        "--add-label",
        to_label,
    ]
    if from_label is not None:
        args.extend(["--remove-label", from_label])
    _run_gh(args)


def comment_on_issue(repo: str, issue: int, body: str) -> None:
    """Post a comment on an issue."""
    _run_gh(
        [
            "issue",
            "comment",
            str(issue),
            "--repo",
            repo,
            "--body",
            body,
        ]
    )


def _slugify(text: str, max_length: int = 40) -> str:
    """Convert text to a URL-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_length].rstrip("-")


def derive_branch_name(issue: dict) -> str:
    """Derive a branch name from issue labels and title."""
    label_names = {label["name"] for label in issue.get("labels", [])}
    number = issue["number"]
    slug = _slugify(issue["title"])

    if "bug" in label_names:
        prefix = "bug"
    elif "cleanup" in label_names:
        prefix = "chore"
    else:
        prefix = "feature"

    return f"{prefix}/{number}-{slug}"
