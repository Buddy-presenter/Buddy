"""Exercise actual application images on a disposable, unexposed PostgreSQL database."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

INITIAL_IMAGE = (
    "549327541867.dkr.ecr.eu-central-1.amazonaws.com/buddy-staging/api@sha256:"
    "f61c966f16ebd4ea59331ae3f54b7034c98b1e91d6716a078ae9f889041a9593"
)
POSTGRES_IMAGE = "postgres:17"
CHECKS = Path(__file__).resolve().parent
MIGRATION_URL = (
    "postgresql://buddy_migration:synthetic-migration-only@127.0.0.1:5432/"
    "buddy_rollback_test?sslmode=disable"
)
RUNTIME_URL = (
    "postgresql://buddy_api:synthetic-runtime-only@127.0.0.1:5432/"
    "buddy_rollback_test?sslmode=disable"
)


def docker(*args: str, timeout: int = 180) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        # Images contain synthetic data only; omit arbitrary dependency/driver text
        # from the release boundary while retaining the failing operation.
        raise RuntimeError(
            "Compatibility container operation failed: " + args[0] + "\n" + result.stderr[-4000:]
        )
    return result.stdout.strip()


def verify_worker_image(worker_image: str, schema_image: str) -> None:
    """Run an exact worker image against a disposable database at the release schema."""
    smoke = CHECKS.parents[2] / "infra/container/smoke_worker.py"
    result = subprocess.run(
        [
            sys.executable,
            str(smoke),
            "--image",
            worker_image,
            "--schema-image",
            schema_image,
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Worker image compatibility check failed: "
            + (result.stdout + "\n" + result.stderr)[-4000:]
        )


def verify_images(
    previous: str,
    candidate: str,
    *,
    initial: str = INITIAL_IMAGE,
    future: str | None = None,
    baseline_image: str | None = None,
    schema_image: str | None = None,
    worker_image: str | None = None,
    previous_worker_image: str | None = None,
    pull_images: bool = True,
) -> dict:
    prefix = "buddy-compat-" + uuid4().hex[:12]
    pg, api = prefix + "-pg", prefix + "-api"
    network = prefix + "-network"
    created = []

    def psql(statement: str) -> None:
        docker(
            "exec",
            pg,
            "psql",
            "-U",
            "buddy_migration",
            "-d",
            "buddy_rollback_test",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        )

    def args(image: str, url: str) -> list[str]:
        return [
            "--network",
            "container:" + pg,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "-e",
            "BUDDY_ENVIRONMENT=test",
            "-e",
            "BUDDY_BIND_HOST=127.0.0.1",
            "-e",
            "BUDDY_ALLOWED_HOSTS=localhost,127.0.0.1",
            "-e",
            "BUDDY_DATABASE_URL=" + url,
            "-v",
            str(CHECKS) + ":/checks:ro",
            "-v",
            str(CHECKS.parents[2] / "backend/tests/support")
            + ":/test-support/backend/tests/support:ro",
            "-v",
            str(CHECKS.parents[2] / "docs/fixtures") + ":/test-support/docs/fixtures:ro",
            image,
        ]

    def migrate(image: str) -> None:
        docker(
            "run",
            "--rm",
            "--entrypoint",
            "python",
            *args(image, MIGRATION_URL),
            "-m",
            "buddy_api.persistence.migrate",
            "upgrade",
        )
        psql("REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM buddy_api")

    def probe(image: str, mode: str) -> None:
        docker("run", "-d", "--name", api, *args(image, RUNTIME_URL))
        created.append(api)
        try:
            # Exercise the image's real entrypoint and HTTP readiness, not a patched
            # test server. Retries are startup polling only; assertions run once.
            poll = """import time
import urllib.request
for attempt in range(30):
    try:
        response = urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=1)
        assert response.status == 200
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(.2)
"""
            docker("exec", api, "python", "-c", poll, timeout=40)
            result = json.loads(docker("exec", api, "python", "/checks/rollback_probe.py", mode))
            assert all(
                result[key] is True for key in ("ready", "analysis", "session", "owner_isolation")
            )
        finally:
            docker("rm", "-fv", api)
            created.remove(api)

    try:
        if pull_images:
            for image in dict.fromkeys(
                [
                    initial,
                    previous,
                    candidate,
                    *([baseline_image] if baseline_image else []),
                    *([future] if future else []),
                    *([schema_image] if schema_image else []),
                    *([worker_image] if worker_image else []),
                    *([previous_worker_image] if previous_worker_image else []),
                ]
            ):
                docker("pull", image, timeout=600)
        docker("network", "create", "--internal", network)
        docker(
            "run",
            "-d",
            "--name",
            pg,
            "--network",
            network,
            "-e",
            "POSTGRES_USER=buddy_migration",
            "-e",
            "POSTGRES_PASSWORD=synthetic-migration-only",
            "-e",
            "POSTGRES_DB=buddy_rollback_test",
            POSTGRES_IMAGE,
        )
        created.append(pg)
        docker(
            "exec",
            pg,
            "sh",
            "-c",
            "until pg_isready -h 127.0.0.1 -U buddy_migration -d buddy_rollback_test; "
            "do sleep 1; done",
            timeout=30,
        )
        psql(
            "CREATE ROLE buddy_api LOGIN PASSWORD 'synthetic-runtime-only'; "
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC; GRANT USAGE ON SCHEMA "
            "public TO buddy_api; ALTER DEFAULT PRIVILEGES FOR ROLE "
            "buddy_migration IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE "
            "ON TABLES TO buddy_api"
        )
        migrate(initial)
        probe(initial, "seed")
        if future:
            # A newly built bridge starts from the exact currently deployed image,
            # not the repository's historical first deployment. Seed that image's
            # own schema before proving the bridge on both sides of the expansion.
            baseline = baseline_image or initial
            migrate(baseline)
            probe(baseline, "baseline")
            probe(candidate, "baseline")
            migrate(future)
            probe(future, "typed")
            probe(candidate, "write")
            probe(future, "read")
        else:
            migrate(schema_image or candidate)
            probe(schema_image or candidate, "typed")
            probe(previous, "write")
            probe(candidate, "read")
        worker_schema = schema_image or future or candidate
        if worker_image:
            verify_worker_image(worker_image, worker_schema)
        if previous_worker_image:
            verify_worker_image(previous_worker_image, worker_schema)
        return {
            "passed": True,
            "checks": {
                "readiness": True,
                "analysis": True,
                "legacy_writes": True,
                "sessions": True,
                "owner_isolation": True,
                "immutability": True,
            },
            "rollback_image": previous,
            "candidate_image": candidate,
            "future_image": future,
            "baseline_image": baseline_image or (initial if future else None),
            "schema_image": schema_image or future or candidate,
            "worker_compatibility": (
                {
                    "passed": True,
                    "target_image": worker_image,
                    "previous_image": previous_worker_image,
                    "schema_image": worker_schema,
                }
                if worker_image
                else None
            ),
        }
    finally:
        for name in reversed(created):
            subprocess.run(
                ["docker", "rm", "-fv", name], capture_output=True, timeout=30, check=False
            )
        subprocess.run(
            ["docker", "network", "rm", network], capture_output=True, timeout=30, check=False
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-image", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--initial-image", default=INITIAL_IMAGE)
    parser.add_argument("--baseline-image")
    parser.add_argument("--future-image")
    parser.add_argument("--worker-image")
    parser.add_argument("--previous-worker-image")
    parser.add_argument(
        "--local-images",
        action="store_true",
        help="Use CI-built local fixtures; release preflight always pulls exact digests",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_images(
        args.previous_image,
        args.candidate_image,
        initial=args.initial_image,
        baseline_image=args.baseline_image,
        future=args.future_image,
        worker_image=args.worker_image,
        previous_worker_image=args.previous_worker_image,
        pull_images=not args.local_images,
    )
    args.output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
