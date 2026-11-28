"""The slide on screen reaches the session once per slide change (#259)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from PySide6.QtGui import QColor, QImage

from buddy_desktop.config import AppConfig
from buddy_desktop.slide_image_service import SlideImageService, slide_jpeg


def picture(width, height):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    return image


def test_slides_become_jpegs_within_jev_limits(qapp):
    from io import BytesIO

    from PIL import Image

    jpeg = slide_jpeg(picture(2560, 1440))
    assert jpeg is not None and jpeg.startswith(b"\xff\xd8\xff")
    assert Image.open(BytesIO(jpeg)).size == (1280, 720)
    assert slide_jpeg(picture(400, 200)) is None  # Too small for Jev.
    assert slide_jpeg(QImage()) is None


def test_upload_puts_the_jpeg_for_the_session_and_slide(qapp):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(
                (self.path, self.headers["Authorization"], self.headers["Content-Type"], body)
            )
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = str(uuid4())
        service = SlideImageService(AppConfig(f"http://127.0.0.1:{server.server_port}"))
        assert service.upload(lambda: "token", session, "slide 3", b"\xff\xd8\xffjpeg")
        path, auth, kind, body = seen[0]
        assert path == f"/api/v1/sessions/{session}/slide-image?slide_id=slide%203"
        assert (auth, kind, body) == ("Bearer token", "image/jpeg", b"\xff\xd8\xffjpeg")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert not SlideImageService(AppConfig("http://127.0.0.1:9")).upload(
        lambda: "token", session, "s", b"x"
    )
    json.dumps(seen[0][0])
