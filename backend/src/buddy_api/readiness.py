"""Real dependency adapters are registered here by later service features."""

import asyncio
import re
from collections.abc import Sequence
from typing import Protocol

from buddy_api.contracts import DependencyStatus


class DependencyProbe(Protocol):
    @property
    def name(self) -> str: ...

    async def check(self) -> None:
        """Raise on failure; never mutate application data or return fabricated success."""
        ...


def validate_probes(probes: Sequence[DependencyProbe]) -> None:
    names = [probe.name for probe in probes]
    if len(set(names)) != len(names) or any(
        not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) for name in names
    ):
        raise ValueError("Readiness probes require unique, safe dependency names.")


async def check_dependencies(
    probes: Sequence[DependencyProbe], timeout: float
) -> list[DependencyStatus]:
    async def check(probe: DependencyProbe) -> DependencyStatus:
        try:
            await asyncio.wait_for(probe.check(), timeout=timeout)
        except Exception:
            # Names are validated at startup; exception text could contain credentials.
            return DependencyStatus(name=probe.name, status="unavailable")
        return DependencyStatus(name=probe.name, status="ready")

    return list(await asyncio.gather(*(check(probe) for probe in probes)))
