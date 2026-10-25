"""Real worker, PostgreSQL and PDF path with fakes at external service boundaries."""

import argparse
import hashlib
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def docker(*args, timeout=180):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Worker smoke Docker {args[0]} failed: {result.stderr[-2000:]}")
    return result.stdout.strip()


def verify(image, schema_image=None):
    prefix = "buddy-worker-smoke-" + uuid4().hex[:10]
    pg, worker = prefix + "-pg", prefix + "-worker"
    source = (ROOT / "tests/fixtures/slides/Buddy_PDF_Geometry.pdf").read_bytes()
    owner, deck, job, lease = [str(uuid4()) for _ in range(4)]
    # The expired seeded lease starts at generation 1. Startup reconciliation fences it
    # and advances it to generation 2 with a short retry delay. The test driver delivers
    # this synthetic SQS wake-up only when the durable retry becomes due.
    wake_up = {
        "MessageId": str(uuid4()),
        "ReceiptHandle": "synthetic-receipt",
        "Body": json.dumps({"schema_version": "buddy.job.v1", "job_id": job, "generation": 2}),
    }
    messages = []
    accesses, acknowledged = [], []
    queue_url = "https://sqs.eu-central-1.amazonaws.com/123456789012/smoke"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            accesses.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(source)))
            self.end_headers()
            self.wfile.write(source)

        def do_POST(self):
            target = self.headers.get("X-Amz-Target", "").split(".")[-1]
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            result = {}
            if target == "GetQueueAttributes":
                result = {"Attributes": {"QueueArn": "arn:aws:sqs:eu-central-1:123456789012:smoke"}}
            elif target == "ReceiveMessage":
                result = {"Messages": messages[:1]}
                if not messages:
                    time.sleep(0.1)
            elif target == "DeleteMessage":
                acknowledged.append(request["ReceiptHandle"])
                messages.clear()
            else:
                raise AssertionError("Worker attempted an unexpected AWS operation")
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-amz-json-1.0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    containers = []
    try:
        docker(
            "run",
            "-d",
            "--name",
            pg,
            "-e",
            "POSTGRES_USER=buddy",
            "-e",
            "POSTGRES_PASSWORD=synthetic-smoke-only",
            "-e",
            "POSTGRES_DB=buddy_worker_test",
            "-p",
            "127.0.0.1::5432",
            "postgres:17",
        )
        containers.append(pg)
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", pg, "pg_isready", "-U", "buddy", "-d", "buddy_worker_test"],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Worker smoke PostgreSQL did not start")
        port = json.loads(docker("inspect", pg))[0]["NetworkSettings"]["Ports"]["5432/tcp"][0][
            "HostPort"
        ]
        url = f"postgresql://buddy:synthetic-smoke-only@127.0.0.1:{port}/buddy_worker_test?sslmode=disable"
        environment = ["-e", "BUDDY_ENVIRONMENT=test", "-e", "BUDDY_DATABASE_URL=" + url]
        docker(
            "run",
            "--rm",
            "--network",
            "host",
            *environment,
            "--entrypoint",
            "python",
            schema_image or image,
            "-m",
            "buddy_api.persistence.migrate",
            "upgrade",
        )

        def sql(statement):
            return docker(
                "exec",
                pg,
                "psql",
                "-U",
                "buddy",
                "-d",
                "buddy_worker_test",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-c",
                statement,
            )

        # An expired running record proves real startup reconciliation, not only a unit helper.
        sql(
            f"""INSERT INTO users(id,subject) VALUES ('{owner}','worker-smoke');
            INSERT INTO decks(id,owner_id,title,source_format,source_key,source_revision,
              source_version_id,source_size_bytes,source_sha256)
            VALUES ('{deck}','{owner}','smoke','pdf','sources/smoke/original.pdf',
              'smoke-v1','pinned',{len(source)},'{hashlib.sha256(source).hexdigest()}');
            INSERT INTO processing_jobs(id,owner_id,deck_id,status,source_revision,attempts,
              generation,lease_id,lease_until,deadline_at,stage_state)
            VALUES ('{job}','{owner}','{deck}','running','smoke-v1',1,1,'{lease}',
              now()-interval '1 second',now()+interval '120 seconds','running');
            INSERT INTO job_outbox(job_id,available_at) VALUES ('{job}',now());
            CREATE ROLE worker_smoke LOGIN PASSWORD 'synthetic-worker-only';
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            GRANT USAGE ON SCHEMA public TO worker_smoke;
            GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO worker_smoke;
            REVOKE INSERT,UPDATE,DELETE ON alembic_version FROM worker_smoke;"""
        )
        runtime_url = f"postgresql://worker_smoke:synthetic-worker-only@127.0.0.1:{port}/buddy_worker_test?sslmode=disable"
        env = {
            "BUDDY_ENVIRONMENT": "test",
            "BUDDY_DATABASE_URL": runtime_url,
            "BUDDY_UPLOADS_ENABLED": "true",
            "BUDDY_DATA_BUCKET": "buddy-smoke",
            "BUDDY_DATA_KMS_KEY_ARN": (
                "arn:aws:kms:eu-central-1:123456789012:key/00000000-0000-0000-0000-000000000000"
            ),
            "BUDDY_AWS_REGION": "eu-central-1",
            "BUDDY_PROCESSING_QUEUE_URL": queue_url,
            "AWS_ACCESS_KEY_ID": "synthetic-smoke",
            "AWS_SECRET_ACCESS_KEY": "synthetic-smoke",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ENDPOINT_URL_SQS": endpoint,
            "AWS_ENDPOINT_URL_S3": endpoint,
        }
        args = [arg for key, value in env.items() for arg in ("-e", key + "=" + value)]
        docker(
            "run",
            "-d",
            "--name",
            worker,
            "--network",
            "host",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--memory=2g",
            "--cpus=1",
            "--tmpfs",
            "/var/lib/buddy-upload:rw,noexec,nosuid,size=512m,uid=10001,gid=10001",
            "-v",
            str(ROOT / "infra/container/smoke_worker_entrypoint.py")
            + ":/smoke_worker_entrypoint.py:ro",
            "-v",
            str(ROOT / "backend/tests/fixtures/pdf/scanned-source-anchor.pdf")
            + ":/scanned-source-anchor.pdf:ro",
            "-e",
            "BUDDY_SMOKE_PDF_SHA256=" + hashlib.sha256(source).hexdigest(),
            *args,
            "--entrypoint",
            "python",
            image,
            "/smoke_worker_entrypoint.py",
        )
        containers.append(worker)
        for _ in range(90):
            due = sql(
                f"""SELECT status = 'retry_wait' AND generation = 2 AND not_before <= now()
                    FROM processing_jobs WHERE id='{job}'"""
            )
            if not messages and due == "t":
                messages.append(wake_up)
            state = sql(f"SELECT status FROM processing_jobs WHERE id='{job}'")
            if state == "analysis_ready":
                break
            if state in {"dead_letter", "failed"}:
                error = sql(f"SELECT error_code FROM processing_jobs WHERE id='{job}'")
                raise RuntimeError(
                    f"Worker smoke failed analysis: status={state}, error={error}; "
                    + docker("logs", worker)
                )
            if json.loads(docker("inspect", worker))[0]["State"]["Status"] != "running":
                raise RuntimeError("Worker smoke exited: " + docker("logs", worker))
            time.sleep(1)
        else:
            details = sql(
                f"""SELECT status || '|' || stage || ':' || stage_state || '|error=' ||
                    COALESCE(error_code, '<none>') || '|generation=' || generation ||
                    '|attempts=' || attempts || '|lease_until=' ||
                    COALESCE(lease_until::text, '<none>') || '|deadline_at=' ||
                    COALESCE(deadline_at::text, '<none>')
                    FROM processing_jobs WHERE id='{job}'"""
            )
            raise RuntimeError(
                f"Worker smoke did not complete PDF inspection: {details}, "
                f"source_requests={accesses}; " + docker("logs", worker)
            )
        assert sql(f"SELECT attempts FROM processing_jobs WHERE id='{job}'") == "2"
        assert sql("SELECT jsonb_array_length(document->'slides') FROM deck_extractions") == "3"
        assert sql("SELECT count(*) FROM analysis_versions") == "1"
        assert accesses and "versionId=pinned" in accesses[0]
        docker("exec", worker, "python", "-m", "buddy_api.processing", "--health")
        docker("restart", worker)
        time.sleep(3)
        assert sql(f"SELECT status FROM processing_jobs WHERE id='{job}'") == "analysis_ready"
        assert sql("SELECT count(*) FROM deck_extractions") == "1"
        assert sql("SELECT count(*) FROM analysis_versions") == "1"
        docker("stop", "--time", "120", worker)
        source = b"Malformed PDF fixture"
        bad_deck, bad_job = str(uuid4()), str(uuid4())
        sql(f"""INSERT INTO decks(id,owner_id,title,source_format,source_key,source_revision,
          source_version_id,source_size_bytes,source_sha256)
        VALUES ('{bad_deck}','{owner}','bad','pdf','sources/smoke/bad.pdf','bad-v1',
          'pinned',{len(source)},'{hashlib.sha256(source).hexdigest()}');
        INSERT INTO processing_jobs(id,owner_id,deck_id,status,source_revision)
        VALUES ('{bad_job}','{owner}','{bad_deck}','queued','bad-v1');
        INSERT INTO job_outbox(job_id,available_at) VALUES ('{bad_job}',now());""")
        messages[:] = [
            {
                "MessageId": str(uuid4()),
                "ReceiptHandle": "bad-receipt",
                "Body": json.dumps(
                    {"schema_version": "buddy.job.v1", "job_id": bad_job, "generation": 0}
                ),
            }
        ]
        docker("start", worker)
        for _ in range(60):
            if sql(f"SELECT status FROM processing_jobs WHERE id='{bad_job}'") == "dead_letter":
                break
            time.sleep(1)
        else:
            raise RuntimeError("Worker container did not persist malformed-PDF failure")
        assert (
            sql(f"SELECT error_code FROM processing_jobs WHERE id='{bad_job}'")
            == "pdf_inspection_failed"
        )
        assert sql("SELECT count(*) FROM deck_extractions") == "1"
        docker("stop", "--time", "120", worker)
        print(
            "Worker image: startup, restricted DB role, real PDF, exact original-PDF hash, "
            "provider-boundary response, analysis persistence, lease recovery, pinned S3 HTTP, "
            "malformed-PDF failure, restart and shutdown passed."
        )
    finally:
        for name in reversed(containers):
            docker("rm", "-f", name)
        server.shutdown()
        server.server_close()
        thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--schema-image",
        help="API image whose migrations define the schema used to test worker compatibility.",
    )
    args = parser.parse_args()
    verify(args.image, args.schema_image)


if __name__ == "__main__":
    main()
