"""Explicit ECS migration/promotion with health checks and recorded safe rollback."""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.request import urlopen
from uuid import uuid4


class DeploymentError(Exception):
    pass


def require_current_main(environment: str, revision: str) -> None:
    """Revalidate automatic staging at each live boundary, not just before build."""
    if os.environ.get("AUTOMATIC_STAGING") != "true":
        return
    if environment != "staging":
        raise DeploymentError("Automatic promotion is staging-only.")
    result = subprocess.run(
        ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/git/ref/heads/main"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if json.loads(result.stdout)["object"]["sha"] != revision:
        raise DeploymentError("Automatic staging revision has been superseded on main.")


def validate(config: dict[str, Any], revision: str) -> None:
    if config.get("environment") not in {"staging", "production"}:
        raise DeploymentError("Unknown deployment environment.")
    account = config.get("account_id", "")
    region = config.get("region", "")
    environment = config["environment"]
    if not re.fullmatch(r"\d{12}", account) or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", region):
        raise DeploymentError("Invalid account or region.")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise DeploymentError("A full reviewed commit SHA is required.")
    if config.get("cluster") != "buddy-" + environment:
        raise DeploymentError("Cluster must match the selected environment.")
    if config.get("bucket") != f"buddy-{environment}-{account}-releases":
        raise DeploymentError("Audit bucket must match the selected account/environment.")
    if not re.fullmatch(r"https://[a-z0-9.-]+\.[a-z]{2,}", config.get("api_url", "")):
        raise DeploymentError("A verified HTTPS API origin is required.")
    network = config.get("network", {})
    egress = config.get("egress_mode", "private_nat")
    expected_public_ip = "ENABLED" if egress == "public_ip" else "DISABLED"
    if (
        egress not in {"private_nat", "public_ip"}
        or (environment == "production" and egress != "private_nat")
        or network.get("assignPublicIp") != expected_public_ip
        or not network.get("subnets")
        or not network.get("securityGroups")
    ):
        raise DeploymentError(
            "Migration networking must match the explicit environment egress mode."
        )
    for name in ("api", "migration"):
        arn = config.get("tasks", {}).get(name, "")
        if not re.fullmatch(
            rf"arn:aws:ecs:{region}:{account}:task-definition/buddy-{environment}-{name}:\d+", arn
        ):
            raise DeploymentError("Task definition belongs to another account/environment.")


def image_digest(config: dict[str, Any], image: str, service: str) -> str:
    expected = (
        f"{config['account_id']}.dkr.ecr.{config['region']}.amazonaws.com/"
        f"buddy-{config['environment']}/{service}@sha256:"
    )
    if not image.startswith(expected) or not re.fullmatch(r"[a-f0-9]{64}", image[len(expected) :]):
        raise DeploymentError("Image must be this environment's private immutable ECR digest.")
    return image


def healthy(origin: str) -> None:
    with urlopen(origin + "/api/v1/ready", timeout=10) as response:
        if response.status != 200:
            raise DeploymentError("Readiness failed.")
        body = json.loads(response.read(65537))
        if body.get("data", {}).get("status") != "ready":
            raise DeploymentError("Readiness response is not healthy.")


class Deployment:
    def __init__(
        self,
        config: dict[str, Any],
        ecs: Any,
        s3: Any,
        sts: Any,
        readiness: Callable[[str], None] = healthy,
        compatibility_report: dict[str, Any] | None = None,
    ):
        self.config, self.ecs, self.s3, self.readiness = config, ecs, s3, readiness
        self.compatibility_report = compatibility_report
        if sts.get_caller_identity()["Account"] != config["account_id"]:
            raise DeploymentError("AWS identity does not match deployment account.")

    def compatible(
        self, candidate: str, rollback: dict[str, dict[str, Any]], before: dict[str, dict[str, Any]]
    ) -> None:
        """Bind tested container results to the actual immediate rollback target."""
        if self.config["environment"] != "staging":
            return  # This transition does not replace production's acceptance policy.
        proof = self.compatibility_report or {}
        required = {
            "readiness",
            "analysis",
            "legacy_writes",
            "sessions",
            "owner_isolation",
            "immutability",
        }
        checks = proof.get("checks")
        if (
            set(rollback) != {"api"}
            or set(before) != {"api"}
            or not isinstance(checks, dict)
            or proof.get("passed") is not True
            or proof.get("before") != before
            or proof.get("candidate_image") != candidate
            or proof.get("rollback") != rollback
            or set(checks) != required
            or not all(value is True for value in checks.values())
        ):
            raise DeploymentError("Staging requires exact-image rollback compatibility evidence.")
        source = self.ecs.describe_task_definition(
            taskDefinition=rollback["api"]["taskDefinition"]
        )["taskDefinition"]
        containers = source.get("containerDefinitions", [])
        if len(containers) != 1 or containers[0]["image"] != proof.get("rollback_image"):
            raise DeploymentError("Rollback image drifted from compatibility evidence.")
        image_digest(self.config, proof["rollback_image"], "api")

    def require_stable_source(self, expected: dict[str, dict[str, Any]]) -> None:
        """Do not migrate while incompatible tasks from an earlier rollout still drain."""
        if self.config["environment"] != "staging":
            return
        response = self.ecs.describe_services(cluster=self.config["cluster"], services=["api"])
        services = response.get("services", [])
        if response.get("failures") or len(services) != 1:
            raise DeploymentError("The staging rollback source is unavailable.")
        service = services[0]
        deployments = service.get("deployments", [])
        target = expected["api"]
        if (
            service.get("taskDefinition") != target["taskDefinition"]
            or service.get("desiredCount") != target["desiredCount"]
            or service.get("runningCount") != target["desiredCount"]
            or service.get("pendingCount") != 0
            or len(deployments) != 1
            or deployments[0].get("status") != "PRIMARY"
            or deployments[0].get("rolloutState") != "COMPLETED"
            or deployments[0].get("taskDefinition") != target["taskDefinition"]
        ):
            raise DeploymentError("Staging must finish its previous rollout before migration.")

    def states(self, services: list[str]) -> dict[str, dict[str, Any]]:
        response = self.ecs.describe_services(cluster=self.config["cluster"], services=services)
        if response.get("failures") or len(response.get("services", [])) != len(services):
            raise DeploymentError("Expected initialized services are unavailable.")
        result = {}
        for service in response["services"]:
            result[service["serviceName"]] = {
                "taskDefinition": service["taskDefinition"],
                "desiredCount": service["desiredCount"],
            }
        return result

    def task(self, service: str, image: str) -> str:
        arn = self.config["tasks"][service]
        source = self.ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]
        allowed = (
            "family",
            "taskRoleArn",
            "executionRoleArn",
            "networkMode",
            "containerDefinitions",
            "volumes",
            "placementConstraints",
            "requiresCompatibilities",
            "cpu",
            "memory",
            "runtimePlatform",
            "ephemeralStorage",
        )
        definition = {key: source[key] for key in allowed if key in source}
        if definition["family"] != f"buddy-{self.config['environment']}-{service}":
            raise DeploymentError("Unexpected task family.")
        containers = definition["containerDefinitions"]
        if len(containers) != 1 or containers[0]["name"] != service:
            raise DeploymentError("Unexpected container layout.")
        if service == "worker" and not containers[0].get("healthCheck"):
            raise DeploymentError("A real worker image and container health check are required.")
        containers[0]["image"] = image_digest(
            self.config, image, "api" if service == "migration" else service
        )
        return self.ecs.register_task_definition(
            **definition,
            tags=[
                {"key": "Application", "value": "Buddy"},
                {"key": "Environment", "value": self.config["environment"]},
            ],
        )["taskDefinition"]["taskDefinitionArn"]

    def migrate(self, task: str) -> None:
        response = self.ecs.run_task(
            cluster=self.config["cluster"],
            taskDefinition=task,
            launchType="FARGATE",
            count=1,
            networkConfiguration={"awsvpcConfiguration": self.config["network"]},
        )
        if response.get("failures") or len(response.get("tasks", [])) != 1:
            raise DeploymentError("Migration could not start; no traffic promoted.")
        arn = response["tasks"][0]["taskArn"]
        self.ecs.get_waiter("tasks_stopped").wait(
            cluster=self.config["cluster"],
            tasks=[arn],
            WaiterConfig={"Delay": 10, "MaxAttempts": 90},
        )
        tasks = self.ecs.describe_tasks(cluster=self.config["cluster"], tasks=[arn])
        containers = tasks.get("tasks", [{}])[0].get("containers", [])
        if tasks.get("failures") or len(containers) != 1 or containers[0].get("exitCode") != 0:
            raise DeploymentError("Migration failed; no traffic promoted.")

    def settle(self, expected: dict[str, dict[str, Any]]) -> None:
        self.ecs.get_waiter("services_stable").wait(
            cluster=self.config["cluster"],
            services=list(expected),
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )
        actual = self.states(list(expected))
        if actual != expected:
            raise DeploymentError("Service rolled back or drifted from the expected revision.")
        if expected["api"]["desiredCount"]:
            self.readiness(self.config["api_url"])

    def restore(self, states: dict[str, dict[str, Any]]) -> None:
        for service, state in states.items():
            self.ecs.update_service(cluster=self.config["cluster"], service=service, **state)
        self.settle(states)

    def audit(self, operation: str, revision: str) -> dict[str, Any]:
        return {
            "schema": 2,
            "operation": operation,
            "operation_id": uuid4().hex,
            "revision": revision,
            "environment": self.config["environment"],
            "account_id": self.config["account_id"],
            "started_at": datetime.now(UTC).isoformat(),
        }

    def record(self, revision: str, value: dict[str, Any], *, finished: bool = True) -> None:
        # Each attempt has its own immutable history object, including reruns of
        # the same SHA and multiple rollbacks from the same operator revision.
        prefix = f"{self.config['environment']}/backend/operations/{revision}"
        suffix = ".json" if finished else ".started.json"
        key = f"{prefix}/{value['operation_id']}{suffix}"
        value["audit_key"] = key
        if finished:
            value["completed_at"] = datetime.now(UTC).isoformat()
        else:
            value["started_audit_key"] = key
        self.s3.put_object(
            Bucket=self.config["bucket"],
            Key=key,
            Body=json.dumps(value).encode(),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        # Operators can fetch the exact retained record even on a failed run;
        # the deployment role deliberately has no bucket-wide list permission.
        print(f"Audit record saved: {key}", file=sys.stderr)
        if finished and value["operation"] == "deploy" and value["status"] == "healthy":
            # Compatibility lookup for the existing SHA-based rollback input.
            # This is the latest healthy attempt, not the retained audit history.
            # Failed attempts and rollback operations never overwrite this lookup.
            self.s3.put_object(
                Bucket=self.config["bucket"],
                Key=f"{self.config['environment']}/backend/{revision}.json",
                Body=json.dumps(value).encode(),
                ContentType="application/json",
            )

    def deploy(
        self, revision: str, api_image: str, worker_image: str | None = None
    ) -> dict[str, Any]:
        validate(self.config, revision)
        services = ["api"] + (["worker"] if worker_image else [])
        audit = {
            **self.audit("deploy", revision),
            "status": "started",
            "stage": "definition_setup",
            "previous": None,
            "target": None,
            "migration": None,
        }
        # If durable intent cannot be saved, do not register or run a migration.
        # A retained start without a terminal record also identifies interrupted runs.
        self.record(revision, audit, finished=False)
        try:
            previous = self.states(services)
            audit["previous"] = previous
            audit["stage"] = "compatibility"
            self.require_stable_source(previous)
            self.compatible(api_image, previous, previous)
            audit["compatibility"] = self.compatibility_report
            audit["stage"] = "definition_setup"
            definitions = {"api": self.task("api", api_image)}
            audit["stage"] = "migration_setup"
            definitions["migration"] = self.task("migration", api_image)
            audit["migration"] = definitions["migration"]
            if worker_image:
                audit["stage"] = "worker_setup"
                definitions["worker"] = self.task("worker", worker_image)
            target = {
                service: {
                    "taskDefinition": definitions[service],
                    "desiredCount": max(
                        previous[service]["desiredCount"],
                        2 if self.config["environment"] == "production" else 1,
                    ),
                }
                for service in services
            }
            audit["target"] = target
            audit["stage"] = "migration"
            if self.states(services) != previous:
                raise DeploymentError(
                    "Service changed after compatibility testing; no migration started."
                )
            self.require_stable_source(previous)
            audit["stage"] = "migration_freshness"
            require_current_main(self.config["environment"], revision)
            audit["stage"] = "migration"
            self.migrate(definitions["migration"])
            # Let an already-started migration finish safely, but do not promote
            # stale traffic if main advanced while that migration was running.
            audit["stage"] = "promotion_freshness"
            require_current_main(self.config["environment"], revision)
        except Exception:
            migration_failed = audit["stage"] in {"migration_setup", "migration"}
            audit["status"] = "migration_failed" if migration_failed else "setup_failed"
            self.record(revision, audit)
            raise DeploymentError(
                "Migration failed or deployment setup was rejected; no traffic promoted. "
                "Inspect the retained private audit record."
            ) from None
        try:
            audit["stage"] = "promotion"
            for service, state in target.items():
                self.ecs.update_service(cluster=self.config["cluster"], service=service, **state)
            self.settle(target)
            audit["status"] = "healthy"
            audit["stage"] = "complete"
        except Exception:
            try:
                self.restore(previous)
                audit["status"] = "rolled_back"
            except Exception:
                audit["status"] = "rollback_failed"
            self.record(revision, audit)
            raise DeploymentError(
                "Deployment failed. Inspect the private audit record; "
                "no database downgrade was attempted."
            ) from None
        self.record(revision, audit)
        return audit

    def rollback(self, revision: str, previous_revision: str) -> dict[str, Any]:
        validate(self.config, revision)
        if not re.fullmatch(r"[a-f0-9]{40}", previous_revision):
            raise DeploymentError("A full deployment revision is required.")
        audit = {
            **self.audit("rollback", revision),
            "target_deployment_revision": previous_revision,
            "deployment": previous_revision,
            "target": None,
            "before": None,
            "restored": None,
            "observed": None,
            "stage": "load_target",
        }
        try:
            response = self.s3.get_object(
                Bucket=self.config["bucket"],
                Key=f"{self.config['environment']}/backend/{previous_revision}.json",
            )
            value = json.loads(response["Body"].read(65537))
            if (
                value.get("status") != "healthy"
                or value.get("environment") != self.config["environment"]
                or value.get("account_id") != self.config["account_id"]
                or value.get("revision") != previous_revision
            ):
                raise DeploymentError(
                    "Rollback requires a healthy audit record from this environment."
                )
            audit["stage"] = "validate_target"
            previous = value["previous"]
            if not isinstance(previous, dict) or "api" not in previous:
                raise DeploymentError("Invalid rollback target.")
            # The authenticated audit record remains constrained to this account/family.
            for service, state in previous.items():
                if (
                    service not in {"api", "worker"}
                    or not re.fullmatch(
                        rf"arn:aws:ecs:{self.config['region']}:{self.config['account_id']}:task-definition/buddy-{self.config['environment']}-{service}:\d+",
                        state["taskDefinition"],
                    )
                    or type(state["desiredCount"]) is not int
                    or not 0 <= state["desiredCount"] <= 100
                ):
                    raise DeploymentError("Invalid rollback target.")
            audit["target"] = previous
            audit["target_audit_key"] = value.get("audit_key")
            audit["before"] = self.states(list(previous))
            audit["stage"] = "compatibility"
            current = self.ecs.describe_task_definition(
                taskDefinition=audit["before"]["api"]["taskDefinition"]
            )["taskDefinition"]
            self.compatible(current["containerDefinitions"][0]["image"], previous, audit["before"])
            audit["compatibility"] = self.compatibility_report
            if self.states(list(previous)) != audit["before"]:
                raise DeploymentError("Service changed after rollback compatibility testing.")
            audit["stage"] = "restore"
            self.restore(previous)
            audit["restored"] = previous
            audit["status"] = "rolled_back"
            audit["stage"] = "complete"
        except Exception:
            audit["status"] = "rollback_failed"
            if audit["target"] is not None:
                # An unavailable service must not be reported as restored.
                with suppress(Exception):
                    audit["observed"] = self.states(list(audit["target"]))
            self.record(revision, audit)
            raise DeploymentError(
                "Rollback failed. Inspect the retained private audit record; "
                "no database change was attempted."
            ) from None
        self.record(revision, audit)
        return audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--api-image")
    parser.add_argument("--worker-image")
    parser.add_argument("--rollback-revision")
    parser.add_argument("--compatibility-report")
    args = parser.parse_args()
    try:
        import os

        import boto3

        config = json.loads(os.environ["BUDDY_DEPLOYMENT_CONFIG"])
        validate(config, args.revision)
        proof = None
        if args.compatibility_report:
            from pathlib import Path

            proof = json.loads(Path(args.compatibility_report).read_text())
            if (
                proof.get("revision") != args.revision
                or proof.get("account_id") != config["account_id"]
                or proof.get("environment") != config["environment"]
            ):
                raise DeploymentError(
                    "Compatibility evidence belongs to another release/environment."
                )
        deployment = Deployment(
            config,
            boto3.client("ecs", region_name=config["region"]),
            boto3.client("s3", region_name=config["region"]),
            boto3.client("sts", region_name=config["region"]),
            compatibility_report=proof,
        )
        if args.rollback_revision:
            result = deployment.rollback(args.revision, args.rollback_revision)
        elif args.api_image:
            result = deployment.deploy(args.revision, args.api_image, args.worker_image)
        else:
            raise DeploymentError("Specify an image or an audited rollback revision.")
        print(json.dumps(result))
    except Exception:
        print(
            "Deployment did not complete. Check private deployment logs and configuration.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
