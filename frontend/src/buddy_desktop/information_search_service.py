"""Authenticated, typed requests for short web-grounded presenter answers."""

import http.client
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import AccessProvider
from buddy_desktop.services import NoRedirects, ServiceError

MAX_RESPONSE_BYTES = 16 * 1024


@dataclass(frozen=True)
class InformationSource:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class InformationAnswer:
    answer: str
    sources: tuple[InformationSource, ...]


class InformationSearchService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def answer(self, access: AccessProvider, query: str) -> InformationAnswer:
        request = Request(
            f"{self.config.api_url}/api/v1/web-search/answer",
            method="POST",
            data=json.dumps({"query": query}, ensure_ascii=False).encode(),
            headers={
                "Authorization": "Bearer " + access(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=10) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if (
                    len(raw) > MAX_RESPONSE_BYTES
                    or response.headers.get_content_type() != "application/json"
                ):
                    raise ValueError
                payload = json.loads(raw)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise ServiceError("Sign in again to search the web from Buddy.") from None
            if status == 429:
                raise ServiceError(
                    "Web search is temporarily rate limited; try again shortly."
                ) from None
            if status == 503:
                raise ServiceError(
                    "Web search is temporarily unavailable; try again shortly."
                ) from None
            raise ServiceError("Buddy could not complete that web search.") from None
        except (URLError, OSError, TimeoutError, http.client.HTTPException):
            raise ServiceError("Buddy could not reach web search; check your connection.") from None
        except (ValueError, UnicodeDecodeError, TypeError):
            raise ServiceError("Buddy received an invalid web search response.") from None

        try:
            answer = payload["answer"]
            raw_sources = payload["sources"]
            if (
                not isinstance(answer, str)
                or not answer.strip()
                or len(answer) > 1200
                or not isinstance(raw_sources, list)
                or len(raw_sources) > 4
            ):
                raise ValueError
            sources = tuple(self._source(item) for item in raw_sources)
            if not sources:
                raise ValueError
            return InformationAnswer(answer.strip(), sources)
        except (KeyError, TypeError, ValueError):
            raise ServiceError("Buddy received an invalid web search response.") from None

    @staticmethod
    def _source(value: object) -> InformationSource:
        if not isinstance(value, dict) or set(value) != {"title", "url", "snippet"}:
            raise ValueError
        title, url, snippet = value["title"], value["url"], value["snippet"]
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 180
            or not isinstance(url, str)
            or len(url) > 2048
            or not isinstance(snippet, str)
            or len(snippet) > 600
        ):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError
        return InformationSource(title.strip(), url, snippet.strip())
