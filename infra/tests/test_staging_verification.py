"""Live-verifier failure boundaries; these are not evidence of an AWS deployment."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "staging_verify", Path(__file__).resolve().parents[1] / "staging/verify.py"
)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)
SHA = "a" * 40
IMAGE = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:" + "b" * 64


@pytest.mark.parametrize(
    "origin",
    [
        "http://api.example.com",
        "https://user:password@api.example.com",
        "https://api.example.com/path",
    ],
)
def test_readiness_rejects_plaintext_credentials_and_paths(origin):
    with pytest.raises(ValueError, match="HTTPS API origin"):
        verify.ready(origin)


@pytest.mark.parametrize(
    ("status", "dependencies", "passes"),
    [(200, [{"name": "database", "status": "ready"}], True), (302, [], False), (200, [], False)],
)
def test_readiness_requires_verified_tls_and_database(monkeypatch, status, dependencies, passes):
    def connect(host, *, timeout, context):
        assert host == "d123.cloudfront.net"
        assert context.check_hostname
        assert context.verify_mode == verify.ssl.CERT_REQUIRED
        assert context.minimum_version >= verify.ssl.TLSVersion.TLSv1_2
        return SimpleNamespace(
            request=lambda method, path: None,
            getresponse=lambda: SimpleNamespace(
                status=status,
                read=lambda size: json.dumps(
                    {"data": {"status": "ready", "dependencies": dependencies}}
                ).encode(),
            ),
            close=lambda: None,
        )

    monkeypatch.setattr(verify, "HTTPSConnection", connect)
    if passes:
        verify.ready("https://d123.cloudfront.net")
    else:
        with pytest.raises(ValueError):
            verify.ready("https://d123.cloudfront.net")


@pytest.fixture
def live_boundary(monkeypatch):
    config = {
        "environment": "staging",
        "account_id": "123456789012",
        "region": "eu-central-1",
        "cluster": "buddy-staging",
        "tasks": {"api": "task:1"},
        "api_url": "https://d123.cloudfront.net",
        "network": {"assignPublicIp": "ENABLED", "subnets": ["subnet-public"]},
    }
    resources = {
        "api_security_group": "sg-api",
        "alb_security_group": "sg-alb",
        "database_id": "buddy-staging",
        "load_balancer_arn": "alb",
        "cloudfront_id": "distribution",
        "buckets": {kind: f"buddy-staging-123456789012-{kind}" for kind in ("data", "releases")},
    }
    state = {
        "account": "123456789012",
        "service": {
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "assignPublicIp": "ENABLED",
                    "subnets": ["subnet-public"],
                    "securityGroups": ["sg-api"],
                }
            },
            "desiredCount": 1,
            "runningCount": 1,
            "pendingCount": 0,
            "taskDefinition": "task:1",
            "deployments": [{"rolloutState": "COMPLETED"}],
        },
        "task": {"containerDefinitions": [{"image": IMAGE}]},
        "db": {
            "DBInstanceStatus": "available",
            "PubliclyAccessible": False,
            "StorageEncrypted": True,
            "DeletionProtection": True,
            "BackupRetentionPeriod": 1,
        },
        "behavior": {
            "ViewerProtocolPolicy": "https-only",
            "MinTTL": 0,
            "DefaultTTL": 0,
            "MaxTTL": 0,
        },
    }
    state["permissions"] = [
        {
            "IpProtocol": "tcp",
            "FromPort": 8000,
            "ToPort": 8000,
            "UserIdGroupPairs": [{"GroupId": "sg-alb"}],
        }
    ]
    client = SimpleNamespace(
        describe_security_groups=lambda **kwargs: {
            "SecurityGroups": [{"IpPermissions": state["permissions"]}]
        },
        get_caller_identity=lambda: {"Account": state["account"]},
        describe_services=lambda **kwargs: {"services": [state["service"]]},
        describe_task_definition=lambda **kwargs: {"taskDefinition": state["task"]},
        describe_images=lambda **kwargs: {"imageDetails": [{"imageTags": [SHA + "-123-1"]}]},
        describe_db_instances=lambda **kwargs: {"DBInstances": [state["db"]]},
        describe_load_balancers=lambda **kwargs: {"LoadBalancers": [{"Scheme": "internal"}]},
        get_public_access_block=lambda **kwargs: {
            "PublicAccessBlockConfiguration": {
                key: True
                for key in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            }
        },
        get_bucket_versioning=lambda **kwargs: {"Status": "Enabled"},
        get_bucket_encryption=lambda **kwargs: {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
            }
        },
        get_distribution=lambda **kwargs: {
            "Distribution": {
                "Status": "Deployed",
                "DistributionConfig": {
                    "DefaultCacheBehavior": state["behavior"],
                    "WebACLId": "waf",
                },
            }
        },
    )
    monkeypatch.setattr(verify, "ready", lambda origin: None)
    return (
        SimpleNamespace(client=lambda *args, **kwargs: client),
        {"deployment_configuration": config, "verification_resources": resources},
        state,
    )


def test_read_only_success_keeps_unexecuted_drills_pending(live_boundary):
    session, outputs, _ = live_boundary
    result = verify.verify(session, outputs, SHA, IMAGE)
    assert result["read_only_checks"] == "passed"
    assert "isolated backup restore" in result["remaining_acceptance"]
    assert "acceptance_complete" not in result


@pytest.mark.parametrize(
    "failure",
    ["account", "drift", "public_db", "no_backups", "wrong_image", "caching", "network", "public_api"],
)
def test_live_drift_and_isolation_fail_closed(live_boundary, failure):
    session, outputs, state = live_boundary
    if failure == "account":
        state["account"] = "999999999999"
    elif failure == "drift":
        state["service"]["taskDefinition"] = "task:2"
    elif failure == "public_db":
        state["db"]["PubliclyAccessible"] = True
    elif failure == "no_backups":
        state["db"]["BackupRetentionPeriod"] = 0
    elif failure == "wrong_image":
        state["task"]["containerDefinitions"][0]["image"] = IMAGE.replace("staging", "production")
    elif failure == "network":
        state["service"]["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] = (
            "DISABLED"
        )
    elif failure == "public_api":
        state["permissions"][0]["IpRanges"] = [{"CidrIp": "0.0.0.0/0"}]
    else:
        state["behavior"]["MaxTTL"] = 60
    with pytest.raises(ValueError):
        verify.verify(session, outputs, SHA, IMAGE)
