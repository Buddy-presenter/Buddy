"""Publish exactly the verified manifest entries to the private environment prefix."""

import json
import os
from pathlib import Path

from frontend.scripts.release.smoke_package import verify


def publish(folder: Path, s3, sts) -> None:
    target, account = os.environ["RELEASE_TARGET"], os.environ["AWS_ACCOUNT"]
    revision = os.environ["GITHUB_SHA"]
    bucket = os.environ["RELEASE_BUCKET"]
    value = json.loads((folder / "manifest.json").read_text())
    if (
        target not in {"staging", "production"}
        or value["revision"] != revision
        or value["channel"] != ("stable" if target == "production" else "preview")
    ):
        raise ValueError("Manifest does not match the release environment/revision.")
    if (
        sts.get_caller_identity()["Account"] != account
        or bucket != f"buddy-{target}-{account}-releases"
    ):
        raise ValueError("Wrong AWS publication account or bucket.")
    artifacts = verify(folder)
    prefix = f"{target}/desktop/{revision}/{value['platform']}/"
    for artifact in [*artifacts, folder / "manifest.json"]:
        with artifact.open("rb") as stream:
            s3.put_object(Bucket=bucket, Key=prefix + artifact.name, Body=stream)


if __name__ == "__main__":
    import sys

    import boto3

    publish(Path(sys.argv[1]), boto3.client("s3"), boto3.client("sts"))
