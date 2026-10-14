"""Initialize scoped DB logins with workload credentials, without printing secrets."""

import asyncio
import json
import os
import secrets
import ssl
import sys
from urllib.parse import quote

import asyncpg
import boto3

ROLES = {"api": "buddy_api", "migration": "buddy_migration", "worker": "buddy_worker"}


async def provision() -> None:
    client = boto3.client("secretsmanager", region_name=os.environ["BUDDY_AWS_REGION"])
    admin = json.loads(
        client.get_secret_value(SecretId=os.environ["BUDDY_DB_ADMIN_SECRET"])["SecretString"]
    )
    host = os.environ["BUDDY_DB_HOST"]
    targets = json.loads(os.environ["BUDDY_DB_SECRET_ARNS"])
    # Resume safely after partial failure. Passwords are staged in their final private
    # secret before SQL runs; a retry reuses them instead of locking out running tasks.
    credentials = {}
    for service, role in ROLES.items():
        try:
            credentials[service] = json.loads(
                client.get_secret_value(SecretId=targets[service])["SecretString"]
            )
        except client.exceptions.ResourceNotFoundException:
            password = secrets.token_urlsafe(32)
            credentials[service] = {
                "username": role,
                "password": password,
                "url": (
                    f"postgresql://{role}:{quote(password, safe='')}@{host}:5432/buddy"
                    "?sslmode=verify-full"
                ),
            }
            client.put_secret_value(
                SecretId=targets[service], SecretString=json.dumps(credentials[service])
            )
        if credentials[service].get("username") != role:
            raise ValueError("Unexpected database role in secret.")
        password = credentials[service]["password"]
        url = (
            f"postgresql://{role}:{quote(password, safe='')}@{host}:5432/buddy?sslmode=verify-full"
        )
        if credentials[service]["url"] != url:
            credentials[service]["url"] = url
            client.put_secret_value(
                SecretId=targets[service], SecretString=json.dumps(credentials[service])
            )
    connection = await asyncpg.connect(
        host=host,
        port=5432,
        database="buddy",
        user=admin["username"],
        password=admin["password"],
        ssl=ssl.create_default_context(),
        timeout=10,
        command_timeout=30,
    )
    try:
        async with connection.transaction():
            await connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            await connection.execute("REVOKE ALL ON DATABASE buddy FROM PUBLIC")
            for service, role in ROLES.items():
                # Role names are fixed code constants; SQL formats the random secret server-side.
                if not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                    await connection.execute(
                        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE"
                    )
                statement = await connection.fetchval(
                    "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)",
                    role,
                    credentials[service]["password"],
                )
                await connection.execute(statement)
                await connection.execute(f"GRANT CONNECT ON DATABASE buddy TO {role}")
                await connection.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            await connection.execute("GRANT buddy_migration TO buddyadmin")
            await connection.execute("GRANT CREATE ON SCHEMA public TO buddy_migration")
            for role in ("buddy_api", "buddy_worker"):
                await connection.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
                )
                await connection.execute(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"
                )
                await connection.execute(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE buddy_migration IN SCHEMA public "
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
                )
                await connection.execute(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE buddy_migration IN SCHEMA public "
                    f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
                )
            if await connection.fetchval("SELECT to_regclass('public.alembic_version')"):
                await connection.execute(
                    "REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM buddy_api, buddy_worker"
                )
    finally:
        await connection.close()


def main() -> int:
    try:
        asyncio.run(provision())
    except Exception:
        # AWS/SQL driver exceptions can contain credentials or SQL literals.
        print(
            "Database bootstrap failed. Check networking, IAM and secrets; retry safely.",
            file=sys.stderr,
        )
        return 1
    print("Database roles and private runtime secrets initialized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
