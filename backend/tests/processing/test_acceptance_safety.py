"""Disposable operator test guards, not evidence that live AWS checks passed."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def operator(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "acceptance_operator", scripts / "staging_worker_acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def journal():
    run = "a" * 12
    return {
        "run": run,
        "database": f"buddy_worker_acceptance_{run}_test",
        "db_role": f"buddy_w_{run}",
        "cluster": "buddy-staging",
        "prefix": f"sources/acceptance/{run}/",
        "region": "eu-central-1",
        "bucket": "buddy-staging-549327541867-data",
    }


def test_isolated_target_accepted(operator, journal):
    operator.validate_journal(journal)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("run", "';DROP DATABASE buddy;--"),
        ("database", "buddy"),
        ("database", "some_other_test"),
        ("db_role", "buddyadmin"),
        ("cluster", "buddy-production"),
        ("prefix", "sources/"),
        ("region", "us-east-1"),
        ("bucket", "buddy-production-data"),
        ("roles", {"worker": {"name": "buddy-staging-worker", "arn": "unrelated"}}),
        ("roles", {"unknown": {}}),
        (
            "queues",
            ["https://sqs.eu-central-1.amazonaws.com/549327541867/buddy-staging-processing"],
        ),
        ("log_group", "/buddy/staging/api"),
        ("runtime_secret", "arn:aws:secretsmanager:eu-central-1:549327541867:secret:database"),
        (
            "definitions",
            ["arn:aws:ecs:eu-central-1:549327541867:task-definition/buddy-staging-api:10"],
        ),
        ("image_tag", "latest"),
    ],
)
def test_cleanup_rejects_unrelated_targets_before_aws(operator, journal, key, value, monkeypatch):
    def unexpected_network(*args, **kwargs):
        raise AssertionError("Unsafe journal must be rejected before AWS access")

    monkeypatch.setattr(operator.boto3, "client", unexpected_network)
    with pytest.raises(ValueError):
        operator.Acceptance("unused.json", {**journal, key: value})


def test_cannot_stop_or_inspect_an_unowned_task(operator, journal):
    class ECS:
        def describe_tasks(self, **kwargs):
            assert kwargs["include"] == ["TAGS"]
            return {"tasks": [{"tags": [{"key": "Application", "value": "Buddy"}]}]}

    acceptance = operator.Acceptance.__new__(operator.Acceptance)
    acceptance.state = journal
    acceptance.clients = {"ecs": ECS()}
    with pytest.raises(ValueError, match="unowned ECS task"):
        acceptance.describe("unrelated-task")


def test_disposable_worker_role_is_scoped_to_claude_inference(operator, journal, monkeypatch):
    acceptance = operator.Acceptance.__new__(operator.Acceptance)
    acceptance.state = {
        **journal,
        "queue_arn": "arn:aws:sqs:eu-central-1:549327541867:acceptance",
        "kms": "arn:aws:kms:eu-central-1:549327541867:key/test",
        "log_group": "/buddy/staging/acceptance/test",
        "runtime_secret": "arn:aws:secretsmanager:eu-central-1:549327541867:secret:test",
        "admin_secret": "arn:aws:secretsmanager:eu-central-1:549327541867:secret:admin",
    }
    policies = {}
    monkeypatch.setattr(
        acceptance,
        "role",
        lambda label, statements: policies.__setitem__(label, statements),
    )
    monkeypatch.setattr(operator.time, "sleep", lambda _seconds: None)

    acceptance.roles()

    bedrock = next(
        statement
        for statement in policies["worker"]
        if "bedrock:InvokeModel" in statement["Action"]
    )
    assert set(bedrock["Resource"]) == {
        "arn:aws:bedrock:eu-central-1:549327541867:inference-profile/eu.anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:eu-central-1::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:eu-north-1::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:eu-south-1::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:eu-south-2::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-sonnet-4-6",
        "arn:aws:bedrock:eu-central-1:549327541867:project/default",
    }
    evidence_write = next(
        statement for statement in policies["worker"] if "s3:PutObject" in statement["Action"]
    )
    assert (
        evidence_write["Resource"]
        == "arn:aws:s3:::buddy-staging-549327541867-data/sources/*/*/validation/*"
    )
    assert set(evidence_write["Action"]) == {"s3:PutObject", "s3:DeleteObjectVersion"}
    evidence_versions = next(
        statement
        for statement in policies["worker"]
        if "s3:ListBucketVersions" in statement["Action"]
    )
    assert evidence_versions["Condition"]["StringLike"]["s3:prefix"] == "sources/*/*/validation/*"
    evidence_key = next(
        statement
        for statement in policies["worker"]
        if "kms:GenerateDataKey" in statement["Action"]
    )
    assert (
        evidence_key["Condition"]["StringEquals"]["kms:ViaService"]
        == "s3.eu-central-1.amazonaws.com"
    )
