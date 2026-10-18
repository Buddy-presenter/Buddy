"""Public identity configuration; no client secret belongs in a desktop application."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

CALLBACK = "org.buddy.desktop://oauth/callback"
SIGNOUT_CALLBACK = "org.buddy.desktop://oauth/signed-out"
BROWSER_CALLBACK_PATH = "/auth/desktop/callback"
BROWSER_SIGNOUT_PATH = "/auth/desktop/signed-out"
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
    callback_url: str = CALLBACK

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
        if self.callback_url != CALLBACK:
            try:
                target = urlsplit(self.callback_url)
                valid = (
                    len(self.callback_url) <= 2048
                    and (
                        (target.scheme == "https" and target.port in {None, 443})
                        or (
                            target.scheme == "http"
                            and target.hostname == "localhost"
                            and target.port == 8000
                        )
                    )
                    and bool(target.hostname)
                    and target.username is None
                    and target.password is None
                    and target.path == BROWSER_CALLBACK_PATH
                    and not target.query
                    and not target.fragment
                    and not any(ord(c) <= 32 or ord(c) >= 127 for c in self.callback_url)
                    and "\\" not in self.callback_url
                )
            except ValueError:
                valid = False
            if not valid:
                raise IdentityError("Invalid browser return URL; use an approved callback.")

    @property
    def logout_url(self) -> str:
        if self.callback_url == CALLBACK:
            return SIGNOUT_CALLBACK
        return self.callback_url.removesuffix(BROWSER_CALLBACK_PATH) + BROWSER_SIGNOUT_PATH

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
        if not any(values.get(key) for key in (*keys, "BUDDY_COGNITO_CALLBACK_URL")):
            return None
        return cls(
            values.get("BUDDY_AWS_REGION", ""),
            values.get("BUDDY_COGNITO_POOL_ID", ""),
            values.get("BUDDY_COGNITO_CLIENT_ID", ""),
            values.get("BUDDY_COGNITO_DOMAIN", ""),
            callback_url=values.get("BUDDY_COGNITO_CALLBACK_URL", CALLBACK),
        )
