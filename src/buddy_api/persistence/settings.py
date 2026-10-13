"""Validate database configuration without rendering credentials in errors or reprs."""

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


class DatabaseConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(repr=False)
    schema: str = "public"
    timeout: float = 3.0
    production: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise DatabaseConfigurationError(
                "Database schema must be a simple lowercase identifier."
            )
        if not math.isfinite(self.timeout) or not 0.1 <= self.timeout <= 30:
            raise DatabaseConfigurationError("Database timeout must be from 0.1 to 30 seconds.")
        self.connection_url()

    def connection_url(self) -> URL:
        try:
            url = make_url(self.url)
            if (
                url.drivername not in {"postgresql", "postgresql+asyncpg"}
                or not url.host
                or not url.database
                or not url.username
                or (url.port is not None and not 1 <= url.port <= 65535)
                or set(url.query) - {"sslmode"}
            ):
                raise ValueError
            mode = url.query.get("sslmode", "prefer")
            if mode not in {"disable", "prefer", "require", "verify-ca", "verify-full"}:
                raise ValueError
            remote = url.host not in {"localhost", "127.0.0.1", "::1"}
            if (self.production or remote) and mode != "verify-full":
                raise DatabaseConfigurationError(
                    "Production/remote databases require sslmode=verify-full and a trusted certificate."
                )
            return url.set(drivername="postgresql+asyncpg", query={})
        except DatabaseConfigurationError:
            raise
        except (ArgumentError, ValueError, TypeError):
            raise DatabaseConfigurationError(
                "BUDDY_DATABASE_URL needs a PostgreSQL host, database and user; "
                "only sslmode is allowed."
            ) from None

    @property
    def ssl_mode(self) -> str:
        return str(make_url(self.url).query.get("sslmode", "prefer"))

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, production: bool = False
    ) -> "DatabaseSettings":
        values = os.environ if environ is None else environ
        if not values.get("BUDDY_DATABASE_URL"):
            raise DatabaseConfigurationError(
                "Set BUDDY_DATABASE_URL before running database operations."
            )
        try:
            timeout = float(values.get("BUDDY_DATABASE_TIMEOUT_SECONDS", "3"))
        except ValueError:
            raise DatabaseConfigurationError(
                "Database timeout must be a number from 0.1 to 30."
            ) from None
        return cls(
            values["BUDDY_DATABASE_URL"],
            values.get("BUDDY_DATABASE_SCHEMA", "public"),
            timeout,
            production,
        )
