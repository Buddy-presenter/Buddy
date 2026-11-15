"""Cohere Embed 5 transport with bounded inputs, outputs and private diagnostics."""

import asyncio
import json
import math
from dataclasses import dataclass, field
from typing import Literal

import httpx

DOCUMENT_MODEL = "embed-v5.0-pro"
QUERY_MODEL = "embed-v5.0-fast"
DIMENSIONS = 512
SPACE = "cohere-embed5-512-v1"


class EmbeddingFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EmbeddingSettings:
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or len(self.api_key) > 4096 or any(c.isspace() for c in self.api_key):
            raise ValueError("Invalid Cohere credentials")


def normalize(raw: object) -> tuple[float, ...]:
    if not isinstance(raw, list) or len(raw) != DIMENSIONS:
        raise EmbeddingFailure("embedding_invalid_response")
    if any(type(v) not in {int, float} or not math.isfinite(v) for v in raw):
        raise EmbeddingFailure("embedding_invalid_response")
    length = math.hypot(*raw)
    if not math.isfinite(length) or length == 0:
        raise EmbeddingFailure("embedding_invalid_response")
    return tuple(float(v / length) for v in raw)


class CohereEmbeddings:
    def __init__(
        self, settings: EmbeddingSettings, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings, self.transport = settings, transport
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(
        self, texts: list[str], kind: Literal["document", "query"]
    ) -> tuple[tuple[float, ...], ...]:
        if not 1 <= len(texts) <= 64 or any(not t.strip() or len(t) > 20000 for t in texts):
            raise EmbeddingFailure("embedding_input_limit")
        payload = {
            "model": DOCUMENT_MODEL if kind == "document" else QUERY_MODEL,
            "input_type": "search_document" if kind == "document" else "search_query",
            "texts": texts,
            "embedding_types": ["float"],
            "output_dimension": DIMENSIONS,
            "truncate": "NONE",
        }
        try:
            async with asyncio.timeout(20 if kind == "document" else 2):
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        transport=self.transport,
                        follow_redirects=False,
                        timeout=15,
                        limits=httpx.Limits(
                            max_connections=16, max_keepalive_connections=8, keepalive_expiry=120
                        ),
                    )
                async with self._client.stream(
                    "POST",
                    "https://api.cohere.com/v2/embed",
                    json=payload,
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                ) as response:
                    if response.status_code != 200:
                        code = (
                            "embedding_rate_limited"
                            if response.status_code == 429
                            else "embedding_provider_unavailable"
                        )
                        raise EmbeddingFailure(code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise EmbeddingFailure("embedding_invalid_response")
            values = json.loads(body)["embeddings"]["float"]
            if not isinstance(values, list) or len(values) != len(texts):
                raise EmbeddingFailure("embedding_invalid_response")
            return tuple(normalize(vector) for vector in values)
        except EmbeddingFailure:
            raise
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
            raise EmbeddingFailure("embedding_provider_unavailable") from None
