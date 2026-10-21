"""Explicit operator-only, disposable ECS/RDS acceptance; never updates an ECS service."""

import argparse
import base64
import hashlib
import json
import re
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy.engine import URL
from worker_acceptance_task import validate

ACCOUNT = "549327541867"
REGION = "eu-central-1"
CLUSTER = "buddy-staging"
BUCKET = "buddy-staging-549327541867-data"
REPOSITORY = "buddy-staging/worker"


def statement(actions, resources, **extra):
    return {"Effect": "Allow", "Action": actions, "Resource": resources, **extra}


def policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def validate_journal(state):
    validate(state)
    name = "buddy-staging-accept-" + state["run"]
    if state["region"] != REGION or state["bucket"] != BUCKET:
        raise ValueError("Acceptance is restricted to the known staging environment")
    for label, role in state.get("roles", {}).items():
        if label not in {"execution", "admin", "worker", "controller"}:
            raise ValueError("Unexpected acceptance role")
        expected = name + "-" + label
        if role != {"name": expected, "arn": f"arn:aws:iam::{ACCOUNT}:role/{expected}"}:
            raise ValueError("Refusing an unrelated IAM role")
    for queue in state.get("queues", []):
        if queue not in {
            f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{name}" + suffix
            for suffix in ("", "-dead")
        }:
            raise ValueError("Refusing an unrelated queue")
    if "log_group" in state and state["log_group"] != "/buddy/staging/acceptance/" + state["run"]:
        raise ValueError("Refusing an unrelated log group")
    if "runtime_secret" in state and not re.fullmatch(
        rf"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-[A-Za-z0-9]{{6}}",
        state["runtime_secret"],
    ):
        raise ValueError("Refusing an unrelated secret")
    for definition in state.get("definitions", []):
        if not re.fullmatch(
            rf"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{name}-[a-z-]+:\d+",
            definition,
        ):
            raise ValueError("Refusing an unrelated task definition")
    if "image_tag" in state and not re.fullmatch(
        r"acceptance-[a-f0-9]{8}-" + state["run"], state["image_tag"]
    ):
        raise ValueError("Refusing an unrelated image tag")


class Acceptance:
    def __init__(self, path, state):
        validate_journal(state)
        self.path, self.state = Path(path), state
        self.clients = {}
        if self.client("sts").get_caller_identity()["Account"] != ACCOUNT:
            raise ValueError("Unexpected AWS account")
        self.deadline = time.monotonic() + 1200

    def client(self, service):
        if service not in self.clients:
            self.clients[service] = boto3.client(
                service,
                region_name=REGION,
                config=Config(
                    connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 3}
                ),
            )
        return self.clients[service]

    def save(self):
        # This journal contains identifiers/results, never credentials or secret values.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(self.path.suffix + ".tmp")
        pending.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        pending.replace(self.path)

    def note(self, name, **values):
        print(json.dumps({"event": name, **values}), flush=True)

    @property
    def tags(self):
        return [
            {"key": "Application", "value": "Buddy"},
            {"key": "Environment", "value": "staging"},
            {"key": "AcceptanceRun", "value": self.state["run"]},
        ]

    def setup(self, source_commit, image):
        s = self.state
        api = self.client("ecs").describe_services(cluster=CLUSTER, services=["api"])["services"][0]
        s["api_before"] = {key: api[key] for key in ("taskDefinition", "desiredCount")}
        network = api["networkConfiguration"]["awsvpcConfiguration"]
        groups = self.client("ec2").describe_security_groups(
            Filters=[{"Name": "group-name", "Values": ["buddy-staging-worker"]}]
        )["SecurityGroups"]
        if len(groups) != 1:
            raise RuntimeError("Expected one existing worker network group")
        s["network"] = {**network, "securityGroups": [groups[0]["GroupId"]]}
        rds = self.client("rds").describe_db_instances(DBInstanceIdentifier="buddy-staging")[
            "DBInstances"
        ][0]
        if rds["DBInstanceStatus"] != "available":
            raise RuntimeError("Staging RDS is unavailable")
        s["host"] = rds["Endpoint"]["Address"]
        s["admin_secret"] = rds["MasterUserSecret"]["SecretArn"]
        rule = self.client("s3").get_bucket_encryption(Bucket=BUCKET)[
            "ServerSideEncryptionConfiguration"
        ]["Rules"][0]
        s["kms"] = rule["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"]
        self.save()
        name = "buddy-staging-accept-" + s["run"]
        password = secrets.token_urlsafe(48)
        url = URL.create(
            "postgresql",
            username=s["db_role"],
            password=password,
            host=s["host"],
            database=s["database"],
            query={"sslmode": "verify-full"},
        )
        s["runtime_secret"] = self.client("secretsmanager").create_secret(
            Name=name,
            KmsKeyId=s["kms"],
            SecretString=json.dumps(
                {
                    "username": s["db_role"],
                    "password": password,
                    "url": url.render_as_string(hide_password=False),
                }
            ),
            Tags=[{"Key": t["key"], "Value": t["value"]} for t in self.tags],
        )["ARN"]
        self.save()
        s["log_group"] = "/buddy/staging/acceptance/" + s["run"]
        self.client("logs").create_log_group(logGroupName=s["log_group"])
        self.client("logs").put_retention_policy(logGroupName=s["log_group"], retentionInDays=1)
        self.save()
        s["queues"] = []
        for suffix in ("-dead", ""):
            attributes = {
                "KmsMasterKeyId": s["kms"],
                "MessageRetentionPeriod": "3600",
                "VisibilityTimeout": "60",
            }
            if not suffix:
                attributes["RedrivePolicy"] = json.dumps(
                    {"deadLetterTargetArn": s["dead_arn"], "maxReceiveCount": "3"}
                )
            queue = self.client("sqs").create_queue(QueueName=name + suffix, Attributes=attributes)[
                "QueueUrl"
            ]
            s["queues"].append(queue)
            self.save()
            arn = self.client("sqs").get_queue_attributes(
                QueueUrl=queue, AttributeNames=["QueueArn"]
            )["Attributes"]["QueueArn"]
            self.client("sqs").set_queue_attributes(
                QueueUrl=queue,
                Attributes={
                    "Policy": json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Deny",
                                    "Principal": "*",
                                    "Action": "sqs:*",
                                    "Resource": arn,
                                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                                }
                            ],
                        }
                    )
                },
            )
            s["dead_arn" if suffix else "queue_arn"] = arn
        s["queue"] = queue
        s["sources"] = {}
        fixture = Path("tests/fixtures/slides/Buddy_PDF_Geometry.pdf").read_bytes()
        for label in ("good", "bad", "restart", "cancel"):
            content = b"Synthetic malformed PDF" if label == "bad" else fixture
            key = s["prefix"] + label + ".pdf"
            digest, revision = hashlib.sha256(content).hexdigest(), str(uuid4())
            result = self.client("s3").put_object(
                Bucket=BUCKET,
                Key=key,
                Body=content,
                ContentType="application/pdf",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=s["kms"],
                ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                Metadata={"source-revision": revision, "sha256": digest},
                IfNoneMatch="*",
            )
            s["sources"][label] = {
                "key": key,
                "revision": revision,
                "version": result["VersionId"],
                "size": len(content),
                "sha256": digest,
            }
            self.save()
        self.publish_image(source_commit, image)
        self.roles()

    def publish_image(self, commit, image):
        s = self.state
        s["source_commit"] = commit
        s["image_tag"] = "acceptance-" + commit[:8] + "-" + s["run"]
        self.save()
        auth = self.client("ecr").get_authorization_token()["authorizationData"][0]
        password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)[1]
        registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
        target = registry + "/" + REPOSITORY + ":" + s["image_tag"]
        # Docker credentials exist only in a temporary config, removed even on failure.
        with tempfile.TemporaryDirectory(prefix="buddy-acceptance-docker-") as folder:
            prefix = ["docker", "--config", folder]
            subprocess.run(
                prefix + ["login", "--username", "AWS", "--password-stdin", registry],
                input=password,
                text=True,
                check=True,
                timeout=60,
            )
            subprocess.run(prefix + ["tag", image, target], check=True, timeout=60)
            subprocess.run(prefix + ["push", target], check=True, timeout=420)
        digest = self.client("ecr").describe_images(
            repositoryName=REPOSITORY, imageIds=[{"imageTag": s["image_tag"]}]
        )["imageDetails"][0]["imageDigest"]
        s["image"] = registry + "/" + REPOSITORY + "@" + digest
        self.save()
        self.note("exact_candidate_published", commit=commit, digest=digest)

    def role(self, label, statements):
        s = self.state
        name = "buddy-staging-accept-" + s["run"] + "-" + label
        trust = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                        "Condition": {
                            "StringEquals": {"aws:SourceAccount": ACCOUNT},
                            "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:*"},
                        },
                    }
                ],
            }
        )
        arn = self.client("iam").create_role(
            RoleName=name,
            AssumeRolePolicyDocument=trust,
            Tags=[{"Key": t["key"], "Value": t["value"]} for t in self.tags],
        )["Role"]["Arn"]
        s.setdefault("roles", {})[label] = {"name": name, "arn": arn}
        self.save()
        self.client("iam").put_role_policy(
            RoleName=name, PolicyName="acceptance", PolicyDocument=policy(statements)
        )

    def roles(self):
        s = self.state

        def kms(actions, services):
            return statement(
                actions, s["kms"], Condition={"StringEquals": {"kms:ViaService": services}}
            )

        self.role(
            "execution",
            [
                statement(["ecr:GetAuthorizationToken"], "*"),
                statement(
                    [
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchGetImage",
                    ],
                    f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{REPOSITORY}",
                ),
                statement(
                    ["logs:CreateLogStream", "logs:PutLogEvents"],
                    f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{s['log_group']}:*",
                ),
                statement(["secretsmanager:GetSecretValue"], s["runtime_secret"]),
                kms(["kms:Decrypt"], f"secretsmanager.{REGION}.amazonaws.com"),
            ],
        )
        self.role(
            "admin",
            [
                statement(
                    ["secretsmanager:GetSecretValue"], [s["admin_secret"], s["runtime_secret"]]
                ),
                kms(["kms:Decrypt"], f"secretsmanager.{REGION}.amazonaws.com"),
            ],
        )
        self.role(
            "worker",
            [
                statement(["s3:ListBucket"], f"arn:aws:s3:::{BUCKET}"),
                statement(
                    ["s3:GetObject", "s3:GetObjectVersion"], f"arn:aws:s3:::{BUCKET}/{s['prefix']}*"
                ),
                statement(
                    [
                        "sqs:ReceiveMessage",
                        "sqs:DeleteMessage",
                        "sqs:ChangeMessageVisibility",
                        "sqs:GetQueueAttributes",
                    ],
                    s["queue_arn"],
                ),
                statement(
                    ["bedrock:InvokeModel"],
                    [
                        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/eu.anthropic.claude-sonnet-4-6",
                        f"arn:aws:bedrock:{REGION}::foundation-model/anthropic.claude-sonnet-4-6",
                        "arn:aws:bedrock:eu-north-1::foundation-model/anthropic.claude-sonnet-4-6",
                        "arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-sonnet-4-6",
                        "arn:aws:bedrock:eu-south-1::foundation-model/anthropic.claude-sonnet-4-6",
                        "arn:aws:bedrock:eu-south-2::foundation-model/anthropic.claude-sonnet-4-6",
                        "arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-sonnet-4-6",
                        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:project/default",
                    ],
                ),
                kms(["kms:Decrypt"], [f"s3.{REGION}.amazonaws.com", f"sqs.{REGION}.amazonaws.com"]),
                statement(
                    ["s3:ListBucketVersions"],
                    f"arn:aws:s3:::{BUCKET}",
                    Condition={"StringLike": {"s3:prefix": "sources/*/*/validation/*"}},
                ),
                statement(
                    ["s3:PutObject", "s3:DeleteObjectVersion"],
                    f"arn:aws:s3:::{BUCKET}/sources/*/*/validation/*",
                ),
                kms(["kms:GenerateDataKey"], f"s3.{REGION}.amazonaws.com"),
            ],
        )
        self.controller_policy = [
            statement(["sqs:SendMessage"], s["queue_arn"]),
            kms(["kms:Decrypt", "kms:GenerateDataKey"], f"sqs.{REGION}.amazonaws.com"),
        ]
        self.role("controller", self.controller_policy)
        time.sleep(15)

    def start(self, mode, *, stop_timeout=120):
        s = self.state
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Acceptance global deadline exceeded")
        worker = mode.startswith("worker")
        role = (
            "worker"
            if worker or mode == "permissions"
            else "controller"
            if mode == "exercise"
            else "admin"
        )
        env = {
            "BUDDY_ENVIRONMENT": "staging",
            "BUDDY_AWS_REGION": REGION,
            "BUDDY_UPLOADS_ENABLED": "true",
            "BUDDY_DATA_BUCKET": BUCKET,
            "BUDDY_DATA_KMS_KEY_ARN": s["kms"],
            "BUDDY_PROCESSING_QUEUE_URL": s["queue"],
            "BUDDY_ACCEPTANCE_CONFIG": json.dumps(
                {
                    k: s[k]
                    for k in (
                        "run",
                        "database",
                        "db_role",
                        "cluster",
                        "region",
                        "bucket",
                        "prefix",
                        "host",
                        "admin_secret",
                        "runtime_secret",
                        "queue",
                        "sources",
                        "jobs",
                        "owner",
                        "fault_task",
                    )
                    if k in s
                }
            ),
        }
        container = {
            "name": "worker",
            "image": s["image"],
            "essential": True,
            "readonlyRootFilesystem": True,
            "user": "10001:10001",
            "stopTimeout": stop_timeout,
            "linuxParameters": {"initProcessEnabled": True, "capabilities": {"drop": ["ALL"]}},
            "mountPoints": [
                {
                    "sourceVolume": "scratch",
                    "containerPath": "/var/lib/buddy-upload",
                    "readOnly": False,
                }
            ],
            "environment": [{"name": k, "value": v} for k, v in env.items()],
            "secrets": [
                {"name": "BUDDY_DATABASE_URL", "valueFrom": s["runtime_secret"] + ":url::"}
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": s["log_group"],
                    "awslogs-region": REGION,
                    "awslogs-stream-prefix": mode,
                },
            },
        }
        if worker:
            container["entryPoint"] = [
                "timeout",
                "--signal=TERM",
                "--kill-after=125",
                "600",
                "python",
                "-m",
                "buddy_api.processing",
            ]
            container["healthCheck"] = {
                "command": ["CMD-SHELL", "python -m buddy_api.processing --health"],
                "interval": 15,
                "timeout": 5,
                "retries": 3,
                "startPeriod": 40,
            }
        else:
            container["entryPoint"] = [
                "timeout",
                "--signal=TERM",
                "--kill-after=5",
                "600" if mode == "exercise" else "240",
                "python",
                "-c",
            ]
            container["command"] = [
                Path(__file__).with_name("worker_acceptance_task.py").read_text(encoding="utf-8"),
                mode,
            ]
        definition = self.client("ecs").register_task_definition(
            family="buddy-staging-accept-" + s["run"] + "-" + mode,
            networkMode="awsvpc",
            requiresCompatibilities=["FARGATE"],
            cpu="1024" if worker else "256" if mode == "exercise" else "512",
            memory="2048" if worker else "512" if mode == "exercise" else "1024",
            executionRoleArn=s["roles"]["execution"]["arn"],
            taskRoleArn=s["roles"][role]["arn"],
            containerDefinitions=[container],
            volumes=[{"name": "scratch"}],
            tags=self.tags,
        )["taskDefinition"]["taskDefinitionArn"]
        s.setdefault("definitions", []).append(definition)
        self.save()
        result = self.client("ecs").run_task(
            cluster=CLUSTER,
            taskDefinition=definition,
            launchType="FARGATE",
            count=1,
            platformVersion="1.4.0",
            startedBy="accept-" + s["run"],
            networkConfiguration={"awsvpcConfiguration": s["network"]},
            tags=self.tags,
        )
        if result.get("failures") or len(result.get("tasks", [])) != 1:
            raise RuntimeError("ECS rejected isolated acceptance task")
        task = result["tasks"][0]["taskArn"]
        s.setdefault("tasks", {})[task] = mode
        self.save()
        self.note("task_started", mode=mode, task=task.rsplit("/", 1)[-1])
        return task

    def describe(self, task):
        info = self.client("ecs").describe_tasks(cluster=CLUSTER, tasks=[task], include=["TAGS"])[
            "tasks"
        ][0]
        if {"key": "AcceptanceRun", "value": self.state["run"]} not in info.get("tags", []):
            raise ValueError("Refusing an unowned ECS task")
        return info

    def events(self, task):
        mode = self.state["tasks"][task]
        stream = mode + "/worker/" + task.rsplit("/", 1)[-1]
        try:
            result = self.client("logs").get_log_events(
                logGroupName=self.state["log_group"], logStreamName=stream, startFromHead=True
            )
        except self.client("logs").exceptions.ResourceNotFoundException:
            return []
        events = []
        for item in result["events"]:
            try:
                value = json.loads(item["message"])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("event") in {
                "started",
                "acceptance_result",
                "acceptance_failed",
                "unexpected_job_state",
                "initial_processing_passed",
                "fault_injected_waiting_for_replacement",
                "replacement_recovery_passed",
                "isolated_database_created",
            }:
                events.append(value)
        return events

    def wait(self, task, *, running=False, seconds=300):
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            info = self.describe(task)
            if running and info["lastStatus"] == "RUNNING":
                return info
            if info["lastStatus"] == "STOPPED":
                containers = info.get("containers", [])
                self.state.setdefault("task_results", {})[task] = {
                    "mode": self.state["tasks"][task],
                    "stopCode": info.get("stopCode"),
                    "reason": info.get("stoppedReason"),
                    "exit_codes": [c.get("exitCode") for c in containers],
                }
                self.save()
                if running or not containers or any(c.get("exitCode") != 0 for c in containers):
                    self.note(
                        "task_failed",
                        details=self.state["task_results"][task],
                        events=self.events(task),
                    )
                    raise RuntimeError("Acceptance ECS task failed")
                for _ in range(6):
                    events = self.events(task)
                    results = [e["result"] for e in events if e["event"] == "acceptance_result"]
                    if results:
                        return results[-1]
                    time.sleep(2)
                raise RuntimeError("Task produced no acceptance result")
            time.sleep(3)
        raise TimeoutError("Acceptance task deadline exceeded")

    def exercise(self):
        s = self.state
        s.update(self.wait(self.start("prepare")))
        s["database_prepared"] = True
        self.save()
        s.setdefault("results", {})["permissions"] = self.wait(self.start("permissions"))
        self.save()
        self.note("actual_task_role_permissions_passed", result=s["results"]["permissions"])
        fault = self.start("worker-fault", stop_timeout=2)
        s["fault_task"] = fault
        self.save()
        self.wait(fault, running=True)
        self.client("iam").put_role_policy(
            RoleName=s["roles"]["controller"]["name"],
            PolicyName="fault-drill",
            PolicyDocument=policy([statement(["ecs:StopTask", "ecs:DescribeTasks"], fault)]),
        )
        time.sleep(10)
        controller = self.start("exercise")
        replacement = None
        end = min(self.deadline, time.monotonic() + 580)
        seen = set()
        while time.monotonic() < end:
            for event in self.events(controller):
                if event["event"] not in seen:
                    seen.add(event["event"])
                    self.note("controller_progress", **{"detail": event})
            fault_info = self.describe(fault)
            if fault_info["lastStatus"] == "STOPPED" and not replacement:
                s["fault_evidence"] = {
                    "stopCode": fault_info.get("stopCode"),
                    "reason": fault_info.get("stoppedReason"),
                    "exit_codes": [c.get("exitCode") for c in fault_info.get("containers", [])],
                }
                self.save()
                if s["fault_evidence"]["exit_codes"] != [137]:
                    raise RuntimeError("Fault drill did not produce the required hard interruption")
                replacement = self.start("worker-replacement")
                self.note("replacement_launched_after_actual_kill", evidence=s["fault_evidence"])
            if self.describe(controller)["lastStatus"] == "STOPPED":
                s["results"]["processing"] = self.wait(controller)
                if not replacement:
                    raise RuntimeError("Replacement recovery was not exercised")
                for _ in range(30):
                    health = self.describe(replacement)
                    if health.get("healthStatus") == "HEALTHY":
                        s["results"]["replacement_health"] = "HEALTHY"
                        self.save()
                        return
                    if health["lastStatus"] == "STOPPED":
                        break
                    time.sleep(3)
                raise RuntimeError("Replacement task health did not pass")
            time.sleep(3)
        raise TimeoutError("End-to-end task test timed out")

    def cleanup(self):
        s = self.state
        self.deadline = time.monotonic() + 600
        errors = []

        def attempt(label, call):
            try:
                call()
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {
                    "ResourceNotFoundException",
                    "NoSuchEntity",
                    "AWS.SimpleQueueService.NonExistentQueue",
                    "QueueDoesNotExist",
                    "ImageNotFoundException",
                }:
                    errors.append({"resource": label, "error_type": type(exc).__name__})
            except Exception as exc:
                errors.append({"resource": label, "error_type": type(exc).__name__})

        for task in list(s.get("tasks", {})):
            info = self.describe(task)
            if info["lastStatus"] != "STOPPED":
                attempt(
                    "stop task",
                    lambda task=task: self.client("ecs").stop_task(
                        cluster=CLUSTER, task=task, reason="Isolated acceptance cleanup"
                    ),
                )
        for task in list(s.get("tasks", {})):
            attempt(
                "wait task stopped",
                lambda task=task: (
                    self.client("ecs")
                    .get_waiter("tasks_stopped")
                    .wait(
                        cluster=CLUSTER, tasks=[task], WaiterConfig={"Delay": 3, "MaxAttempts": 55}
                    )
                ),
            )
        if errors:
            s["cleanup_errors"] = errors
            self.save()
            raise RuntimeError("Tasks must stop before dependent resources are removed")
        s["task_runtime"] = []
        for task, mode in list(s.get("tasks", {}).items()):
            info = self.describe(task)
            s["task_runtime"].append(
                {
                    "mode": mode,
                    "cpu": info.get("cpu"),
                    "memory": info.get("memory"),
                    "pull_started": str(info.get("pullStartedAt")),
                    "started": str(info.get("startedAt")),
                    "stopped": str(info.get("stoppedAt")),
                    "exit_codes": [c.get("exitCode") for c in info.get("containers", [])],
                }
            )
        self.save()
        # Keep admin access/secret intact if database cleanup fails, permitting an explicit retry.
        if "admin" in s.get("roles", {}) and "image" in s and not s.get("database_cleanup"):
            try:
                s["database_cleanup"] = self.wait(self.start("cleanup"))
                self.save()
            except Exception:
                s["cleanup_errors"] = errors + [
                    {"resource": "isolated database", "error_type": "CleanupNeedsRetry"}
                ]
                self.save()
                raise RuntimeError(
                    "Database cleanup failed; identifiers retained for retry"
                ) from None
        for definition in s.get("definitions", []):
            attempt(
                "task definition",
                lambda definition=definition: self.client("ecs").deregister_task_definition(
                    taskDefinition=definition
                ),
            )
            attempt(
                "delete task definition",
                lambda definition=definition: self.client("ecs").delete_task_definitions(
                    taskDefinitions=[definition]
                ),
            )
        if "prefix" in s:
            try:
                versions = self.client("s3").list_object_versions(Bucket=BUCKET, Prefix=s["prefix"])
                if versions.get("IsTruncated"):
                    raise RuntimeError("Unexpected large acceptance prefix")
                for kind in ("Versions", "DeleteMarkers"):
                    for obj in versions.get(kind, []):
                        if not obj["Key"].startswith(s["prefix"]):
                            raise ValueError("Refusing to delete an unrelated object")
                        self.client("s3").delete_object(
                            Bucket=BUCKET, Key=obj["Key"], VersionId=obj["VersionId"]
                        )
                remaining = self.client("s3").list_object_versions(
                    Bucket=BUCKET, Prefix=s["prefix"]
                )
                if remaining.get("Versions") or remaining.get("DeleteMarkers"):
                    raise RuntimeError("Acceptance objects remain")
            except Exception as exc:
                errors.append({"resource": "test objects", "error_type": type(exc).__name__})
        for queue in s.get("queues", []):
            attempt("queue", lambda queue=queue: self.client("sqs").delete_queue(QueueUrl=queue))
        for role in s.get("roles", {}).values():
            for name in ("acceptance", "fault-drill"):
                attempt(
                    "inline policy",
                    lambda role=role, name=name: self.client("iam").delete_role_policy(
                        RoleName=role["name"], PolicyName=name
                    ),
                )
            attempt("role", lambda role=role: self.client("iam").delete_role(RoleName=role["name"]))
        if "runtime_secret" in s:
            attempt(
                "secret",
                lambda: self.client("secretsmanager").delete_secret(
                    SecretId=s["runtime_secret"], ForceDeleteWithoutRecovery=True
                ),
            )
        if "log_group" in s:
            attempt(
                "log group",
                lambda: self.client("logs").delete_log_group(logGroupName=s["log_group"]),
            )
        if "image_tag" in s:
            attempt(
                "test image",
                lambda: self.client("ecr").batch_delete_image(
                    repositoryName=REPOSITORY, imageIds=[{"imageTag": s["image_tag"]}]
                ),
            )
        api = self.client("ecs").describe_services(cluster=CLUSTER, services=["api"])["services"][0]
        s["api_after"] = {key: api[key] for key in ("taskDefinition", "desiredCount")}
        s["api_unchanged"] = s.get("api_before") == s["api_after"]
        s["cleanup_errors"] = errors
        s["cleanup_complete"] = not errors
        self.save()
        self.note(
            "cleanup_finished", complete=not errors, api_unchanged=s["api_unchanged"], errors=errors
        )
        if errors:
            raise RuntimeError("Some isolated resources need cleanup retry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--commit")
    args = parser.parse_args()
    if args.cleanup:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    else:
        if not args.image or not args.commit or not re.fullmatch(r"[a-f0-9]{40}", args.commit):
            raise ValueError("Supply the exact candidate --commit and its built local --image")
        if args.state.exists():
            raise ValueError("Use a fresh state path or --cleanup")
        run = uuid4().hex[:12]
        state = {
            "run": run,
            "region": REGION,
            "bucket": BUCKET,
            "cluster": CLUSTER,
            "database": "buddy_worker_acceptance_" + run + "_test",
            "db_role": "buddy_w_" + run,
            "prefix": "sources/acceptance/" + run + "/",
            "started_at": datetime.now(UTC).isoformat(),
        }
    acceptance = Acceptance(args.state, state)
    if args.cleanup:
        acceptance.cleanup()
        return
    acceptance.save()
    try:
        acceptance.setup(args.commit, args.image)
        acceptance.exercise()
        state["passed"] = True
        acceptance.save()
        acceptance.note("all_live_acceptance_checks_passed", results=state["results"])
    except Exception as exc:
        state["failure_type"] = type(exc).__name__
        acceptance.save()
        acceptance.note("acceptance_failed", error_type=type(exc).__name__)
        raise
    finally:
        acceptance.cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps({"event": "operator_failed", "error_type": type(exc).__name__}), flush=True
        )
        raise SystemExit(1) from None
