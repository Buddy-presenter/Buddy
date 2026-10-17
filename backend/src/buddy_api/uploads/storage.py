"""S3 atomic private PUTs and exact-key version cleanup, using workload identity."""

import asyncio
import base64
from pathlib import Path
from typing import Any, Protocol, cast

from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import PPTX_TYPE, UploadError


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def list_object_versions(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def head_bucket(self, **kwargs: Any) -> dict[str, Any]: ...
    def close(self) -> None: ...


class SourceStorage:
    def __init__(self, settings: UploadSettings, client: S3Client | None = None):
        self.settings = settings
        self._client = client

    def client(self) -> S3Client:
        if self._client is None:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]

            self._client = cast(
                S3Client,
                boto3.client(
                    "s3",
                    region_name=self.settings.region,
                    config=Config(
                        connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 1}
                    ),
                ),
            )
        return self._client

    @property
    def bucket_arguments(self) -> dict[str, str]:
        return {
            "Bucket": self.settings.bucket,
            "ExpectedBucketOwner": self.settings.kms_key.split(":")[4],
        }

    def put(self, key: str, path: Path, *, size: int, sha256: str, revision: str) -> str:
        try:
            with path.open("rb") as stream:
                result = self.client().put_object(
                    **self.bucket_arguments,
                    Key=key,
                    Body=stream,
                    ContentLength=size,
                    ContentType=PPTX_TYPE,
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
                    ServerSideEncryption="aws:kms",
                    SSEKMSKeyId=self.settings.kms_key,
                    IfNoneMatch="*",
                    Metadata={"source-revision": revision, "sha256": sha256},
                )
            version = result.get("VersionId")
            if (
                not isinstance(version, str)
                or not version
                or version == "null"
                or len(version) > 1024
            ):
                raise ValueError("Versioned storage is required")
            return version
        except Exception:
            raise UploadError(
                503, "storage_unavailable", "Private file storage is temporarily unavailable.", 15
            ) from None

    def remove(self, key: str) -> None:
        try:
            # The bucket is versioned: a plain DeleteObject would leave source bytes
            # behind a delete marker. Delete all versions/markers for this exact key.
            for _ in range(16):
                listing = self.client().list_object_versions(
                    **self.bucket_arguments,
                    Prefix=key,
                    MaxKeys=100,
                )
                versions = [
                    item
                    for kind in ("Versions", "DeleteMarkers")
                    for item in listing.get(kind, [])
                    if item.get("Key") == key
                ]
                if not versions:
                    if listing.get("IsTruncated"):
                        raise ValueError("Ambiguous cleanup listing")
                    return
                for version in versions:
                    self.client().delete_object(
                        **self.bucket_arguments,
                        Key=key,
                        VersionId=version["VersionId"],
                    )
            raise ValueError("Cleanup budget exceeded; reconciliation will retry")
        except Exception:
            raise UploadError(
                503, "cleanup_pending", "File cleanup is pending; retry later.", 30
            ) from None

    def check(self) -> None:
        try:
            self.client().head_bucket(**self.bucket_arguments)
        except Exception:
            raise UploadError(
                503, "storage_unavailable", "Private storage is unavailable."
            ) from None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


class StorageProbe:
    name = "source_storage"

    def __init__(self, storage: SourceStorage):
        self.storage = storage

    async def check(self) -> None:
        # Read-only work can be discarded on the existing readiness deadline.
        await asyncio.to_thread(self.storage.check)
