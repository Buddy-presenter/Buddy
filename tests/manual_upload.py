"""Manual staging upload acceptance using real browser login; never print session tokens."""

import argparse
import hashlib
import http.client
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from uuid import UUID, uuid4

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

from buddy_desktop.auth import AccountSession, NativeTokenStore
from buddy_desktop.config import validate_api_url
from buddy_desktop.native_callback import NativeCallbackServer
from buddy_desktop.services import NoRedirects
from buddy_desktop.tasks import TaskRunner
from buddy_identity.config import IdentityConfig, IdentityError

PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("infra/staging/public-config.json"))
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--file", type=Path)
    operation.add_argument("--check-other-upload", type=UUID)
    parser.add_argument("--check-other-deck", type=UUID)
    parser.add_argument("--key", type=UUID)
    parser.add_argument("--new-login", action="store_true")
    args = parser.parse_args()
    try:
        public = json.loads(args.config.read_text(encoding="utf-8-sig"))
        origin = validate_api_url(public["BUDDY_API_URL"])
        identity = IdentityConfig.from_environment(public)
        if identity is None:
            raise ValueError
        app = QApplication(sys.argv[:1])
        app.setOrganizationName("Buddy")
        app.setApplicationName("BuddyDesktop")
        callback = NativeCallbackServer(app)
        app.installEventFilter(callback)
        if not callback.start():
            print("Close Buddy and restart this manual check.")
            return 1
        directory = Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        )
        store = NativeTokenStore(
            origin + "|" + identity.issuer + "|" + identity.client_id, directory
        )
        session = AccountSession(identity, origin, store)
        runner = TaskRunner(app)

        def send(path, *, data=None, method="GET", headers=None):
            # Refresh/revalidate before each authenticated operation, without
            # persisting or printing access tokens or accepting URL redirects.
            session.account()
            if session.tokens is None:
                raise ValueError
            request = Request(
                origin + path,
                data=data,
                method=method,
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer " + session.tokens.access,
                    **(headers or {}),
                },
            )
            try:
                response = build_opener(NoRedirects()).open(request, timeout=180)
            except HTTPError as error:
                response = error
            with response:
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError
                return response.code, json.loads(body)

        def run_check():
            try:
                print("Close other Buddy windows before this test to avoid concurrent")
                print("session rotation.")
                print("Testing backend:", origin)
                if args.new_login:
                    session.sign_in()
                else:
                    try:
                        session.resume()
                    except IdentityError:
                        session.sign_in()

                if args.check_other_upload:
                    return check_other_upload()

                return upload_file()
            except IdentityError as error:
                print(str(error))
                return 1
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                print(
                    "Manual upload check could not complete; "
                    "verify login, file, connectivity and deployment."
                )
                return 1

        def check_other_upload():
            status, _ = send(f"/api/v1/uploads/{args.check_other_upload}")
            if status != 403:
                print(
                    "FAIL: expected another account's upload to return HTTP 403; received", status
                )
                return 1
            status, _ = send(
                f"/api/v1/uploads/{args.check_other_upload}/content",
                data=b"",
                method="PUT",
                headers={"Content-Type": PPTX_TYPE},
            )
            if status != 403:
                print("FAIL: another account's upload transfer was not denied; received", status)
                return 1
            if args.check_other_deck:
                status, _ = send(f"/api/v1/resources/decks/{args.check_other_deck}")
                if status != 403:
                    print(
                        "FAIL: expected another account's deck to return HTTP 403; received", status
                    )
                    return 1
            print("PASS: another account's private resources were denied.")
            return 0

        def upload_file():
            source = args.file.resolve(strict=True)
            size = source.stat().st_size
            if not source.is_file() or not 1 <= size <= 64 * 1024 * 1024:
                print("Choose a PPTX file between 1 byte and 64 MiB.")
                return 1
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            key = args.key or uuid4()
            print("Retry key:", key)
            metadata = {
                "filename": source.name,
                "title": source.stem,
                "size_bytes": size,
                "sha256": digest,
                "content_type": PPTX_TYPE,
            }
            status, result = send(
                "/api/v1/uploads",
                data=json.dumps(metadata).encode(),
                method="POST",
                headers={"Content-Type": "application/json", "Idempotency-Key": str(key)},
            )
            if status != 200:
                print("Upload reservation rejected with HTTP", status)
                return 1
            upload = result["data"]
            identifier = UUID(upload["id"])
            print("Upload ID:", identifier)
            if upload["status"] == "pending":
                with source.open("rb") as stream:
                    status, result = send(
                        f"/api/v1/uploads/{identifier}/content",
                        data=stream,
                        method="PUT",
                        headers={"Content-Type": PPTX_TYPE, "Content-Length": str(size)},
                    )
                if status != 200:
                    print(
                        "Transfer returned HTTP",
                        status,
                        "; check status and reuse the printed retry key.",
                    )
                    return 1
                upload = result["data"]
            if upload["status"] != "complete":
                print(
                    "Upload state:",
                    upload["status"],
                    "; repeat with the same retry key after recovery.",
                )
                return 1
            deck = upload["deck"]
            if (
                deck["sha256"] != digest
                or deck["size_bytes"] != size
                or deck["status"] != "uploaded"
            ):
                raise ValueError
            print("Deck ID:", UUID(deck["id"]))
            print(
                "PASS: private deck saved with matching source hash/size and awaiting processing."
            )
            return 0

        def succeeded(_identifier, result):
            app.exit(result if type(result) is int else 1)

        def failed(_identifier, _message):
            print(
                "Manual upload check could not complete; verify login, connectivity and deployment."
            )
            app.exit(1)

        runner.succeeded.connect(succeeded)
        runner.failed.connect(failed)
        runner.submit(run_check)
        try:
            return app.exec()
        finally:
            runner.wait_for_shutdown()
            callback.close()

    except IdentityError as error:
        print(
            str(error)
        )  # IdentityError messages are explicitly sanitized by the account boundary.
        return 1
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        print(
            "Manual upload check could not complete; "
            "verify login, file, connectivity and deployment."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
