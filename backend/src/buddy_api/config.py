"""Explicit environment configuration without loading or logging secret files."""

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from buddy_api.persistence.settings import DatabaseConfigurationError, DatabaseSettings
from buddy_api.uploads.config import UploadSettings
from buddy_identity.config import IdentityConfig, IdentityError


class ConfigurationError(ValueError):
    pass


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


def valid_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return len(value) <= 253 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in value.split(".")
        )


@dataclass(frozen=True)
class Settings:
    environment: Environment
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    bind_host: str = "127.0.0.1"
    port: int = 8000
    readiness_timeout: float = 2.0
    database: DatabaseSettings | None = field(default=None, repr=False)
    identity: IdentityConfig | None = None
    uploads: UploadSettings | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.environment, Environment):
            raise ConfigurationError("BUDDY_ENVIRONMENT must be development, test, or production.")
        if not self.allowed_hosts or not all(valid_host(host) for host in self.allowed_hosts):
            raise ConfigurationError(
                "BUDDY_ALLOWED_HOSTS needs exact hosts, without ports/wildcards."
            )
        if not valid_host(self.bind_host):
            raise ConfigurationError("BUDDY_BIND_HOST must be a valid host or IP address.")
        if not 1 <= self.port <= 65535:
            raise ConfigurationError("BUDDY_PORT must be an integer from 1 to 65535.")
        if not math.isfinite(self.readiness_timeout) or not 0.1 <= self.readiness_timeout <= 30:
            raise ConfigurationError("BUDDY_READINESS_TIMEOUT_SECONDS must be from 0.1 to 30.")
        if self.environment is Environment.PRODUCTION and (
            self.database is None or not self.database.production
        ):
            raise ConfigurationError(
                "Production requires a verified production BUDDY_DATABASE_URL."
            )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environ is None else environ
        try:
            environment = Environment(values["BUDDY_ENVIRONMENT"])
        except (KeyError, ValueError) as exc:
            raise ConfigurationError(
                "Set BUDDY_ENVIRONMENT to development, test, or production before starting the API."
            ) from exc
        if environment is Environment.PRODUCTION and not values.get("BUDDY_ALLOWED_HOSTS"):
            raise ConfigurationError("Production requires explicit BUDDY_ALLOWED_HOSTS.")
        hosts = tuple(
            host.strip().lower()
            for host in values.get("BUDDY_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",")
        )
        try:
            port = int(values.get("BUDDY_PORT", "8000"))
            timeout = float(values.get("BUDDY_READINESS_TIMEOUT_SECONDS", "2"))
        except ValueError as exc:
            raise ConfigurationError(
                "API port and readiness timeout must be valid numbers."
            ) from exc
        try:
            database = (
                DatabaseSettings.from_environment(
                    values, production=environment is Environment.PRODUCTION
                )
                if values.get("BUDDY_DATABASE_URL")
                else None
            )
        except DatabaseConfigurationError as exc:
            raise ConfigurationError(str(exc)) from None
        try:
            identity = IdentityConfig.from_environment(values)
        except IdentityError as exc:
            raise ConfigurationError(str(exc)) from None
        try:
            uploads = UploadSettings.from_environment(values)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from None
        return cls(
            environment,
            hosts,
            values.get("BUDDY_BIND_HOST", "127.0.0.1"),
            port,
            timeout,
            database,
            identity,
            uploads,
        )
