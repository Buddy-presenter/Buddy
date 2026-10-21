"""Release failure paths: no promotion on migration failure, exact rollback and integrity."""

import copy
import io
import json
import runpy
from pathlib import Path

import pytest
from backend.scripts.release.deploy import Deployment, DeploymentError, image_digest, validate
from frontend.scripts.release.build_desktop import manifest
from frontend.scripts.release.smoke_package import verify

release_gate = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/release/gate.py"))
approved_checks = release_gate["approved_checks"]

SHA = "a" * 40
ACCOUNT = "123456789012"
PREFIX = f"arn:aws:ecs:eu-central-1:{ACCOUNT}:task-definition/buddy-staging-"
OLD_IMAGE = f"{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:" + "d" * 64
IMAGE = f"{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:" + "b" * 64
OLD_WORKER_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/worker@sha256:" + "e" * 64
)
WORKER_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/worker@sha256:" + "c" * 64
)
CONFIG = {
    "environment": "staging",
    "account_id": ACCOUNT,
    "region": "eu-central-1",
    "cluster": "buddy-staging",
    "bucket": f"buddy-staging-{ACCOUNT}-releases",
    "api_url": "https://staging.example.com",
    "tasks": {"api": PREFIX + "api:1", "migration": PREFIX + "migration:1"},
    "network": {
        "assignPublicIp": "DISABLED",
        "subnets": ["subnet-private"],
        "securityGroups": ["sg-migration"],
    },
}


class Sts:
    def get_caller_identity(self):
        return {"Account": ACCOUNT}


class S3:
    def __init__(self):
        self.records = {}

    def put_object(self, **kwargs):
        if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in self.records:
            raise RuntimeError("Audit history cannot be overwritten.")
        self.records[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.records[kwargs["Key"]])}


def operation_records(s3):
    return {
        key: json.loads(body)
        for key, body in s3.records.items()
        if "/operations/" in key and not key.endswith(".started.json")
    }


class Ecs:
    def __init__(self, migration_exit=0, expected_public_ip="DISABLED"):
        self.migration_exit = migration_exit
        self.expected_public_ip = expected_public_ip
        self.state = {"api": {"taskDefinition": PREFIX + "api:1", "desiredCount": 1}}
        self.updates = []
        self.waits = []
        self.registered = []
        self.runs = []

    def describe_services(self, **kwargs):
        names = kwargs["services"]
        present = [name for name in names if name in self.state]
        return {
            "services": [
                {
                    "serviceName": name,
                    **self.state[name],
                    "runningCount": self.state[name]["desiredCount"],
                    "pendingCount": 0,
                    "deployments": [
                        {
                            "status": "PRIMARY",
                            "rolloutState": "COMPLETED",
                            "taskDefinition": self.state[name]["taskDefinition"],
                        }
                    ],
                }
                for name in present
            ],
            "failures": [
                {"arn": name, "reason": "MISSING", "detail": "synthetic absent service"}
                for name in names
                if name not in self.state
            ],
        }

    def describe_task_definition(self, **kwargs):
        family = kwargs["taskDefinition"].split("/")[1].split(":")[0]
        name = family.rsplit("-", 1)[1]
        return {
            "taskDefinition": {
                "family": family,
                "containerDefinitions": [
                    {
                        "name": name,
                        "image": (
                            (
                                WORKER_IMAGE
                                if kwargs["taskDefinition"].endswith(":2")
                                else OLD_WORKER_IMAGE
                            )
                            if name == "worker"
                            else (IMAGE if kwargs["taskDefinition"].endswith(":2") else OLD_IMAGE)
                        ),
                        **(
                            {"healthCheck": {"command": ["CMD", "python", "--health"]}}
                            if name == "worker"
                            else {}
                        ),
                    }
                ],
                "requiresCompatibilities": ["FARGATE"],
            }
        }

    def register_task_definition(self, **kwargs):
        self.registered.append(kwargs)
        return {
            "taskDefinition": {
                "taskDefinitionArn": PREFIX + kwargs["family"].rsplit("-", 1)[1] + ":2"
            }
        }

    def run_task(self, **kwargs):
        self.runs.append(kwargs)
        assert (
            kwargs["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"]
            == self.expected_public_ip
        )
        return {"tasks": [{"taskArn": "task"}]}

    def get_waiter(self, name):
        self.waits.append(name)
        return self

    def wait(self, **kwargs):
        pass

    def describe_tasks(self, **kwargs):
        return {"tasks": [{"containers": [{"exitCode": self.migration_exit}]}]}

    def update_service(self, **kwargs):
        self.updates.append(kwargs)
        self.state[kwargs["service"]] = {
            "taskDefinition": kwargs["taskDefinition"],
            "desiredCount": kwargs["desiredCount"],
        }


class PreparedDeployment(Deployment):
    """Supply the synthetic external Docker-verifier result before each unit operation.

    These existing tests exercise ECS/S3 failure paths. Separate preflight tests use
    Deployment directly to reject missing, stale, mismatched and drifted evidence.
    """

    def evidence(self, candidate, rollback, worker_image=None, before=None):
        before = before or self.ecs.state
        self.compatibility_report = {
            "passed": True,
            "candidate_image": candidate,
            "rollback_image": OLD_IMAGE
            if rollback["api"]["taskDefinition"].endswith(":1")
            else IMAGE,
            "before": copy.deepcopy(before),
            "rollback": copy.deepcopy(rollback),
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
            "schema_image": candidate,
        }
        if worker_image:
            previous_worker_image = None
            if "worker" in before:
                previous_worker_image = self.ecs.describe_task_definition(
                    taskDefinition=before["worker"]["taskDefinition"]
                )["taskDefinition"]["containerDefinitions"][0]["image"]
            self.compatibility_report["worker_compatibility"] = {
                "passed": True,
                "target_image": worker_image,
                "previous_image": previous_worker_image,
                "schema_image": candidate,
            }

    def deploy(self, revision, api_image, worker_image=None):
        evidence_worker = worker_image
        if evidence_worker is None and "worker" in self.ecs.state:
            evidence_worker = self.ecs.describe_task_definition(
                taskDefinition=self.ecs.state["worker"]["taskDefinition"]
            )["taskDefinition"]["containerDefinitions"][0]["image"]
        self.evidence(api_image, self.ecs.state, evidence_worker)
        return super().deploy(revision, api_image, worker_image)

    def rollback(self, revision, previous_revision):
        raw = self.s3.records.get(f"staging/backend/{previous_revision}.json", b"{}")
        target = json.loads(raw).get("previous", self.ecs.state)
        if not isinstance(target, dict) or not isinstance(target.get("api"), dict):
            target = self.ecs.state
        candidate = IMAGE if self.ecs.state["api"]["taskDefinition"].endswith(":2") else OLD_IMAGE
        worker_image = None
        if isinstance(target, dict) and isinstance(target.get("worker"), dict):
            worker_image = self.ecs.describe_task_definition(
                taskDefinition=target["worker"]["taskDefinition"]
            )["taskDefinition"]["containerDefinitions"][0]["image"]
        self.evidence(candidate, target, worker_image, before=self.ecs.state)
        return super().rollback(revision, previous_revision)


def test_migration_failure_never_changes_live_service(monkeypatch, capsys):
    ecs, s3 = Ecs(migration_exit=1), S3()
    before = copy.deepcopy(ecs.state)
    run = ecs.run_task

    def audited_run(**kwargs):
        started = [
            json.loads(body) for key, body in s3.records.items() if key.endswith(".started.json")
        ]
        assert len(started) == 1
        assert started[0]["status"] == "started"
        assert "completed_at" not in started[0]
        assert not operation_records(s3)
        return run(**kwargs)

    monkeypatch.setattr(ecs, "run_task", audited_run)
    with pytest.raises(DeploymentError, match="Migration failed"):
        PreparedDeployment(CONFIG, ecs, s3, Sts()).deploy(SHA, IMAGE)
    assert ecs.updates == []
    assert ecs.state == before
    audit = next(iter(operation_records(s3).values()))
    assert audit["operation"] == "deploy"
    assert audit["revision"] == SHA
    assert audit["environment"] == CONFIG["environment"]
    assert audit["account_id"] == ACCOUNT
    assert audit["status"] == "migration_failed"
    assert audit["stage"] == "migration"
    assert audit["migration"] == PREFIX + "migration:2"
    assert audit["previous"] == before
    assert audit["audit_key"] in capsys.readouterr().err
    assert (
        json.loads(s3.records[audit["started_audit_key"]])["operation_id"] == audit["operation_id"]
    )
    assert f"staging/backend/{SHA}.json" not in s3.records


@pytest.mark.parametrize("failure", ["setup", "rejected", "wait"])
def test_migration_setup_rejection_and_wait_failures_retain_sanitized_outcomes(
    failure, monkeypatch, capsys
):
    ecs, s3 = Ecs(), S3()
    before = copy.deepcopy(ecs.state)

    def unavailable(**kwargs):
        raise RuntimeError("private credentials and provider error details")

    if failure == "setup":
        describe = ecs.describe_task_definition

        def setup(**kwargs):
            if "migration" in kwargs["taskDefinition"]:
                return unavailable(**kwargs)
            return describe(**kwargs)

        monkeypatch.setattr(ecs, "describe_task_definition", setup)
    elif failure == "rejected":
        monkeypatch.setattr(
            ecs, "run_task", lambda **kwargs: {"failures": [{"reason": "private credentials"}]}
        )
    else:
        monkeypatch.setattr(ecs, "wait", unavailable)
    with pytest.raises(DeploymentError, match="no traffic promoted") as error:
        PreparedDeployment(CONFIG, ecs, s3, Sts()).deploy(SHA, IMAGE)
    assert ecs.updates == [] and ecs.state == before
    audit = next(iter(operation_records(s3).values()))
    assert audit["status"] == "migration_failed"
    assert audit["stage"] == ("migration_setup" if failure == "setup" else "migration")
    assert audit["previous"] == before
    assert audit["audit_key"] in capsys.readouterr().err
    assert "private credentials" not in json.dumps(audit) + str(error.value)
    assert f"staging/backend/{SHA}.json" not in s3.records
    if failure == "setup":
        assert ecs.runs == []


def test_missing_durable_intent_prevents_migration_or_registration(monkeypatch):
    ecs, s3 = Ecs(), S3()

    def denied(**kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(s3, "put_object", denied)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        PreparedDeployment(CONFIG, ecs, s3, Sts()).deploy(SHA, IMAGE)
    assert ecs.runs == [] and ecs.registered == [] and ecs.updates == []


def test_readiness_failure_restores_previous_task_and_records_outcome():
    ecs, s3, probes = Ecs(), S3(), []

    def readiness(_):
        probes.append(1)
        if len(probes) == 1:
            raise RuntimeError("unhealthy")

    with pytest.raises(DeploymentError):
        PreparedDeployment(CONFIG, ecs, s3, Sts(), readiness).deploy(SHA, IMAGE)
    assert ecs.state["api"]["taskDefinition"] == PREFIX + "api:1"
    assert next(iter(operation_records(s3).values()))["status"] == "rolled_back"
    assert len(ecs.updates) == 2


def test_healthy_deployment_is_auditable_and_explicit_rollback_works():
    ecs, s3 = Ecs(), S3()
    deployment = PreparedDeployment(CONFIG, ecs, s3, Sts(), lambda _: None)
    assert deployment.deploy(SHA, IMAGE)["status"] == "healthy"
    assert ecs.state["api"]["taskDefinition"] == PREFIX + "api:2"
    lookup = copy.deepcopy(s3.records[f"staging/backend/{SHA}.json"])
    result = deployment.rollback("c" * 40, SHA)
    assert result["status"] == "rolled_back"
    recorded = json.loads(s3.records[result["audit_key"]])
    assert recorded == result
    assert recorded["operation"] == "rollback"
    assert recorded["revision"] == "c" * 40
    assert recorded["target_deployment_revision"] == SHA
    assert recorded["environment"] == "staging"
    assert recorded["account_id"] == ACCOUNT
    assert recorded["before"]["api"]["taskDefinition"] == PREFIX + "api:2"
    assert recorded["restored"] == ecs.state
    assert recorded["completed_at"] >= recorded["started_at"]
    assert s3.records[f"staging/backend/{SHA}.json"] == lookup
    assert ecs.state["api"]["taskDefinition"] == PREFIX + "api:1"
    assert ecs.waits.count("tasks_stopped") == 1  # rollback never downgrades the DB


def test_worker_promotion_updates_and_rolls_back_api_and_worker_together():
    ecs, s3 = Ecs(), S3()
    ecs.state["worker"] = {
        "taskDefinition": PREFIX + "worker:1",
        "desiredCount": 0,
    }
    config = copy.deepcopy(CONFIG)
    config["tasks"]["worker"] = PREFIX + "worker:1"
    deployment = PreparedDeployment(config, ecs, s3, Sts(), lambda _: None)

    result = deployment.deploy(SHA, IMAGE, WORKER_IMAGE)

    assert result["status"] == "healthy"
    assert ecs.state["api"] == {"taskDefinition": PREFIX + "api:2", "desiredCount": 1}
    assert ecs.state["worker"] == {"taskDefinition": PREFIX + "worker:2", "desiredCount": 1}
    assert {item["service"] for item in ecs.updates} == {"api", "worker"}
    assert result["previous"]["worker"]["desiredCount"] == 0
    assert result["target"]["worker"]["desiredCount"] == 1

    restored = deployment.rollback("c" * 40, SHA)

    assert restored["status"] == "rolled_back"
    assert ecs.state["api"] == {"taskDefinition": PREFIX + "api:1", "desiredCount": 1}
    assert ecs.state["worker"] == {"taskDefinition": PREFIX + "worker:1", "desiredCount": 0}


def test_worker_image_cannot_be_promoted_before_terraform_provisions_service():
    ecs, s3 = Ecs(), S3()
    deployment = PreparedDeployment(CONFIG, ecs, s3, Sts(), lambda _: None)

    with pytest.raises(DeploymentError, match="deployment setup was rejected"):
        deployment.deploy(SHA, IMAGE, WORKER_IMAGE)

    assert not ecs.registered and not ecs.runs and not ecs.updates


def test_api_only_compatibility_phase_keeps_an_existing_worker_on_its_image():
    ecs, s3 = Ecs(), S3()
    ecs.state["worker"] = {
        "taskDefinition": PREFIX + "worker:1",
        "desiredCount": 1,
    }
    config = copy.deepcopy(CONFIG)
    config["tasks"]["worker"] = PREFIX + "worker:1"
    deployment = PreparedDeployment(config, ecs, s3, Sts(), lambda _: None)

    result = deployment.deploy(SHA, IMAGE)

    assert result["status"] == "healthy"
    assert ecs.state["worker"] == {
        "taskDefinition": PREFIX + "worker:1",
        "desiredCount": 1,
    }
    assert [update["service"] for update in ecs.updates] == ["api"]
    assert set(result["previous"]) == {"api", "worker"}
    assert set(result["target"]) == {"api"}


@pytest.mark.parametrize("failure", ["update", "readiness"])
def test_explicit_rollback_failure_leaves_durable_audit_without_claiming_recovery(failure, capsys):
    ecs, s3 = Ecs(), S3()
    deployment = PreparedDeployment(CONFIG, ecs, s3, Sts(), lambda _: None)
    deployed = deployment.deploy(SHA, IMAGE)
    lookup = copy.deepcopy(s3.records[f"staging/backend/{SHA}.json"])
    capsys.readouterr()

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic private failure details must not enter audit")

    if failure == "update":
        ecs.update_service = unavailable
    else:
        deployment.readiness = unavailable
    with pytest.raises(DeploymentError, match="Rollback failed"):
        deployment.rollback("c" * 40, SHA)
    audits = list(operation_records(s3).values())
    assert len(audits) == 2
    audit = next(record for record in audits if record["operation"] == "rollback")
    assert audit["status"] == "rollback_failed"
    assert audit["revision"] == "c" * 40
    assert audit["target_deployment_revision"] == SHA
    assert audit["target_audit_key"] == deployed["audit_key"]
    assert audit["environment"] == CONFIG["environment"]
    assert audit["account_id"] == ACCOUNT
    assert audit["target"] == deployed["previous"]
    assert audit["restored"] is None
    assert audit["observed"] == ecs.state
    assert audit["audit_key"] in capsys.readouterr().err
    assert "private failure details" not in json.dumps(audit)
    assert s3.records[f"staging/backend/{SHA}.json"] == lookup
    assert ecs.waits.count("tasks_stopped") == 1


def test_repeated_same_revision_attempts_retain_each_deployment_and_rollback_record():
    ecs, s3 = Ecs(), S3()
    deployment = PreparedDeployment(CONFIG, ecs, s3, Sts(), lambda _: None)
    first = deployment.deploy(SHA, IMAGE)
    deployment.rollback(SHA, SHA)
    second = deployment.deploy(SHA, IMAGE)
    deployment.rollback(SHA, SHA)
    records = operation_records(s3)
    assert len(records) == 4
    assert len({value["operation_id"] for value in records.values()}) == 4
    assert records[first["audit_key"]] == first
    assert records[second["audit_key"]] == second
    assert first["audit_key"] != second["audit_key"]
    assert json.loads(s3.records[f"staging/backend/{SHA}.json"])["audit_key"] == second["audit_key"]
    assert {value["operation"] for value in records.values()} == {"deploy", "rollback"}
    assert ecs.waits.count("tasks_stopped") == 2  # two promotions, no rollback DDL


def test_partial_api_worker_rollback_records_observed_state_and_supports_legacy_lookup():
    ecs, s3 = Ecs(), S3()
    config = json.loads(json.dumps(CONFIG).replace("staging", "production"))
    prefix = PREFIX.replace("staging", "production")
    ecs.state = {
        "api": {"taskDefinition": prefix + "api:2", "desiredCount": 2},
        "worker": {"taskDefinition": prefix + "worker:2", "desiredCount": 2},
    }
    previous = {
        "api": {"taskDefinition": prefix + "api:1", "desiredCount": 1},
        "worker": {"taskDefinition": prefix + "worker:1", "desiredCount": 1},
    }
    s3.records[f"production/backend/{SHA}.json"] = json.dumps(
        {
            "schema": 1,
            "revision": SHA,
            "environment": "production",
            "account_id": ACCOUNT,
            "status": "healthy",
            "previous": previous,
        }
    ).encode()
    update = ecs.update_service

    def partial(**kwargs):
        if kwargs["service"] == "worker":
            raise RuntimeError("worker update failed")
        update(**kwargs)

    ecs.update_service = partial
    with pytest.raises(DeploymentError, match="Rollback failed"):
        Deployment(config, ecs, s3, Sts(), lambda _: None).rollback("c" * 40, SHA)
    audit = next(iter(operation_records(s3).values()))
    assert audit["status"] == "rollback_failed"
    assert audit["before"]["api"]["taskDefinition"] == prefix + "api:2"
    assert audit["target"] == previous
    assert audit["target_audit_key"] is None  # old schema-1 records still work
    assert audit["observed"] == ecs.state
    assert audit["observed"]["api"] == previous["api"]
    assert audit["observed"]["worker"]["taskDefinition"] == prefix + "worker:2"
    assert audit["restored"] is None
    assert "tasks_stopped" not in ecs.waits


@pytest.mark.parametrize("failure", ["readiness", "migration"])
def test_failed_later_deployment_does_not_overwrite_healthy_revision_lookup(failure):
    ecs, s3 = Ecs(), S3()
    deployment = PreparedDeployment(CONFIG, ecs, s3, Sts(), lambda _: None)
    first = deployment.deploy(SHA, IMAGE)
    lookup = copy.deepcopy(s3.records[f"staging/backend/{SHA}.json"])

    def unavailable(_):
        raise RuntimeError("unhealthy")

    if failure == "readiness":
        deployment.readiness = unavailable
    else:
        ecs.migration_exit = 1
    with pytest.raises(DeploymentError):
        deployment.deploy(SHA, IMAGE)
    assert s3.records[f"staging/backend/{SHA}.json"] == lookup
    assert json.loads(s3.records[first["audit_key"]]) == first
    audits = list(operation_records(s3).values())
    assert len(audits) == 2
    expected = "rollback_failed" if failure == "readiness" else "migration_failed"
    assert any(value["status"] == expected for value in audits)


def test_failed_rollback_is_not_reported_as_success():
    ecs, s3 = Ecs(), S3()

    def readiness(_):
        raise RuntimeError("unhealthy")

    with pytest.raises(DeploymentError):
        PreparedDeployment(CONFIG, ecs, s3, Sts(), readiness).deploy(SHA, IMAGE)
    assert next(iter(operation_records(s3).values()))["status"] == "rollback_failed"


@pytest.mark.parametrize(
    "change",
    [
        {"environment": "unknown"},
        {"cluster": "buddy-production"},
        {"bucket": "foreign"},
        {"api_url": "http://insecure.example.com"},
        {"network": {"assignPublicIp": "ENABLED"}},
    ],
)
def test_wrong_environment_or_unsafe_network_is_rejected(change):
    with pytest.raises(DeploymentError):
        validate({**CONFIG, **change}, SHA)


def test_explicit_staging_public_egress_runs_real_deployment_driver():
    config = {
        **CONFIG,
        "egress_mode": "public_ip",
        "network": {**CONFIG["network"], "subnets": ["subnet-public"], "assignPublicIp": "ENABLED"},
    }
    validate(config, SHA)
    ecs, s3 = Ecs(expected_public_ip="ENABLED"), S3()
    PreparedDeployment(config, ecs, s3, Sts(), lambda _: None).deploy(SHA, IMAGE)
    assert len(ecs.runs) == 1
    assert ecs.updates


@pytest.mark.parametrize(
    "change",
    [
        {"egress_mode": "unknown"},
        {"egress_mode": "public_ip"},  # Must also explicitly select public networking.
        {"egress_mode": "public_ip", "environment": "production"},
    ],
)
def test_unreviewed_or_production_public_egress_is_rejected(change):
    with pytest.raises(DeploymentError):
        validate({**CONFIG, **change}, SHA)


@pytest.mark.parametrize(
    "image",
    [
        IMAGE.replace(ACCOUNT, "000000000000"),
        IMAGE.replace("staging", "production"),
        IMAGE.split("@")[0] + ":latest",
    ],
)
def test_only_private_environment_pinned_images_are_allowed(image):
    with pytest.raises(DeploymentError):
        image_digest(CONFIG, image, "api")


def test_cross_environment_rollback_record_is_rejected():
    ecs, s3 = Ecs(), S3()
    deployment = PreparedDeployment(CONFIG, ecs, s3, Sts(), lambda _: None)
    deployment.deploy(SHA, IMAGE)
    key = f"staging/backend/{SHA}.json"
    value = json.loads(s3.records[key])
    value["environment"] = "production"
    s3.records[key] = json.dumps(value).encode()
    before = copy.deepcopy(ecs.updates)
    with pytest.raises(DeploymentError):
        deployment.rollback("c" * 40, SHA)
    assert ecs.updates == before
    failures = list(operation_records(s3).values())
    audit = next(record for record in failures if record["operation"] == "rollback")
    assert audit["status"] == "rollback_failed"
    assert audit["restored"] is None and audit["target"] is None


def test_missing_failed_or_wrong_revision_ci_blocks_release():
    checks = [
        {"name": name, "head_sha": SHA, "status": "completed", "conclusion": "success"}
        for name in [
            "CI",
            "Terraform boundaries",
            "API container and database bootstrap",
            "Native package (windows-latest)",
            "Native package (macos-latest)",
        ]
    ]
    assert approved_checks(checks, SHA)
    assert not approved_checks(checks[:-1], SHA)
    assert not approved_checks([{**entry, "conclusion": "failure"} for entry in checks], SHA)
    assert not approved_checks(checks, "c" * 40)


def test_modified_installer_or_path_traversal_is_rejected(tmp_path):
    path = tmp_path / "Buddy-test.exe"
    path.write_bytes(b"synthetic installer, never executed")
    result = manifest(tmp_path, SHA, "preview")
    (tmp_path / "manifest.json").write_text(json.dumps(result))
    assert verify(tmp_path) == [path]
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        verify(tmp_path)
    result["artifacts"][0]["name"] = "../Buddy-test.exe"
    (tmp_path / "manifest.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="path"):
        verify(tmp_path)


def test_publication_uploads_only_verified_entries_not_the_build_tree(tmp_path, monkeypatch):
    from frontend.scripts.release.publish import publish

    artifact = tmp_path / "Buddy-test.exe"
    artifact.write_bytes(b"synthetic artifact")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest(tmp_path, SHA, "preview")))
    (tmp_path / "not-a-release.exe").write_bytes(b"must not upload")
    monkeypatch.setenv("RELEASE_TARGET", "staging")
    monkeypatch.setenv("AWS_ACCOUNT", ACCOUNT)
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("RELEASE_BUCKET", CONFIG["bucket"])

    class Bucket:
        def __init__(self):
            self.keys = []

        def put_object(self, **kwargs):
            self.keys.append(kwargs["Key"])
            assert kwargs["Body"].read()

    bucket = Bucket()
    publish(tmp_path, bucket, Sts())
    assert {key.rsplit("/", 1)[1] for key in bucket.keys} == {"Buddy-test.exe", "manifest.json"}
    monkeypatch.setenv("RELEASE_TARGET", "production")
    with pytest.raises(ValueError):
        publish(tmp_path, bucket, Sts())


def test_production_and_branch_gates_fail_before_credentials_or_network(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("RELEASE_TARGET", "production")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.delenv("STAGING_ACCEPTED_REVISION", raising=False)
    monkeypatch.delenv("CALLBACK_RELEASE_APPROVED", raising=False)
    monkeypatch.delenv("RELEASE_OPERATION", raising=False)
    with pytest.raises(ValueError, match="staging acceptance"):
        release_gate["main"]()
    monkeypatch.setenv("RELEASE_OPERATION", "rollback")
    assert release_gate["main"]() == 0
    monkeypatch.setenv("GITHUB_REF", "refs/pull/1/merge")
    with pytest.raises(ValueError, match="main revision"):
        release_gate["main"]()


@pytest.mark.parametrize(
    "boundary", ["before-migration", "during-migration", "unchanged", "lookup-failure"]
)
def test_automatic_deploy_rechecks_main_at_live_boundaries(monkeypatch, boundary):
    from types import SimpleNamespace

    from backend.scripts.release import deploy as driver

    monkeypatch.setenv("AUTOMATIC_STAGING", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Buddy-presenter/Buddy")
    ecs, s3 = Ecs(), S3()
    lookups = []

    def lookup(command, **kwargs):
        lookups.append(command)
        assert kwargs["timeout"] == 30
        if boundary == "lookup-failure":
            raise RuntimeError("GitHub unavailable")
        stale = boundary == "before-migration" or (boundary == "during-migration" and ecs.runs)
        return SimpleNamespace(stdout=json.dumps({"object": {"sha": "b" * 40 if stale else SHA}}))

    monkeypatch.setattr(driver.subprocess, "run", lookup)
    deployment = PreparedDeployment(CONFIG, ecs, s3, Sts(), lambda _: None)
    if boundary == "unchanged":
        assert deployment.deploy(SHA, IMAGE)["status"] == "healthy"
        assert len(lookups) == 2 and len(ecs.runs) == 1 and ecs.updates
        return
    with pytest.raises(DeploymentError, match="no traffic promoted"):
        deployment.deploy(SHA, IMAGE)
    assert ecs.updates == []
    assert len(ecs.runs) == (1 if boundary == "during-migration" else 0)
    audit = next(iter(operation_records(s3).values()))
    assert audit["status"] == "setup_failed"
    assert audit["stage"] == (
        "promotion_freshness" if boundary == "during-migration" else "migration_freshness"
    )
    assert audit["started_audit_key"]
