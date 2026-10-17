"""Read-only live staging checks; recovery/provider drills require separate evidence."""

import argparse
import json
import re
import ssl
from datetime import UTC, datetime
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

import boto3


def require(condition, message):
    if not condition:
        raise ValueError(message)


def ready(origin):
    url = urlsplit(origin)
    require(
        url.scheme == "https"
        and url.hostname
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment
        and url.path in ("", "/")
        and url.port in (None, 443),
        "Expected an HTTPS API origin without credentials or a path",
    )
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = HTTPSConnection(url.hostname, timeout=20, context=context)
    try:
        connection.request("GET", "/api/v1/ready")
        response = connection.getresponse()
        require(response.status == 200, "HTTPS readiness failed (redirects are not followed)")
        body = json.loads(response.read(65537))
        data = body.get("data", {})
        require(data.get("status") == "ready", "API did not report ready")
        require(
            any(
                item.get("name") == "database" and item.get("status") == "ready"
                for item in data.get("dependencies", [])
            ),
            "Readiness must include a real migrated database",
        )
    finally:
        connection.close()


def verify(session, deployment, revision, expected_image):
    config = deployment["deployment_configuration"]
    resources = deployment["verification_resources"]
    require(config["environment"] == "staging", "This verifier is staging-only")
    require(re.fullmatch(r"[a-f0-9]{40}", revision), "A full reviewed revision is required")
    account = session.client("sts").get_caller_identity()["Account"]
    require(account == config["account_id"], "AWS account differs from the reviewed configuration")
    require(config["cluster"] == "buddy-staging", "Unexpected staging cluster")
    prefix = f"{account}.dkr.ecr.{config['region']}.amazonaws.com/buddy-staging/api@sha256:"
    require(
        expected_image.startswith(prefix)
        and re.fullmatch(r"[a-f0-9]{64}", expected_image[len(prefix) :]),
        "Expected an immutable staging API image from this account and region",
    )

    def client(name):
        return session.client(name, region_name=config["region"])

    ecs = client("ecs")
    result = ecs.describe_services(cluster=config["cluster"], services=["api"])
    require(not result.get("failures") and len(result["services"]) == 1, "API service missing")
    service = result["services"][0]
    network = service.get("networkConfiguration", {}).get("awsvpcConfiguration", {})
    require(
        network.get("assignPublicIp") == config["network"]["assignPublicIp"]
        and set(network.get("subnets", [])) == set(config["network"]["subnets"])
        and network.get("securityGroups") == [resources["api_security_group"]],
        "API networking differs from the reviewed egress configuration",
    )
    group = client("ec2").describe_security_groups(GroupIds=[resources["api_security_group"]])[
        "SecurityGroups"
    ][0]
    permissions = group.get("IpPermissions", [])
    require(
        len(permissions) == 1
        and permissions[0].get("IpProtocol") == "tcp"
        and permissions[0].get("FromPort") == permissions[0].get("ToPort") == 8000
        and not permissions[0].get("IpRanges")
        and not permissions[0].get("Ipv6Ranges")
        and not permissions[0].get("PrefixListIds")
        and [pair["GroupId"] for pair in permissions[0].get("UserIdGroupPairs", [])]
        == [resources["alb_security_group"]],
        "API ingress must allow only the reviewed ALB security group",
    )
    require(
        service["desiredCount"] == service["runningCount"] == 1
        and service["pendingCount"] == 0
        and service["taskDefinition"] == config["tasks"]["api"]
        and len(service["deployments"]) == 1
        and service["deployments"][0].get("rolloutState") == "COMPLETED",
        "API is not stable at the exact reviewed task definition",
    )
    task = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
    containers = task["containerDefinitions"]
    require(len(containers) == 1 and containers[0]["image"] == expected_image, "Wrong API image")
    images = client("ecr").describe_images(
        repositoryName="buddy-staging/api", imageIds=[{"imageDigest": expected_image.split("@")[1]}]
    )["imageDetails"]
    require(
        len(images) == 1
        and any(
            tag == revision or tag.startswith(revision + "-")
            for tag in images[0].get("imageTags", [])
        ),
        "Registry digest has no tag for the reviewed revision; retain build provenance separately",
    )
    require(resources["database_id"] == "buddy-staging", "Unexpected database")
    db = client("rds").describe_db_instances(DBInstanceIdentifier="buddy-staging")["DBInstances"][0]
    require(
        db["DBInstanceStatus"] == "available"
        and not db["PubliclyAccessible"]
        and db["StorageEncrypted"]
        and db["DeletionProtection"]
        and db["BackupRetentionPeriod"] >= 1,
        "Database availability, isolation, encryption or recovery protection failed",
    )
    alb = client("elbv2").describe_load_balancers(
        LoadBalancerArns=[resources["load_balancer_arn"]]
    )["LoadBalancers"][0]
    require(alb["Scheme"] == "internal", "AWS HTTPS origin must be private")
    s3 = client("s3")
    for kind in ("data", "releases"):
        bucket = resources["buckets"][kind]
        require(bucket == f"buddy-staging-{account}-{kind}", "Unexpected bucket")
        block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        require(
            all(
                block.get(key)
                for key in (
                    "BlockPublicAcls",
                    "IgnorePublicAcls",
                    "BlockPublicPolicy",
                    "RestrictPublicBuckets",
                )
            ),
            "Bucket public access is not fully blocked",
        )
        require(
            s3.get_bucket_versioning(Bucket=bucket).get("Status") == "Enabled",
            "Bucket versioning missing",
        )
        rules = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"][
            "Rules"
        ]
        require(
            all(
                rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
                for rule in rules
            )
            and rules,
            "Bucket KMS encryption missing",
        )
    distribution = session.client("cloudfront").get_distribution(Id=resources["cloudfront_id"])[
        "Distribution"
    ]
    require(distribution["Status"] == "Deployed", "CloudFront has not deployed")
    behavior = distribution["DistributionConfig"]["DefaultCacheBehavior"]
    require(
        behavior["ViewerProtocolPolicy"] == "https-only"
        and all(behavior[key] == 0 for key in ("MinTTL", "DefaultTTL", "MaxTTL"))
        and distribution["DistributionConfig"].get("WebACLId"),
        "Public HTTPS, cache isolation or edge WAF configuration failed",
    )
    ready(config["api_url"])
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "account_id": account,
        "region": config["region"],
        "reviewed_revision": revision,
        "api_image": expected_image,
        "api_url": config["api_url"],
        "read_only_checks": "passed",
        "remaining_acceptance": [
            "private object/queue operations and denied public requests",
            "restricted DB roles and verified TLS",
            "Cognito provider acceptance and live SSE timing",
            "API restart durability",
            "real migration rejection and audited rollback",
            "isolated backup restore",
            "protected GitHub deployment environment and budget alerts",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outputs", type=Path, help="terraform output -json (keep local)")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    try:
        outputs = json.loads(args.outputs.read_text(encoding="utf-8-sig"))
        result = verify(
            boto3.Session(profile_name=args.profile),
            outputs["deployment"]["value"],
            args.revision,
            args.image,
        )
    except Exception:
        # AWS/DB errors can contain sensitive values. Never emit their raw text.
        print(json.dumps({"read_only_checks": "failed", "acceptance_complete": False}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
