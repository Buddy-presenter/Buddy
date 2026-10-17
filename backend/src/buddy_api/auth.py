"""Cognito access verification and durable account bootstrap; resource ownership is #31."""

import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Annotated, Any
from urllib.request import Request as HttpRequest
from uuid import UUID

import jwt
from fastapi import APIRouter, Header, Request
from jwt import PyJWKClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from buddy_api.contracts import ErrorResponse
from buddy_api.persistence.models import User
from buddy_identity.config import IdentityConfig, IdentityError, SessionExpired
from buddy_identity.transport import request_json


class AccountData(BaseModel):
    id: UUID


class AccountResponse(BaseModel):
    data: AccountData
    request_id: str


class AttemptLimiter:
    """Bounded bootstrap credential budgets; trusted-edge WAF owns client-IP limits."""

    def __init__(self) -> None:
        self.buckets: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self.lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            start, count = self.buckets.get(key, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            self.buckets[key] = (start, count + 1)
            self.buckets.move_to_end(key)
            if len(self.buckets) > 4096:
                self.buckets.popitem(last=False)
            return count < 30


class CognitoVerifier:
    def __init__(self, config: IdentityConfig) -> None:
        self.config = config
        self.keys = PyJWKClient(config.issuer + "/.well-known/jwks.json", timeout=5, lifespan=300)

    def verify(self, token: str) -> str:
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                options={
                    "verify_aud": False,
                    "require": ["exp", "iat", "sub", "iss", "client_id", "token_use"],
                },
            )
            if (
                claims["token_use"] != "access"
                or claims["client_id"] != self.config.client_id
                or not isinstance(claims["sub"], str)
                or not re.fullmatch(r"[a-f0-9-]{36}", claims["sub"])
            ):
                raise ValueError
        except jwt.PyJWKClientConnectionError:
            raise IdentityError("Identity verification is temporarily unavailable.") from None
        except (jwt.PyJWTError, ValueError, KeyError):
            raise SessionExpired("Sign-in expired or was rejected. Please sign in again.") from None
        # Signature/expiry checks alone do not detect Cognito revocation. GetUser does.
        # This call is deliberately never cached and requires no AWS access key.
        result = request_json(
            HttpRequest(
                f"https://cognito-idp.{self.config.region}.amazonaws.com/",
                data=json.dumps({"AccessToken": token}).encode(),
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSCognitoIdentityProviderService.GetUser",
                },
            )
        )
        attributes = result.get("UserAttributes")
        # GetUser is trusted provider state; access tokens need not carry email claims.
        # Reject duplicate/malformed attributes instead of accepting an ambiguous value.
        values: dict[str, str] = {}
        if not isinstance(attributes, list):
            raise SessionExpired("Sign-in expired or was rejected. Please sign in again.")
        for item in attributes:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("Name"), str)
                or not isinstance(item.get("Value"), str)
                or item["Name"] in values
            ):
                raise SessionExpired("Sign-in expired or was rejected. Please sign in again.")
            values[item["Name"]] = item["Value"]
        if (
            values.get("sub") != claims["sub"]
            or values.get("email_verified") != "true"
            or not re.fullmatch(r"[^\s@]+@[^\s@]+", values.get("email", ""))
        ):
            raise SessionExpired("Sign-in expired or was rejected. Please sign in again.")
        return self.config.issuer + "#" + str(claims["sub"])


@dataclass(frozen=True)
class Principal:
    """An account resolved from verified provider state, never a client-supplied UUID."""

    id: UUID


class Authentication:
    def __init__(self, config: IdentityConfig | None) -> None:
        self.verifier = CognitoVerifier(config) if config else None
        self.limiter = AttemptLimiter()

    async def identify(self, request: Request, authorization: str | None) -> Principal:
        if self.verifier is None or request.app.state.database is None:
            raise HTTPException(503)
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or len(authorization) > 16384
            or not authorization[7:].strip()
        ):
            raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
        try:
            subject = await run_in_threadpool(self.verifier.verify, authorization[7:])
            async with request.app.state.database.sessions() as session, session.begin():
                await session.execute(
                    insert(User)
                    .values(subject=subject)
                    .on_conflict_do_nothing(index_elements=[User.subject])
                )
                identifier = (
                    await session.execute(select(User.id).where(User.subject == subject))
                ).scalar_one()
        except SessionExpired:
            raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"}) from None
        except (IdentityError, SQLAlchemyError, OSError, TimeoutError):
            raise HTTPException(503) from None
        return Principal(identifier)


async def authenticated(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    authentication: Authentication = request.app.state.authentication
    if (
        not authorization
        or not authorization.startswith("Bearer ")
        or not authorization[7:].strip()
    ):
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    return await authentication.identify(request, authorization)


def auth_router(config: IdentityConfig | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["account"])

    @router.get(
        "/me",
        response_model=AccountResponse,
        responses={code: {"model": ErrorResponse} for code in (401, 429, 503)},
    )
    async def account(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> AccountResponse:
        authentication: Authentication = request.app.state.authentication
        # The direct peer is an ALB in production. Never share that peer's budget
        # across accounts, and never charge resource/SSE checks to bootstrap limits.
        # Store only an opaque digest; forwarded headers are not trusted here.
        credential = hashlib.sha256((authorization or "").encode()).hexdigest()
        if not authentication.limiter.allow(credential):
            raise HTTPException(429, headers={"Retry-After": "60"})
        principal = await authentication.identify(request, authorization)
        return AccountResponse(
            data=AccountData(id=principal.id), request_id=request.state.request_id
        )

    return router
