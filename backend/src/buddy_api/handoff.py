"""Short-lived, capability-protected encrypted desktop returns in private S3.

Only ciphertext, a public key and a digest of the collection secret are durable.
Conditional S3 writes serialize completion/consumption across API tasks and restarts.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import deque
from collections.abc import Callable
from threading import BoundedSemaphore, Lock
from typing import Annotated, Any, Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from buddy_api.uploads.storage import SourceStorage
from buddy_identity.config import HANDOFF_PATH


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StateBody(MessageBody):
    state: str = Field(pattern=r"^buddy-v1-[A-Za-z0-9_-]{43}$")


class StartBody(StateBody):
    read_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    public_key: str = Field(min_length=392, max_length=392)

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        try:
            key = serialization.load_der_public_key(base64.b64decode(value, validate=True))
        except Exception:
            raise ValueError("Unsupported handoff public key") from None
        if (
            not isinstance(key, rsa.RSAPublicKey)
            or key.key_size != 2048
            or key.public_numbers().e != 65537
        ):
            raise ValueError("Unsupported handoff public key")
        return value


class Sealed(MessageBody):
    key: str = Field(min_length=344, max_length=344)
    nonce: str = Field(min_length=16, max_length=16)
    data: str = Field(min_length=24, max_length=8192)

    @field_validator("key", "nonce", "data")
    @classmethod
    def valid_encoding(cls, value: str, info: ValidationInfo) -> str:
        decoded = base64.b64decode(value, validate=True)
        if (info.field_name == "key" and len(decoded) != 256) or (
            info.field_name == "nonce" and len(decoded) != 12
        ):
            raise ValueError("Invalid encrypted return")
        return value


class CompleteBody(StateBody):
    sealed: Sealed


class PollBody(StateBody):
    action: Literal["poll", "cancel"] = "poll"


class HandoffLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        size = 0

        async def bounded() -> Message:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > 12288:
                raise HTTPException(413)
            return message

        target = scope["type"] == "http" and scope["path"].startswith(HANDOFF_PATH + "/")
        await self.app(scope, bounded if target else receive, send)


class HandoffStorage:
    def __init__(self, storage: SourceStorage) -> None:
        self.storage = storage
        self.capacity = BoundedSemaphore(8)
        self.lock = Lock()
        self.starts: deque[float] = deque()

    def run(self, operation: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        if not self.capacity.acquire(blocking=False):
            raise HTTPException(429, headers={"Retry-After": "2"})
        try:
            return operation(*args)
        finally:
            self.capacity.release()

    @staticmethod
    def key(state: str) -> str:
        return "auth-handoffs/v1/" + hashlib.sha256(state.encode()).hexdigest()

    def load(self, state: str) -> tuple[dict[str, Any], str]:
        body = None
        try:
            response = self.storage.client().get_object(
                **self.storage.bucket_arguments, Key=self.key(state)
            )
            body = response["Body"]
            content = body.read(12289)
            if len(content) > 12288:
                raise ValueError
            record = json.loads(content)
            if not isinstance(record, dict) or record["expires"] <= time.time():
                raise HTTPException(410)
            return record, str(response["ETag"])
        except HTTPException:
            raise
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
                raise HTTPException(410) from None
            raise HTTPException(503) from None
        finally:
            if body is not None:
                body.close()

    def put(self, state: str, record: dict[str, Any], etag: str | None = None) -> None:
        condition = {"IfMatch": etag} if etag is not None else {"IfNoneMatch": "*"}
        try:
            self.storage.client().put_object(
                **self.storage.bucket_arguments,
                Key=self.key(state),
                Body=json.dumps(record).encode(),
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self.storage.settings.kms_key,
                **condition,
            )
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
            }:
                raise HTTPException(409) from None
            raise HTTPException(503) from None

    def start(self, body: StartBody) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            while self.starts and self.starts[0] <= now - 60:
                self.starts.popleft()
            if len(self.starts) >= 60:
                raise HTTPException(429, headers={"Retry-After": "60"})
            self.starts.append(now)
        self.put(
            body.state,
            {
                "read_hash": body.read_hash,
                "public_key": body.public_key,
                "expires": int(time.time()) + 300,
                "status": "pending",
            },
        )
        return {"status": "pending"}

    def resolve(self, body: StateBody) -> dict[str, Any]:
        record, _ = self.load(body.state)
        if record["status"] != "pending":
            raise HTTPException(410)
        return {"public_key": record["public_key"]}

    def complete(self, body: CompleteBody) -> dict[str, Any]:
        record, etag = self.load(body.state)
        if record["status"] != "pending":
            raise HTTPException(409)
        record.update(status="complete", sealed=body.sealed.model_dump())
        self.put(body.state, record, etag)
        return {"status": "complete"}

    def poll(self, body: PollBody, secret: str) -> dict[str, Any]:
        record, etag = self.load(body.state)
        if not hmac.compare_digest(
            record["read_hash"], hashlib.sha256(secret.encode()).hexdigest()
        ):
            raise HTTPException(403)
        if record["status"] == "consumed":
            raise HTTPException(410)
        result = {"status": record["status"]}
        if body.action == "cancel":
            result = {"status": "consumed"}
        if body.action == "cancel" or record["status"] == "complete":
            if body.action != "cancel":
                result["sealed"] = record["sealed"]
            # Leave a tombstone until lifecycle expiry: no late callback/replay can
            # recreate the attempt. Remove public key/ciphertext from the new version.
            self.put(
                body.state,
                {
                    "expires": record["expires"],
                    "read_hash": record["read_hash"],
                    "status": "consumed",
                },
                etag,
            )
        return result


def handoff_router(storage: SourceStorage | None) -> APIRouter:
    router = APIRouter(prefix=HANDOFF_PATH, include_in_schema=False)
    service = HandoffStorage(storage) if storage else None

    def available() -> HandoffStorage:
        if service is None:
            raise HTTPException(503)
        return service

    @router.post("/start")
    async def start(body: StartBody) -> dict[str, Any]:
        service = available()
        return await asyncio.to_thread(service.run, service.start, body)

    @router.post("/key")
    async def key(body: StateBody) -> dict[str, Any]:
        service = available()
        return await asyncio.to_thread(service.run, service.resolve, body)

    @router.post("/complete")
    async def complete(body: CompleteBody) -> dict[str, Any]:
        service = available()
        return await asyncio.to_thread(service.run, service.complete, body)

    @router.post("/poll")
    async def poll(
        body: PollBody,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        import re

        if request.headers.getlist("authorization") != [authorization] or not authorization:
            raise HTTPException(401)
        if not re.fullmatch(r"Buddy-Handoff [A-Za-z0-9_-]{43}", authorization):
            raise HTTPException(401)
        service = available()
        return await asyncio.to_thread(service.run, service.poll, body, authorization.split(" ")[1])

    return router
