"""Read staging targets, test exact images locally, and write release-bound evidence."""

import argparse
import json
import os
import re
from pathlib import Path

import boto3
from backend.scripts.release.deploy import Deployment, DeploymentError, image_digest, validate
from backend.scripts.release.image_compatibility import verify_images


def image(ecs, state, service="api"):
    definition = ecs.describe_task_definition(taskDefinition=state["taskDefinition"])[
        "taskDefinition"
    ]
    containers = definition["containerDefinitions"]
    if len(containers) != 1 or containers[0]["name"] != service:
        raise DeploymentError("Unsupported rollback container layout.")
    return containers[0]["image"]


def current_states(driver):
    """Read API and an optional Terraform-provisioned, possibly paused worker."""
    state = driver.states(["api"])
    response = driver.ecs.describe_services(cluster=driver.config["cluster"], services=["worker"])
    if response.get("services"):
        if response.get("failures") or len(response["services"]) != 1:
            raise DeploymentError("The staging worker service is ambiguous or unavailable.")
        worker = response["services"][0]
        state["worker"] = {
            "taskDefinition": worker["taskDefinition"],
            "desiredCount": worker["desiredCount"],
        }
    elif response.get("failures") and all(
        failure.get("reason") in {"MISSING", "MISSING_RESOURCE"} for failure in response["failures"]
    ):
        return state
    elif response.get("failures"):
        raise DeploymentError("Could not safely determine whether the staging worker exists.")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--candidate-image")
    parser.add_argument("--worker-image")
    parser.add_argument("--reader-only", action="store_true")
    parser.add_argument("--future-image")
    parser.add_argument("--base-image")
    parser.add_argument("--rollback-revision")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(os.environ["BUDDY_DEPLOYMENT_CONFIG"])
    validate(config, args.revision)
    if config["environment"] != "staging":
        raise DeploymentError("This compatibility transition is staging-only.")
    ecs = boto3.client("ecs", region_name=config["region"])
    s3 = boto3.client("s3", region_name=config["region"])
    driver = Deployment(config, ecs, s3, boto3.client("sts", region_name=config["region"]))
    before = current_states(driver)
    if args.reader_only and (
        args.worker_image
        or args.future_image
        or args.rollback_revision
        or ("worker" in before and before["worker"]["desiredCount"] != 0)
    ):
        raise DeploymentError("Reader preparation requires a paused worker and API-only promotion.")
    if not args.rollback_revision:
        driver.require_stable_source(before)
    rollback = before
    candidate = args.candidate_image
    schema_image = None
    if args.rollback_revision:
        if not re.fullmatch(r"[a-f0-9]{40}", args.rollback_revision):
            raise DeploymentError("A full deployment revision is required.")
        record = s3.get_object(
            Bucket=config["bucket"], Key=f"staging/backend/{args.rollback_revision}.json"
        )
        audit = json.loads(record["Body"].read(65537))
        if (
            audit.get("status") != "healthy"
            or audit.get("account_id") != config["account_id"]
            or audit.get("revision") != args.rollback_revision
            or audit.get("environment") != "staging"
        ):
            raise DeploymentError("A healthy staging rollback audit is required.")
        # Even if an automatic rollback has already restored the bridge, the
        # database may remain expanded. Never infer its schema from that old app.
        evidence = audit.get("compatibility", {})
        schema_image = evidence.get("future_image") or evidence.get("schema_image")
        if not schema_image:
            raise DeploymentError("Rollback needs a retained feature-schema image.")
        image_digest(config, schema_image, "api")
        rollback = audit["previous"]
        # A worker added after an older API-only deployment must be paused if
        # that API revision is restored; it must not keep processing with a
        # schema-incompatible image during rollback.
        if "worker" in before and "worker" not in rollback:
            rollback["worker"] = {
                "taskDefinition": before["worker"]["taskDefinition"],
                "desiredCount": 0,
            }
        candidate = image(ecs, before["api"])
    if not candidate or "api" not in rollback or not set(rollback) <= {"api", "worker"}:
        raise DeploymentError("Staging release has an invalid API/worker service target.")
    worker_image = args.worker_image
    previous_worker_image = None
    if "worker" in rollback:
        rollback_worker = rollback["worker"]
        if not isinstance(rollback_worker, dict) or not re.fullmatch(
            rf"arn:aws:ecs:{config['region']}:{config['account_id']}:task-definition/buddy-staging-worker:\d+",
            rollback_worker.get("taskDefinition", ""),
        ):
            raise DeploymentError("Invalid staging worker target.")
        worker_image = (
            image(ecs, rollback_worker, "worker")
            if args.rollback_revision
            else (worker_image or image(ecs, rollback_worker, "worker"))
        )
        worker_image = image_digest(config, worker_image, "worker")
    if "worker" in before:
        previous_worker_image = image_digest(
            config, image(ecs, before["worker"], "worker"), "worker"
        )
    if "worker" in before and not worker_image:
        raise DeploymentError("The live staging worker requires a paired worker image release.")
    if worker_image:
        worker_image = image_digest(config, worker_image, "worker")
    state = rollback["api"]
    expected = (
        f"arn:aws:ecs:{config['region']}:{config['account_id']}:task-definition/buddy-staging-api:"
    )
    if (
        not isinstance(state, dict)
        or not re.fullmatch(re.escape(expected) + r"\d+", state.get("taskDefinition", ""))
        or type(state.get("desiredCount")) is not int
        or not 0 <= state["desiredCount"] <= 100
    ):
        raise DeploymentError("Invalid staging rollback target.")
    previous_image = image_digest(config, image(ecs, rollback["api"]), "api")
    candidate = image_digest(config, candidate, "api")
    if args.future_image:
        if not args.base_image:
            raise DeploymentError(
                "Bridge preparation must name the immutable currently deployed base image."
            )
        base_image = image_digest(config, args.base_image, "api")
        if previous_image != base_image:
            raise DeploymentError("Bridge base image drifted from the stable staging service.")
        image_digest(config, args.future_image, "api")
    compatibility_args = {
        "future": args.future_image,
        "baseline_image": previous_image if args.future_image else None,
        "schema_image": schema_image,
    }
    if args.reader_only:
        compatibility_args["reader_only"] = True
    if worker_image:
        compatibility_args.update(
            worker_image=worker_image,
            previous_worker_image=previous_worker_image,
        )
    report = verify_images(previous_image, candidate, **compatibility_args)
    report.update(
        revision=args.revision,
        environment="staging",
        account_id=config["account_id"],
        before=before,
        rollback=rollback,
    )
    if current_states(driver) != before:
        raise DeploymentError("Service changed while compatibility tests were running.")
    args.output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
