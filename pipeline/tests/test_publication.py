"""Guards on what this repo publishes.

These are not pipeline tests. They exist because the public template and the
private instance share a codebase but not a policy: the instance tracks
interests.md, feedback-log.md, daily candidates and the event log on purpose,
and the template must carry none of it. Every publication so far has been a
manual copy, and the failure it invites is silent — a stray candidates file or
a hardcoded repo slug looks like nothing in a 30-file diff.

Repo role is inferred from whether interests.md is tracked. A repo that tracks
it is somebody's instance and the personal-data guards do not apply; a repo
that does not is the template, and must be clean of the *rest* of the personal
artifacts too. That is the realistic mistake — deleting the obvious file and
leaving the 26 non-obvious ones.
"""

import fnmatch
import pathlib
import subprocess

import pytest

from pipeline import db

# This file necessarily contains the strings it searches for, so it excludes
# itself from its own grep. Derived from __file__ so renaming it cannot
# silently turn the guard into a no-op against some stale hardcoded path.
SELF = str(pathlib.Path(__file__).resolve().relative_to(db.REPO_ROOT))

# Personal artifacts that must never appear in the published template. Values
# are (glob, why it matters) so a failure explains itself.
PERSONAL_PATTERNS = [
    ("interests.md", "the author's stated interests"),
    ("feedback-log.md", "the author's feedback history"),
    ("eval/metrics-*.json", "per-run eval reports"),
    ("eval/judge-*.json", "per-run LLM-judge reports"),
    ("daily/candidates-*.json", "daily ranked candidates"),
    ("daily/digest-manifest-*.json", "per-day digest manifests"),
    ("data/events.jsonl", "the legacy flat event log"),
    ("data/events/*.jsonl", "monthly event shards"),
    ("data/health/*.jsonl", "pipeline health records"),
]


def _tracked():
    """Every path git tracks, as a list of repo-relative strings."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=db.REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def _is_template(tracked):
    return "interests.md" not in tracked


def _matching(tracked, pattern):
    return [p for p in tracked if fnmatch.fnmatch(p, pattern)]


def test_the_template_publishes_no_personal_data():
    """A template seeded with the author's history trains every new user's
    ranker on the author's taste before they have read a single digest."""
    tracked = _tracked()
    if not _is_template(tracked):
        # skip, not a bare return: a zero-assertion "pass" is indistinguishable
        # from a verified-clean one in the instance's CI, where this branch is
        # always taken.
        pytest.skip("instance repo — it tracks personal data on purpose")

    leaked = []
    for pattern, why in PERSONAL_PATTERNS:
        hits = _matching(tracked, pattern)
        if hits:
            leaked.append(f"{len(hits)}x {pattern} ({why}) e.g. {hits[0]}")
    assert not leaked, "personal data tracked in the template:\n  " + "\n  ".join(leaked)


def test_the_template_hardcodes_no_instance_repo_slug():
    """`my-morning-digest` in a published file sends a template user's worker
    writes, or their setup instructions, at somebody else's repo."""
    tracked = _tracked()
    if not _is_template(tracked):
        pytest.skip("instance repo — its own slug is expected here")

    out = subprocess.run(
        ["git", "grep", "-l", "my-morning-digest"],
        cwd=db.REPO_ROOT,
        capture_output=True,
        text=True,
    )
    offenders = [line for line in out.stdout.splitlines() if line and line != SELF]
    assert not offenders, f"instance repo slug published in: {offenders}"


def test_no_wrangler_state_is_tracked():
    """worker/.wrangler/ caches the Cloudflare account id and account name.
    It is gitignored, but the ignore rule postdates the file, and gitignore
    does not untrack. Applies to both repos — it should not be in either."""
    tracked = _tracked()
    offenders = [p for p in tracked if p.startswith("worker/.wrangler/")]
    assert not offenders, f"wrangler local state is tracked: {offenders}"


def test_workflows_that_push_to_main_serialize_and_retry():
    """Four workflows and the Worker all write to main. The workflows race
    each other; push-with-retry.sh plus a shared concurrency group is what
    makes them queue instead of clobbering. A new workflow that pushes
    without both silently reintroduces the race."""
    workflows = sorted((db.REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found"

    problems = []
    for wf in workflows:
        text = wf.read_text()
        if "git push" not in text and "push-with-retry" not in text:
            continue  # does not write to main
        if "push-with-retry.sh" not in text:
            problems.append(f"{wf.name} pushes without push-with-retry.sh")
        if "main-branch-writes" not in text:
            problems.append(f"{wf.name} pushes without the main-branch-writes concurrency group")
    assert not problems, "\n".join(problems)


def test_push_with_retry_is_executable():
    """Committed mode 100644 instead of 100755 fails the workflow at the
    point of pushing — after the run has already done its work."""
    script = db.REPO_ROOT / ".github" / "scripts" / "push-with-retry.sh"
    assert script.exists(), f"{script} is missing"
    mode = subprocess.run(
        ["git", "ls-files", "-s", ".github/scripts/push-with-retry.sh"],
        cwd=db.REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert mode and mode[0] == "100755", f"expected mode 100755, got {mode[0] if mode else 'untracked'}"


def test_readme_does_not_document_the_deleted_sync_workflow():
    """The template auto-sync workflow was deleted because it cannot work —
    template instantiation shares no history, so the merge conflicts on every
    personal file. The README kept telling users to run it."""
    readme = (db.REPO_ROOT / "README.md").read_text()
    assert "Sync Template Updates" not in readme, (
        "README documents the auto-sync workflow, which was deleted in 141cc2c"
    )


def test_every_scheduled_workflow_is_guarded_against_running_in_the_template():
    """The template ships these workflows for downstream users but must not run
    them itself — a scheduled run there recommits candidates and event shards
    into a repo meant to ship clean, and the watchdog fails daily since the
    template has no digest. GitHub Actions cannot share an expression across
    workflow files, so the guard is copied per file; this is what makes a
    forgotten copy red instead of silent. ci.yml is deliberately unguarded —
    it is what enforces these guards in the template."""
    guard = "github.repository != 'neelgun17/morning-digest'"
    unguarded = []
    for wf in sorted((db.REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text()
        if "\n  schedule:" not in text:
            continue  # not scheduled — nothing to run unbidden in the template
        if guard not in text:
            unguarded.append(wf.name)
    assert not unguarded, (
        "scheduled workflows missing the template guard: " + ", ".join(unguarded)
    )
