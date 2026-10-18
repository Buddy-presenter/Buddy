"""Select a successful current-main revision for automatic staging, without AWS access."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from gate import approved_checks


def github_json(path: str):
    result = subprocess.run(["gh", "api", path], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def eligible_event(event: dict, repository: str) -> bool:
    run = event.get("workflow_run", {})
    return (
        event.get("repository", {}).get("full_name") == repository
        and run.get("head_repository", {}).get("full_name") == repository
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("conclusion") == "success"
        and run.get("name") == "Continuous integration"
        and re.fullmatch(r"[a-f0-9]{40}", run.get("head_sha", "")) is not None
    )


def select_revision(event: dict, repository: str, *, fetch=github_json, sleep=time.sleep) -> str:
    if not eligible_event(event, repository):
        return ""
    revision = event["workflow_run"]["head_sha"]
    # Infrastructure and native packages run independently from CI. Bound waiting
    # without granting this job AWS/OIDC or checking out a PR's code.
    for attempt in range(90):
        main = fetch(f"repos/{repository}/git/ref/heads/main")["object"]["sha"]
        if main != revision:
            print("Skipping superseded main revision.")
            return ""
        checks = fetch(f"repos/{repository}/commits/{revision}/check-runs?per_page=100")
        if approved_checks(checks["check_runs"], revision):
            return revision
        if any(
            check.get("head_sha") == revision
            and check.get("name")
            in {
                "CI",
                "Terraform boundaries",
                "API container and database bootstrap",
                "Native package (windows-latest)",
                "Native package (macos-latest)",
            }
            and check.get("status") == "completed"
            and check.get("conclusion") not in {"success"}
            for check in checks["check_runs"]
        ):
            raise ValueError("Required main checks failed or were cancelled/skipped.")
        if attempt < 89:
            sleep(20)
    raise TimeoutError("Required main checks did not all succeed within 30 minutes.")


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    revision = select_revision(event, os.environ["GITHUB_REPOSITORY"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"revision={revision}\ndeploy={'true' if revision else 'false'}\n")


if __name__ == "__main__":
    main()
