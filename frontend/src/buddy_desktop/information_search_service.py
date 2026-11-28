"""Authenticated, typed requests for web answers and images shown in the presenter panel."""

import http.client
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from buddy_desktop.config import AppConfig
from buddy_desktop.library_service import AccessProvider
from buddy_desktop.services import NoRedirects, ServiceError

MAX_RESPONSE_BYTES = 32 * 1024
MAX_SOURCES = 4
MAX_IMAGES = 9
# Thumbnails only: small, bounded downloads that never block the written answer.
MAX_THUMBNAIL_BYTES = 512 * 1024
THUMBNAIL_TIMEOUT = 4
THUMBNAIL_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@dataclass(frozen=True)
class InformationSource:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class InformationImage:
    title: str
    thumbnail: str
    source_url: str
    source: str
    # Downloaded thumbnail bytes; None when the download failed (the tile is skipped).
    data: bytes | None = None


@dataclass(frozen=True)
class InformationAnswer:
    answer: str | None
    sources: tuple[InformationSource, ...]
    images: tuple[InformationImage, ...] = ()


def _https(url: object, limit: int = 2048) -> str:
    if not isinstance(url, str) or len(url) > limit:
        raise ValueError
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError
    return url


def _search_base(config: AppConfig) -> str:
    """The API serving web search: the app's API, or a local developer search server.

    `BUDDY_WEB_SEARCH_URL` lets a developer test search changes against a local server
    while everything else keeps using the configured Buddy API. Only HTTPS or a loopback
    HTTP address is accepted.
    """
    override = os.environ.get("BUDDY_WEB_SEARCH_URL", "").rstrip("/")
    if override:
        parts = urlsplit(override)
        loopback = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
        if (parts.scheme == "https" or loopback) and parts.hostname and not parts.username:
            return override
    return config.api_url


class InformationSearchService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def answer(
        self,
        access: AccessProvider,
        query: str,
        *,
        images: bool = False,
        spoken_context: str | None = None,
    ) -> InformationAnswer:
        """Search for ``query``; with ``spoken_context`` (the sentence before, possibly
        empty) ``query`` is a spoken sentence and the server writes the search."""
        mode = "images" if images else "web"
        body: dict[str, object] = {"query": query[-300:], "mode": mode}
        if spoken_context is not None:
            body["spoken"] = True
            if spoken_context:
                body["context"] = spoken_context[-600:]
        request = Request(
            f"{_search_base(self.config)}/api/v1/web-search/answer",
            method="POST",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={
                "Authorization": "Bearer " + access(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=20) as response:
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
                    "Web search found nothing usable or is unavailable; try again shortly."
                ) from None
            raise ServiceError("Buddy could not complete that web search.") from None
        except (URLError, OSError, TimeoutError, http.client.HTTPException):
            raise ServiceError("Buddy could not reach web search; check your connection.") from None
        except (ValueError, UnicodeDecodeError, TypeError):
            raise ServiceError("Buddy received an invalid web search response.") from None

        try:
            answer = payload.get("answer")
            raw_sources = payload.get("sources", [])
            raw_images = payload.get("images", [])
            if (
                (answer is not None and (not isinstance(answer, str) or not answer.strip()))
                or (isinstance(answer, str) and len(answer) > 1200)
                or not isinstance(raw_sources, list)
                or len(raw_sources) > MAX_SOURCES
                or not isinstance(raw_images, list)
                or len(raw_images) > MAX_IMAGES
            ):
                raise ValueError
            sources = tuple(self._source(item) for item in raw_sources)
            found = tuple(self._image(item) for item in raw_images)
            if images and not found:
                raise ValueError
            if not images and (answer is None or not sources):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ServiceError("Buddy received an invalid web search response.") from None
        return InformationAnswer(
            answer.strip() if isinstance(answer, str) else None,
            sources,
            self._with_thumbnails(found),
        )

    @staticmethod
    def _source(value: object) -> InformationSource:
        if not isinstance(value, dict) or set(value) != {"title", "url", "snippet"}:
            raise ValueError
        title, url, snippet = value["title"], value["url"], value["snippet"]
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 180
            or not isinstance(snippet, str)
            or len(snippet) > 600
        ):
            raise ValueError
        return InformationSource(title.strip(), _https(url), snippet.strip())

    @staticmethod
    def _image(value: object) -> InformationImage:
        if not isinstance(value, dict) or set(value) != {
            "title",
            "thumbnail",
            "source_url",
            "source",
        }:
            raise ValueError
        title, source = value["title"], value["source"]
        if (
            not isinstance(title, str)
            or len(title) > 180
            or not isinstance(source, str)
            or len(source) > 120
        ):
            raise ValueError
        return InformationImage(
            title.strip(), _https(value["thumbnail"]), _https(value["source_url"]), source.strip()
        )

    @staticmethod
    def _download(url: str) -> bytes | None:
        """Fetch one thumbnail: HTTPS only, no redirects, image types, bounded size."""
        try:
            request = Request(url, headers={"Accept": "image/*", "User-Agent": "BuddyDesktop"})
            with build_opener(NoRedirects()).open(request, timeout=THUMBNAIL_TIMEOUT) as response:
                if response.headers.get_content_type() not in THUMBNAIL_TYPES:
                    return None
                data = response.read(MAX_THUMBNAIL_BYTES + 1)
                return data if 0 < len(data) <= MAX_THUMBNAIL_BYTES else None
        except (HTTPError, URLError, OSError, TimeoutError, http.client.HTTPException, ValueError):
            return None

    @classmethod
    def _with_thumbnails(cls, images: tuple[InformationImage, ...]) -> tuple[InformationImage, ...]:
        if not images:
            return ()
        with ThreadPoolExecutor(max_workers=min(6, len(images))) as pool:
            data = list(pool.map(cls._download, (image.thumbnail for image in images)))
        return tuple(
            InformationImage(i.title, i.thumbnail, i.source_url, i.source, d)
            for i, d in zip(images, data, strict=True)
            if d is not None
        )
