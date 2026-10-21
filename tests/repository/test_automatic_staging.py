"""Automatic staging rejects untrusted/obsolete events and waits for all exact-SHA gates."""

import copy
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RELEASE = Path(__file__).resolve().parents[2] / "scripts/release"
sys.path.insert(0, str(RELEASE))
try:
    staging = SimpleNamespace(**runpy.run_path(str(RELEASE / "automatic_staging.py")))
    gate = SimpleNamespace(**runpy.run_path(str(RELEASE / "gate.py")))
finally:
    sys.path.pop(0)

SHA = "a" * 40
REPO = "Buddy-presenter/Buddy"
EVENT = {
    "repository": {"full_name": REPO},
    "workflow_run": {
        "head_repository": {"full_name": REPO},
        "event": "push",
        "head_branch": "main",
        "conclusion": "success",
        "name": "Continuous integration",
        "head_sha": SHA,
    },
}
NAMES = [
    "CI",
    "Terraform boundaries",
    "API container and database bootstrap",
    "Native package (windows-latest)",
    "Native package (macos-latest)",
]


def checks():
    return [
        {"name": name, "head_sha": SHA, "status": "completed", "conclusion": "success"}
        for name in NAMES
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("conclusion", "failure"),
        ("conclusion", "cancelled"),
        ("head_sha", "main"),
        ("name", "Other workflow"),
        ("head_repository", {"full_name": "attacker/Buddy"}),
    ],
)
def test_untrusted_events_never_query_or_deploy(field, value):
    event = copy.deepcopy(EVENT)
    event["workflow_run"][field] = value

    def forbidden(_):
        pytest.fail("Untrusted event queried release evidence")

    assert staging.select_revision(event, REPO, fetch=forbidden) == ""


def test_foreign_repository_and_empty_event_are_rejected():
    assert not staging.eligible_event(EVENT, "other/Buddy")
    assert not staging.eligible_event({}, REPO)


def test_waits_for_independent_checks_on_exact_revision():
    responses = iter(
        [
            {"object": {"sha": SHA}},
            {"check_runs": checks()[:-1]},
            {"object": {"sha": SHA}},
            {"check_runs": checks()},
        ]
    )
    delays = []
    assert (
        staging.select_revision(EVENT, REPO, fetch=lambda _: next(responses), sleep=delays.append)
        == SHA
    )
    assert delays == [20]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out"])
def test_failed_required_checks_stop_without_waiting(conclusion):
    entries = checks()
    entries[-1]["conclusion"] = conclusion

    def fetch(path):
        return {"object": {"sha": SHA}} if "/git/ref/" in path else {"check_runs": entries}

    with pytest.raises(ValueError, match="Required main checks"):
        staging.select_revision(EVENT, REPO, fetch=fetch)


def test_missing_checks_time_out_and_wrong_sha_cannot_pass():
    entries = [{**entry, "head_sha": "b" * 40} for entry in checks()]

    def fetch(path):
        return {"object": {"sha": SHA}} if "/git/ref/" in path else {"check_runs": entries}

    delays = []
    with pytest.raises(TimeoutError):
        staging.select_revision(EVENT, REPO, fetch=fetch, sleep=delays.append)
    assert len(delays) == 89


def test_superseded_commit_is_skipped_before_check_lookup():
    assert staging.select_revision(EVENT, REPO, fetch=lambda _: {"object": {"sha": "b" * 40}}) == ""


def test_api_failure_fails_closed():
    def fetch(_):
        raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError):
        staging.select_revision(EVENT, REPO, fetch=fetch)


def test_selected_revision_is_rechecked_before_aws(monkeypatch):
    for name, value in {
        "GITHUB_SHA": "b" * 40,
        "RELEASE_REVISION": SHA,
        "RELEASE_TARGET": "staging",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": REPO,
        "AUTOMATIC_STAGING": "true",
        "RELEASE_OPERATION": "deploy",
        "ROLLOUT_PHASE": "feature",
    }.items():
        monkeypatch.setenv(name, value)
    calls = []

    def run(command, **kwargs):
        calls.append(command[-1])
        return SimpleNamespace(
            stdout=json.dumps(
                {"object": {"sha": SHA}} if "/git/ref/" in command[-1] else {"check_runs": checks()}
            )
        )

    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.main() == 0
    assert SHA in calls[-1]
    monkeypatch.setenv("RELEASE_REVISION", "c" * 40)
    with pytest.raises(ValueError, match="superseded"):
        gate.main()
    monkeypatch.setenv("RELEASE_TARGET", "production")
    with pytest.raises(ValueError, match="only promote staging"):
        gate.main()


def test_workflow_checks_out_selected_sha_and_keeps_serialization():
    workflow = Path(".github/workflows/release-backend.yml").read_text()
    assert "workflows: [Continuous integration]" in workflow
    assert "ref: ${{ needs.revision.outputs.revision }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "queue: max" in workflow
    assert "Prove exact rollback-image compatibility before staging DDL" in workflow
    assert '--revision "$RELEASE_REVISION"' in workflow
    assert '--revision "$GITHUB_SHA"' not in workflow


def test_worker_bootstrap_is_explicit_paused_and_release_is_digest_pinned():
    workflow = Path(".github/workflows/release-backend.yml").read_text()
    assert "bootstrap_worker_image" in workflow
    assert "terraform-managed staging worker service" in workflow.lower()
    assert "docker build -f infra/container/Dockerfile.worker" in workflow
    assert 'worker_image="$registry/$worker_repository@$worker_digest"' in workflow
    assert "name: staging-release-images" in workflow
    assert '"worker_service_exists": os.environ["WORKER_SERVICE_EXISTS"] == "true"' in workflow
    assert "worker_start_services = false" in Path("docs/infrastructure.md").read_text()
    assert "paused" in Path("docs/infrastructure.md").read_text().lower()
