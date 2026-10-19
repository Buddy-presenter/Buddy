"""Real HTTP/crypto with conditional object storage shared across API instances."""

import base64
import hashlib
import io
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from buddy_api.app import create_app
from buddy_api.config import Environment, Settings
from buddy_api.handoff import CompleteBody, HandoffStorage, PollBody, StartBody, StateBody
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.storage import SourceStorage
from buddy_identity.config import HANDOFF_PATH


class StorageError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class Cloud:
    def __init__(self):
        self.objects = {}
        self.versions = []
        self.lock = Lock()
        self.unavailable = False

    def get_object(self, **kwargs):
        with self.lock:
            if self.unavailable:
                raise OSError("private storage diagnostic")
            if kwargs["Key"] not in self.objects:
                raise StorageError("NoSuchKey")
            content, etag = self.objects[kwargs["Key"]]
            return {"Body": io.BytesIO(content), "ETag": etag}

    def put_object(self, **kwargs):
        with self.lock:
            if self.unavailable:
                raise OSError("private storage diagnostic")
            previous = self.objects.get(kwargs["Key"])
            if kwargs.get("IfNoneMatch") == "*" and previous is not None:
                raise StorageError("PreconditionFailed")
            if "IfMatch" in kwargs and (previous is None or previous[1] != kwargs["IfMatch"]):
                raise StorageError("PreconditionFailed")
            assert kwargs["ServerSideEncryption"] == "aws:kms"
            assert "ACL" not in kwargs
            assert kwargs["ExpectedBucketOwner"] == "123456789012"
            content = kwargs["Body"]
            etag = hashlib.sha256(content).hexdigest()
            self.objects[kwargs["Key"]] = (content, etag)
            self.versions.append(content)
            return {"ETag": etag}

    def close(self):
        pass


@pytest.fixture
def handoff():
    cloud = Cloud()
    settings = UploadSettings(
        bucket="private-buddy-test",
        region="eu-central-1",
        kms_key="arn:aws:kms:eu-central-1:123456789012:key/"
        + "1" * 8
        + "-"
        + "1" * 4
        + "-"
        + "1" * 4
        + "-"
        + "1" * 4
        + "-"
        + "1" * 12,
    )
    storage = SourceStorage(settings, cloud)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    secret = secrets.token_urlsafe(32)
    body = StartBody(
        state="buddy-v1-" + secrets.token_urlsafe(32),
        read_hash=hashlib.sha256(secret.encode()).hexdigest(),
        public_key=base64.b64encode(
            private.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode(),
    )
    return HandoffStorage(storage), storage, cloud, private, secret, body


def encrypted_placeholder(body):
    return CompleteBody(
        state=body.state,
        sealed={
            "key": base64.b64encode(b"k" * 256).decode(),
            "nonce": base64.b64encode(b"n" * 12).decode(),
            "data": base64.b64encode(b"encrypted-result" * 2).decode(),
        },
    )


def test_shared_storage_restart_single_use_and_capability_isolation(handoff):
    first, storage, cloud, _private, secret, body = handoff
    second = HandoffStorage(storage)
    assert first.start(body) == {"status": "pending"}
    assert second.resolve(StateBody(state=body.state)) == {"public_key": body.public_key}
    with pytest.raises(HTTPException) as error:
        second.poll(PollBody(state=body.state), secrets.token_urlsafe(32))
    assert error.value.status_code == 403
    assert first.poll(PollBody(state=body.state), secret) == {"status": "pending"}
    sealed = encrypted_placeholder(body)
    second.complete(sealed)
    result = first.poll(PollBody(state=body.state), secret)
    assert result == {"status": "complete", "sealed": sealed.sealed.model_dump()}
    for operation in (
        lambda: second.poll(PollBody(state=body.state), secret),
        lambda: first.resolve(StateBody(state=body.state)),
        lambda: second.complete(sealed),
        lambda: first.start(body),
    ):
        with pytest.raises(HTTPException):
            operation()
    stored = b"".join(cloud.versions)
    assert secret.encode() not in stored and body.state.encode() not in stored
    assert "sealed" not in json.loads(cloud.objects[first.key(body.state)][0])


def test_expiry_cancellation_and_storage_failure_are_closed(handoff, monkeypatch):
    service, _, cloud, _, secret, body = handoff
    service.start(body)
    service.poll(PollBody(state=body.state, action="cancel"), secret)
    with pytest.raises(HTTPException):
        service.complete(encrypted_placeholder(body))
    monkeypatch.setattr("buddy_api.handoff.time.time", lambda: 10**12)
    with pytest.raises(HTTPException) as expired:
        service.poll(PollBody(state=body.state), secret)
    assert expired.value.status_code == 410
    cloud.unavailable = True
    with pytest.raises(HTTPException) as failed:
        service.resolve(StateBody(state=body.state))
    assert failed.value.status_code == 503
    assert "private" not in str(failed.value)


def test_concurrent_completions_and_collectors_have_only_one_winner(handoff, monkeypatch):
    service, _, _, _, secret, body = handoff
    service.start(body)
    load = service.load
    barrier = Barrier(8)

    def simultaneous_load(state):
        result = load(state)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(service, "load", simultaneous_load)

    def attempt(operation):
        try:
            return operation()
        except HTTPException:
            return None

    with ThreadPoolExecutor(8) as pool:
        results = list(
            pool.map(
                lambda _: attempt(lambda: service.complete(encrypted_placeholder(body))), range(8)
            )
        )
        assert sum(result is not None for result in results) == 1
        results = list(
            pool.map(
                lambda _: attempt(lambda: service.poll(PollBody(state=body.state), secret)),
                range(8),
            )
        )
        assert sum(result is not None for result in results) == 1


def test_registration_and_parallel_work_have_bounded_capacity(handoff, monkeypatch):
    service, _, _, _, _, body = handoff
    monkeypatch.setattr(service, "put", lambda *args: None)
    for _ in range(60):
        service.start(body)
    with pytest.raises(HTTPException) as limited:
        service.start(body)
    assert limited.value.status_code == 429
    for _ in range(8):
        assert service.capacity.acquire(blocking=False)
    with pytest.raises(HTTPException) as exhausted:
        service.run(lambda: {})
    assert exhausted.value.status_code == 429
    for _ in range(8):
        service.capacity.release()


def test_http_validation_bounds_and_sanitized_failures(handoff):
    _, storage, _, _, secret, body = handoff
    with TestClient(
        create_app(Settings(Environment.TEST, ("testserver",)), upload_storage=storage)
    ) as client:
        path = HANDOFF_PATH
        assert client.post(path + "/start", json=body.model_dump()).status_code == 200
        for authorization in (None, "Bearer " + secret, "Buddy-Handoff wrong"):
            headers = {"Authorization": authorization} if authorization else {}
            assert (
                client.post(path + "/poll", json={"state": body.state}, headers=headers).status_code
                == 401
            )
        assert client.post(
            path + "/poll",
            json={"state": body.state},
            headers={"Authorization": "Buddy-Handoff " + secret},
        ).json() == {"status": "pending"}
        assert (
            client.post(
                path + "/start", json={**body.model_dump(), "public_key": "!" * 392}
            ).status_code
            == 422
        )
        assert (
            client.post(
                path + "/start", content=b"x" * 12289, headers={"Content-Type": "application/json"}
            ).status_code
            == 413
        )
        assert client.post(path + "/key", json={"state": "buddy-v1-" + "x" * 43}).status_code == 410
        assert (
            client.post(
                path + "/key", json={"state": body.state, "extra": "private-code"}
            ).status_code
            == 422
        )
    with TestClient(create_app(Settings(Environment.TEST, ("testserver",)))) as client:
        response = client.post(path + "/start", json=body.model_dump())
        assert response.status_code == 503 and secret not in response.text
