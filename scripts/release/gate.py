"""Require an exact successful CI revision and explicit production acceptance."""

import json
import os
import re
import subprocess


def approved_checks(checks: list[dict], revision: str) -> bool:
    required = {
        "CI",
        "Terraform boundaries",
        "API container and database bootstrap",
        "Native package (windows-latest)",
        "Native package (macos-latest)",
    }
    return all(
        any(
            check.get("name") == name
            and check.get("head_sha") == revision
            and check.get("status") == "completed"
            and check.get("conclusion") == "success"
            for check in checks
        )
        for name in required
    )


def main() -> int:
    revision = os.environ.get("RELEASE_REVISION", os.environ["GITHUB_SHA"])
    target = os.environ["RELEASE_TARGET"]
    phase = os.environ.get("ROLLOUT_PHASE", "feature")
    if phase not in {"feature", "compatibility"} or (
        phase == "compatibility" and target != "staging"
    ):
        raise ValueError("The compatibility bridge is a staging-only transition.")
    if (
        not re.fullmatch(r"[a-f0-9]{40}", revision)
        or os.environ["GITHUB_REF"] != "refs/heads/main"
        or target not in {"staging", "production"}
    ):
        raise ValueError("Release requires an exact main revision and known environment.")
    if os.environ.get("AUTOMATIC_STAGING") == "true":
        if (
            target != "staging"
            or phase != "feature"
            or os.environ.get("RELEASE_OPERATION") == "rollback"
        ):
            raise ValueError("Automatic releases may only promote staging features.")
        current = subprocess.run(
            ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/git/ref/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        )
        if json.loads(current.stdout)["object"]["sha"] != revision:
            raise ValueError("Automatic staging revision has been superseded on main.")
    if os.environ.get("RELEASE_OPERATION") == "rollback":
        # Recovery restores an independently validated healthy audit record, not this build.
        return 0
    if target == "production" and (
        os.environ.get("STAGING_ACCEPTED_REVISION") != revision
        or os.environ.get("CALLBACK_RELEASE_APPROVED") != "true"
    ):
        raise ValueError(
            "Production requires same-revision live staging acceptance "
            "and callback release approval."
        )
    response = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{os.environ['GITHUB_REPOSITORY']}/commits/{revision}/check-runs?per_page=100",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if not approved_checks(json.loads(response.stdout)["check_runs"], revision):
        raise ValueError("Required current-revision checks are not successful.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
