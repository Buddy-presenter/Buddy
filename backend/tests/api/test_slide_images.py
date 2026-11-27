"""The current slide image reaches Jev only for its own session, owner and slide (#259)."""

import io
import time
from uuid import uuid4

import pytest
from PIL import Image

from buddy_api import slide_images
from buddy_api.slide_images import SlideImage, SlideImages, checked_image


def encoded(size=(640, 360), kind="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, kind)
    return out.getvalue()


def test_only_still_png_or_jpeg_within_jev_limits_is_accepted():
    assert checked_image(encoded()) == "png"
    assert checked_image(encoded(kind="JPEG")) == "jpg"
    for bad in (encoded((200, 360)), encoded((1600, 900)), b"GIF89a" + b"0" * 40, b"\x89PNG"):
        with pytest.raises((ValueError, OSError)):
            checked_image(bad)


def test_store_returns_an_image_only_for_the_same_owner_and_slide(monkeypatch):
    store, session, owner = SlideImages(), uuid4(), uuid4()
    store.put(session, SlideImage(owner, "slide-3", "png", b"png", time.monotonic()))
    assert store.get(session, owner, "slide-3") is not None
    assert store.get(session, uuid4(), "slide-3") is None
    assert store.get(session, owner, "slide-4") is None
    assert store.get(uuid4(), owner, "slide-3") is None
    later = time.monotonic() + slide_images.TTL_SECONDS + 1
    monkeypatch.setattr(slide_images.time, "monotonic", lambda: later)
    assert store.get(session, owner, "slide-3") is None


def test_store_is_bounded(monkeypatch):
    monkeypatch.setattr(slide_images, "MAX_SESSIONS", 2)
    store, owner = SlideImages(), uuid4()
    sessions = [uuid4() for _ in range(3)]
    for index, session in enumerate(sessions):
        store.put(session, SlideImage(owner, "s", "png", b"x", time.monotonic() + index))
    assert store.get(sessions[0], owner, "s") is None
    assert store.get(sessions[2], owner, "s") is not None
