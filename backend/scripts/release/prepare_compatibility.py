"""Read staging targets, test exact images locally, and write release-bound evidence."""

import argparse
import json
import os
import re
from pathlib import Path

import boto3
from backend.scripts.release.deploy import Deployment, DeploymentError, image_digest, validate
from backend.scripts.release.image_compatibility import INITIAL_IMAGE, verify_images


def image(ecs, state):
    definition = ecs.describe_task_definition(taskDefinition=state["taskDefinition"])[
        "taskDefinition"
    ]
    containers = definition["containerDefinitions"]
    if len(containers) != 1 or containers[0]["name"] != "api":
        raise DeploymentError("Unsupported rollback container layout.")
    return containers[0]["image"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--candidate-image")
    parser.add_argument("--future-image")
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
    before = driver.states(["api"])
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
        candidate = image(ecs, before["api"])
    if not candidate or set(rollback) != {"api"}:
        raise DeploymentError("This staging transition covers the existing API only.")
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
        if previous_image != INITIAL_IMAGE:
            raise DeploymentError(
                "Bridge preparation must start from the pinned initial staging image."
            )
        image_digest(config, args.future_image, "api")
    report = verify_images(
        previous_image, candidate, future=args.future_image, schema_image=schema_image
    )
    report.update(
        revision=args.revision,
        environment="staging",
        account_id=config["account_id"],
        before=before,
        rollback=rollback,
    )
    if driver.states(["api"]) != before:
        raise DeploymentError("Service changed while compatibility tests were running.")
    args.output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
