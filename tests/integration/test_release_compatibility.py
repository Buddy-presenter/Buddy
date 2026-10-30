"""Real release guards; only ECS/S3 and container-proof inputs are external fakes."""

import copy

import pytest
from backend.scripts.release.deploy import Deployment, DeploymentError
from test_release import (
    CONFIG,
    IMAGE,
    OLD_IMAGE,
    OLD_WORKER_IMAGE,
    PREFIX,
    S3,
    SHA,
    WORKER_IMAGE,
    Ecs,
    Sts,
)


def proof(ecs):
    return {
        "passed": True,
        "traced_analysis": True,
        "before": copy.deepcopy(ecs.state),
        "rollback": copy.deepcopy(ecs.state),
        "rollback_image": OLD_IMAGE,
        "candidate_image": IMAGE,
        "schema_image": IMAGE,
        "checks": dict.fromkeys(
            [
                "readiness",
                "analysis",
                "legacy_writes",
                "sessions",
                "owner_isolation",
                "immutability",
            ],
            True,
        ),
    }


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "failed",
        "candidate",
        "rollback",
        "image",
        "before",
        "missing_check",
        "malformed_checks",
    ],
)
def test_invalid_proof_stops_before_definition_registration_or_live_ddl(failure):
    ecs, s3 = Ecs(), S3()
    report = proof(ecs)
    if failure == "missing":
        report = None
    elif failure == "failed":
        report["checks"]["legacy_writes"] = False
    elif failure == "candidate":
        report["candidate_image"] = OLD_IMAGE
    elif failure == "rollback":
        report["rollback"]["api"]["taskDefinition"] = PREFIX + "api:9"
    elif failure == "image":
        report["rollback_image"] = IMAGE
    elif failure == "before":
        report["before"]["api"]["desiredCount"] = 2
    elif failure == "malformed_checks":
        report["checks"] = ["readiness"]
    else:
        del report["checks"]["analysis"]
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=report)
    with pytest.raises(DeploymentError):
        driver.deploy(SHA, IMAGE)
    assert not ecs.registered and not ecs.runs and not ecs.updates


def test_service_drift_after_preflight_never_runs_migrations():
    ecs, s3 = Ecs(), S3()
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=proof(ecs))
    original = ecs.register_task_definition

    def drift(**kwargs):
        result = original(**kwargs)
        ecs.state["api"]["taskDefinition"] = PREFIX + "api:7"
        return result

    ecs.register_task_definition = drift
    with pytest.raises(DeploymentError):
        driver.deploy(SHA, IMAGE)
    assert not ecs.runs and not ecs.updates


def test_explicit_rollback_requires_a_fresh_test_of_its_actual_target():
    ecs, s3 = Ecs(), S3()
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=proof(ecs))
    driver.deploy(SHA, IMAGE)
    ecs.updates.clear()
    # The deploy proof has the pre-promotion state. Reusing it for a later
    # rollback cannot silently authorize another target/current service state.
    with pytest.raises(DeploymentError):
        driver.rollback("c" * 40, SHA)
    assert not ecs.updates


@pytest.mark.parametrize(
    "target,phase",
    [
        ("production", "compatibility"),
        ("production", "reader"),
        ("staging", "unknown"),
    ],
)
def test_transition_gate_rejects_unsupported_operations(monkeypatch, target, phase):
    from test_release import release_gate

    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("RELEASE_TARGET", target)
    monkeypatch.setenv("ROLLOUT_PHASE", phase)
    with pytest.raises(ValueError):
        release_gate["main"]()


def test_transition_gate_allows_staging_worker_after_current_revision_ci(monkeypatch):
    import json
    from types import SimpleNamespace

    from test_release import approved_checks, release_gate

    checks = [
        {"name": name, "head_sha": SHA, "status": "completed", "conclusion": "success"}
        for name in (
            "CI",
            "Terraform boundaries",
            "API container and database bootstrap",
            "Native package (windows-latest)",
            "Native package (macos-latest)",
        )
    ]
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("RELEASE_TARGET", "staging")
    monkeypatch.setenv("ROLLOUT_PHASE", "feature")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Buddy-presenter/Buddy")
    monkeypatch.setenv("WORKER_IMAGE", WORKER_IMAGE)
    monkeypatch.setattr(
        release_gate["subprocess"],
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"check_runs": checks})),
    )

    assert approved_checks(checks, SHA)
    assert release_gate["main"]() == 0


@pytest.mark.parametrize("worker_count", [None, 0, 1])
def test_reader_preparation_never_promotes_or_resumes_a_worker(worker_count):
    ecs, s3 = Ecs(), S3()
    if worker_count is not None:
        ecs.state["worker"] = {"taskDefinition": PREFIX + "worker:1", "desiredCount": worker_count}
    report = proof(ecs)
    report.update(reader_only=True, traced_analysis=False)
    if worker_count is not None:
        report["worker_compatibility"] = {
            "passed": True,
            "target_image": OLD_WORKER_IMAGE,
            "previous_image": OLD_WORKER_IMAGE,
            "schema_image": IMAGE,
        }
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=report)
    if worker_count == 1:
        with pytest.raises(DeploymentError, match="paused worker"):
            driver.deploy(SHA, IMAGE, reader_only=True)
        assert not ecs.registered and not ecs.runs and not ecs.updates
    else:
        driver.deploy(SHA, IMAGE, reader_only=True)
        assert all(update["service"] != "worker" for update in ecs.updates)


@pytest.mark.parametrize(
    "proof_changes",
    [
        {"traced_analysis": False},
        {"reader_only": True},
        {"traced_analysis": None},
    ],
)
def test_feature_promotion_cannot_reuse_reader_only_or_missing_trace_proof(proof_changes):
    ecs, s3 = Ecs(), S3()
    report = proof(ecs)
    report.update(proof_changes)
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=report)
    with pytest.raises(DeploymentError, match="traced-analysis"):
        driver.deploy(SHA, IMAGE)
    assert not ecs.registered and not ecs.runs and not ecs.updates


def test_reader_preparation_cannot_publish_a_worker_image():
    ecs, s3 = Ecs(), S3()
    report = proof(ecs)
    report.update(reader_only=True, traced_analysis=False)
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=report)
    with pytest.raises(DeploymentError, match="API-only"):
        driver.deploy(SHA, IMAGE, WORKER_IMAGE, reader_only=True)
    assert not ecs.registered and not ecs.runs and not ecs.updates


def test_schema_bridge_proof_cannot_publish_a_worker_image():
    ecs, s3 = Ecs(), S3()
    report = proof(ecs)
    report.update(future_image=IMAGE, traced_analysis=False)
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=report)
    with pytest.raises(DeploymentError, match="bridge preparation"):
        driver.deploy(SHA, IMAGE, WORKER_IMAGE)
    assert not ecs.registered and not ecs.runs and not ecs.updates


def test_rollback_preflight_uses_retained_feature_schema_after_app_rollback(monkeypatch, tmp_path):
    import json
    import sys

    from backend.scripts.release import prepare_compatibility as preflight

    ecs, s3 = Ecs(), S3()
    s3.records[f"staging/backend/{SHA}.json"] = json.dumps(
        {
            "status": "healthy",
            "environment": "staging",
            "account_id": CONFIG["account_id"],
            "revision": SHA,
            "previous": copy.deepcopy(ecs.state),
            "compatibility": {"schema_image": IMAGE},
        }
    ).encode()
    clients = {"ecs": ecs, "s3": s3, "sts": Sts()}
    monkeypatch.setattr(preflight.boto3, "client", lambda service, **kw: clients[service])
    observed = {}

    def verify(previous, candidate, **kwargs):
        observed.update(previous=previous, candidate=candidate, **kwargs)
        return {"passed": True}

    monkeypatch.setattr(preflight, "verify_images", verify)
    monkeypatch.setenv("BUDDY_DEPLOYMENT_CONFIG", json.dumps(CONFIG))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--revision",
            "c" * 40,
            "--rollback-revision",
            SHA,
            "--output",
            str(tmp_path / "proof.json"),
        ],
    )
    preflight.main()
    assert observed["candidate"] == OLD_IMAGE  # app already restored
    assert observed["schema_image"] == IMAGE  # database still expanded


def test_bridge_preflight_uses_the_exact_current_service_image_as_its_schema_baseline(
    monkeypatch, tmp_path
):
    import json
    import sys

    from backend.scripts.release import prepare_compatibility as preflight

    ecs, s3 = Ecs(), S3()
    clients = {"ecs": ecs, "s3": s3, "sts": Sts()}
    monkeypatch.setattr(preflight.boto3, "client", lambda service, **kw: clients[service])
    monkeypatch.setattr(preflight, "image_digest", lambda config, value, service: value)
    observed = {}

    def verify(previous, candidate, **kwargs):
        observed.update(previous=previous, candidate=candidate, **kwargs)
        return {"passed": True}

    monkeypatch.setattr(preflight, "verify_images", verify)
    monkeypatch.setenv("BUDDY_DEPLOYMENT_CONFIG", json.dumps(CONFIG))
    bridge = (
        f"{CONFIG['account_id']}.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:"
        + "c" * 64
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--revision",
            "c" * 40,
            "--candidate-image",
            bridge,
            "--future-image",
            IMAGE,
            "--base-image",
            OLD_IMAGE,
            "--output",
            str(tmp_path / "proof.json"),
        ],
    )

    preflight.main()

    assert observed == {
        "previous": OLD_IMAGE,
        "candidate": bridge,
        "future": IMAGE,
        "baseline_image": OLD_IMAGE,
        "schema_image": None,
    }


def test_worker_preflight_binds_candidate_and_previous_worker_images_to_live_service(
    monkeypatch, tmp_path
):
    import json
    import sys

    from backend.scripts.release import prepare_compatibility as preflight

    ecs, s3 = Ecs(), S3()
    ecs.state["worker"] = {
        "taskDefinition": PREFIX + "worker:1",
        "desiredCount": 0,
    }
    config = copy.deepcopy(CONFIG)
    config["tasks"]["worker"] = PREFIX + "worker:1"
    clients = {"ecs": ecs, "s3": s3, "sts": Sts()}
    monkeypatch.setattr(preflight.boto3, "client", lambda service, **kw: clients[service])
    observed = {}

    def verify(previous, candidate, **kwargs):
        observed.update(previous=previous, candidate=candidate, **kwargs)
        return {"passed": True}

    monkeypatch.setattr(preflight, "verify_images", verify)
    monkeypatch.setenv("BUDDY_DEPLOYMENT_CONFIG", json.dumps(config))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--revision",
            "c" * 40,
            "--candidate-image",
            IMAGE,
            "--worker-image",
            WORKER_IMAGE,
            "--output",
            str(tmp_path / "proof.json"),
        ],
    )

    preflight.main()

    assert observed["worker_image"] == WORKER_IMAGE
    assert observed["previous_worker_image"] == OLD_WORKER_IMAGE
    assert observed["schema_image"] is None
    assert (
        json.loads((tmp_path / "proof.json").read_text())["rollback"]["worker"]["desiredCount"] == 0
    )


def test_bridge_preflight_stops_if_the_live_api_image_changed_after_build(monkeypatch, tmp_path):
    import json
    import sys

    from backend.scripts.release import prepare_compatibility as preflight

    ecs, s3 = Ecs(), S3()
    clients = {"ecs": ecs, "s3": s3, "sts": Sts()}
    monkeypatch.setattr(preflight.boto3, "client", lambda service, **kw: clients[service])
    monkeypatch.setattr(preflight, "image_digest", lambda config, value, service: value)
    monkeypatch.setattr(
        preflight, "verify_images", lambda *args, **kwargs: pytest.fail("must not test drift")
    )
    monkeypatch.setenv("BUDDY_DEPLOYMENT_CONFIG", json.dumps(CONFIG))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            "--revision",
            "c" * 40,
            "--candidate-image",
            IMAGE,
            "--future-image",
            IMAGE,
            "--base-image",
            IMAGE,
            "--output",
            str(tmp_path / "proof.json"),
        ],
    )

    with pytest.raises(DeploymentError, match="drifted"):
        preflight.main()


def test_bridge_compatibility_fixture_starts_from_the_live_image_schema(monkeypatch):
    import json

    from backend.scripts.release import image_compatibility as compatibility

    initial, baseline, bridge, future = "initial", "current", "bridge", "feature"
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if "/checks/rollback_probe.py" in args:
            return json.dumps(
                {
                    "ready": True,
                    "analysis": True,
                    "session": True,
                    "owner_isolation": True,
                }
            )
        return "ok"

    monkeypatch.setattr(compatibility, "docker", docker)
    monkeypatch.setattr(compatibility.subprocess, "run", lambda *args, **kwargs: None)

    report = compatibility.verify_images(
        baseline,
        bridge,
        initial=initial,
        baseline_image=baseline,
        future=future,
        pull_images=False,
    )

    migrations = [call[-4] for call in calls if call[:3] == ("run", "--rm", "--entrypoint")]
    assert migrations == [initial, baseline, future]
    probes = [call[-1] for call in calls if "/checks/rollback_probe.py" in call]
    assert probes == ["seed", "baseline", "baseline", "typed", "write", "read"]
    assert report["baseline_image"] == baseline
    assert report["rollback_image"] == baseline


def test_worker_images_are_smoked_against_the_feature_schema(monkeypatch):
    import json

    from backend.scripts.release import image_compatibility as compatibility

    observed = []

    def docker(*args, **kwargs):
        if "/checks/rollback_probe.py" in args:
            return json.dumps(
                {"ready": True, "analysis": True, "session": True, "owner_isolation": True}
            )
        return "ok"

    monkeypatch.setattr(compatibility, "docker", docker)
    monkeypatch.setattr(
        compatibility,
        "verify_worker_image",
        lambda image, schema: observed.append((image, schema)),
    )
    monkeypatch.setattr(compatibility.subprocess, "run", lambda *args, **kwargs: None)

    report = compatibility.verify_images(
        OLD_IMAGE,
        IMAGE,
        future="feature-api",
        worker_image=WORKER_IMAGE,
        previous_worker_image=OLD_WORKER_IMAGE,
        pull_images=False,
    )

    assert observed == [(WORKER_IMAGE, "feature-api"), (OLD_WORKER_IMAGE, "feature-api")]
    assert report["worker_compatibility"] == {
        "passed": True,
        "target_image": WORKER_IMAGE,
        "previous_image": OLD_WORKER_IMAGE,
        "schema_image": "feature-api",
    }


@pytest.mark.parametrize("trace_verified", [True, False])
def test_feature_proof_requires_both_readers_to_verify_trace(monkeypatch, trace_verified):
    import json

    from backend.scripts.release import image_compatibility as compatibility

    probes = []

    def docker(*args, **kwargs):
        if "/checks/rollback_probe.py" in args:
            probes.append(args[-1])
            return json.dumps(
                {
                    "ready": True,
                    "analysis": True,
                    "session": True,
                    "owner_isolation": True,
                    "traced_analysis": trace_verified,
                }
            )
        return "ok"

    monkeypatch.setattr(compatibility, "docker", docker)
    monkeypatch.setattr(compatibility.subprocess, "run", lambda *args, **kwargs: None)
    if trace_verified:
        report = compatibility.verify_images("previous", "candidate", pull_images=False)
        assert probes[-3:] == ["trace-write", "trace-read", "trace-read"]
        assert report["traced_analysis"] is True
    else:
        with pytest.raises(AssertionError):
            compatibility.verify_images("previous", "candidate", pull_images=False)


@pytest.mark.parametrize("state", ["draining", "pending", "in_progress"])
@pytest.mark.parametrize("when", ["before_registration", "before_migration"])
def test_unfinished_source_rollout_blocks_migration(state, when):
    ecs, s3 = Ecs(), S3()
    describe = ecs.describe_services

    def unfinished(**kwargs):
        result = describe(**kwargs)
        if when == "before_migration" and not ecs.registered:
            return result
        service = result["services"][0]
        if state == "draining":
            service["deployments"].append({"status": "ACTIVE"})
        elif state == "pending":
            service["pendingCount"] = 1
        else:
            service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
        return result

    ecs.describe_services = unfinished
    driver = Deployment(CONFIG, ecs, s3, Sts(), lambda _: None, compatibility_report=proof(ecs))
    with pytest.raises(DeploymentError):
        driver.deploy(SHA, IMAGE)
    if when == "before_registration":
        assert not ecs.registered
    assert not ecs.runs and not ecs.updates
