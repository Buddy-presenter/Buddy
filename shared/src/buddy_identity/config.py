"""Public identity configuration; no client secret belongs in a desktop application."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

CALLBACK = "org.buddy.desktop://oauth/callback"
SIGNOUT_CALLBACK = "org.buddy.desktop://oauth/signed-out"
SCOPES = "openid email aws.cognito.signin.user.admin"


class IdentityError(Exception):
    """Only fixed, credential-free messages may cross the service boundary."""


class SessionExpired(IdentityError):
    pass


@dataclass(frozen=True)
class IdentityConfig:
    region: str
    pool_id: str
    client_id: str
    domain: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z]{2}-(?:[a-z]+-)?[a-z]+-\d", self.region)
            or not re.fullmatch(re.escape(self.region) + r"_[A-Za-z0-9]+", self.pool_id)
            or not re.fullmatch(r"[a-z0-9]{1,128}", self.client_id)
            or not re.fullmatch(
                r"https://[a-z0-9-]+\.auth\." + re.escape(self.region) + r"\.amazoncognito\.com",
                self.domain,
            )
        ):
            raise IdentityError(
                "Invalid Cognito configuration; verify region, pool, client and domain."
            )

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.pool_id}"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "IdentityConfig | None":
        values = os.environ if environ is None else environ
        keys = (
            "BUDDY_AWS_REGION",
            "BUDDY_COGNITO_POOL_ID",
            "BUDDY_COGNITO_CLIENT_ID",
            "BUDDY_COGNITO_DOMAIN",
        )
        if not any(values.get(key) for key in keys):
            return None
        return cls(*(values.get(key, "") for key in keys))
