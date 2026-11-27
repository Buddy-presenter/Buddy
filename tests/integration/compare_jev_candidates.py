"""Opt-in paired real-provider evaluation; never talks to the shared staging API/DB.

Uses AWS only for approved provider keys and private synthetic source/evidence storage.
Identity is replaced only at the loopback test boundary. SQL, API gates, retrieval,
Jev selection, and optional native validation are real. No paid calls occur without --live.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import sys
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import UUID, uuid4

import boto3
from backend.tests.support.database import migrate
from fastapi.testclient import TestClient
from sqlalchemy.schema import CreateSchema, DropSchema
from tests.integration.jev_comparison_fixtures import cases, document, sources
from tests.integration.jev_comparison_metrics import correct, summarize
from tests.integration.jev_native_execution import NativeExecution

from buddy_api.agent_provider import AgentSettings
from buddy_api.app import create_app, decision_providers
from buddy_api.auth import Principal, authenticated
from buddy_api.config import Environment, Settings
from buddy_api.embeddings import EmbeddingSettings
from buddy_api.extraction.runner import extract_pdf, extract_pptx
from buddy_api.persistence.database import Database
from buddy_api.persistence.engine import create_engine
from buddy_api.persistence.settings import DatabaseSettings
from buddy_api.preflight.config import PreflightSettings
from buddy_api.preflight.contracts import PreflightRequest
from buddy_api.preflight.runner import run_preflight
from buddy_api.preflight.service import PreflightService
from buddy_api.presentation_agent import AgentRuntime
from buddy_api.uploads.config import UploadSettings
from buddy_api.uploads.contracts import PDF_TYPE, PPTX_TYPE
from buddy_api.uploads.storage import SourceStorage


def secret(client, name):
    value = client.get_secret_value(SecretId=name)["SecretString"].strip()
    if value.startswith("{"):
        item = json.loads(value)
        value = next(item[k] for k in ("api_key", "key", "token") if k in item)
    return value


async def idle_worker(self):
    await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", required=True, action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="buddy-staging-tools")
    parser.add_argument("--bucket", default="buddy-staging-549327541867-data")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--native-validation", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute each paired case in real players on repeat 1",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--case-ids", help="Comma-separated frozen case IDs")
    parser.add_argument("--audio-replay", action="store_true")
    parser.add_argument("--also-audio", action="store_true", help="Add eight paired speech replays")
    parser.add_argument(
        "--speech-only", action="store_true", help="Measure ASR without selecting actions"
    )
    parser.add_argument(
        "--pair-interval",
        type=float,
        default=4,
        help="Minimum seconds between cases, outside measured request latency",
    )
    parser.add_argument(
        "--preserve-on-error",
        action="store_true",
        help="Keep this synthetic schema/S3 evidence for recovery if interrupted",
    )
    args = parser.parse_args()
    if args.speech_only and not (args.audio_replay or args.also_audio):
        parser.error("--speech-only requires --audio-replay or --also-audio")
    os.environ["AWS_PROFILE"] = args.profile
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.Round)
    gui = QApplication([])
    screen = gui.primaryScreen()
    if screen is None or sys.platform != "win32":
        parser.error("A real Windows/Office desktop is required for independent format validation")
    scale = screen.devicePixelRatio()
    pdf_profile = "wide-2x" if scale == 2 else "wide-1x"
    native_viewport = (
        round(screen.geometry().width() * scale),
        round(screen.geometry().height() * scale),
    )
    args.output.mkdir(parents=True, exist_ok=False)
    folder = args.output.resolve()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    admin = DatabaseSettings(os.environ["BUDDY_TEST_DATABASE_URL"])
    if not admin.connection_url().database.endswith("_test"):
        parser.error("A disposable *_test database is required")
    schema = "buddy_jev_" + uuid4().hex
    database_settings = replace(admin, schema=schema)
    cloud = boto3.Session(profile_name=args.profile, region_name=args.region)
    secrets = cloud.client("secretsmanager")
    jev_key = secret(secrets, "buddy-staging/agent/jev")
    cohere_key = secret(secrets, "buddy-staging/embeddings/cohere")
    s3 = cloud.client("s3")
    kms = s3.get_bucket_encryption(Bucket=args.bucket)["ServerSideEncryptionConfiguration"][
        "Rules"
    ][0]["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"]
    upload_settings = UploadSettings(bucket=args.bucket, region=args.region, kms_key=kms)
    storage = SourceStorage(upload_settings, s3)
    paths = sources(folder / "sources")
    if args.speech_only:
        # ASR is format-independent; use a real PDF session without claiming Office readiness.
        paths = {"pdf": paths["pdf"]}
    corpus = cases()
    if args.case_ids:
        selected = set(args.case_ids.split(","))
        corpus = [case for case in corpus if case["id"] in selected]
        if len(corpus) != len(selected):
            parser.error("Unknown or repeated case IDs")
    if args.limit:
        corpus = corpus[: args.limit]
    corpus_raw = json.dumps(corpus, sort_keys=True).encode()
    (folder / "cases.json").write_bytes(corpus_raw)
    owner, receipt, fixtures, records = None, [], {}, []
    succeeded = False
    native_player = None
    db = Database(database_settings)

    async def setup():
        engine = create_engine(admin)
        try:
            async with engine.begin() as connection:
                await connection.execute(CreateSchema(schema))
        finally:
            await engine.dispose()
        await migrate(database_settings)
        async with db.transaction() as repos:
            identifier = (await repos.users.create("synthetic-jev-comparison-" + uuid4().hex)).id
        await db.close()
        return identifier

    try:
        owner = asyncio.run(setup())
        config = Settings(
            Environment.TEST,
            ("testserver",),
            database=database_settings,
            uploads=upload_settings,
            embeddings=EmbeddingSettings(cohere_key),
            agent=AgentSettings(cohere_key, jev_api_key=jev_key, requests_per_minute=500),
            preflight=PreflightSettings(python=sys.executable, region=args.region),
        )
        app = create_app(config, upload_storage=storage)
        if args.audio_replay or args.also_audio:
            from buddy_api.speech import DeepgramProvider, SpeechSettings

            app.state.speech_provider = DeepgramProvider(
                SpeechSettings(secret(secrets, "buddy-staging/speech/deepgram"))
            )

        async def identity():
            return Principal(owner, "synthetic@example.test")

        async def speech_identity(request, authorization):
            return await identity()

        app.dependency_overrides[authenticated] = identity
        with (
            patch.object(PreflightService, "run", idle_worker),
            patch("buddy_api.speech.authenticated", speech_identity),
            TestClient(app) as client,
        ):
            client.headers["Authorization"] = "Bearer synthetic-evaluation"
            backend = app.state.database

            async def seed(kind, path):
                extracted = (extract_pdf if kind == "pdf" else extract_pptx)(
                    path, source_revision="paired-evaluation-v1"
                )
                async with backend.transaction() as repos:
                    deck = await repos.decks.create(
                        owner_id=owner, title="Synthetic Jev comparison " + kind, source_format=kind
                    )
                    job = await repos.jobs.create(owner_id=owner, deck_id=deck.id)
                    extraction = await repos.extractions.save(
                        owner_id=owner, deck_id=deck.id, extraction=extracted
                    )
                    doc = document(extracted, deck.id, job.id, extraction.id)
                    key = f"sources/{owner}/{deck.id}/original.{kind}"
                    version = await asyncio.to_thread(
                        storage.put,
                        key,
                        path,
                        size=path.stat().st_size,
                        sha256=extracted.source_sha256,
                        revision=extracted.source_revision,
                        content_type=PDF_TYPE if kind == "pdf" else PPTX_TYPE,
                    )
                    receipt.append(f"sources/{owner}/{deck.id}/")
                    deck.source_key, deck.source_version_id = key, version
                    deck.source_revision, deck.source_sha256 = (
                        extracted.source_revision,
                        extracted.source_sha256,
                    )
                    deck.source_size_bytes, deck.source_slide_count = (
                        path.stat().st_size,
                        len(extracted.slides),
                    )
                    saved = await repos.analyses.create(
                        owner_id=owner, deck_id=deck.id, version=1, document=doc
                    )
                    job.status, job.stage_state, job.progress = "analysis_partial", "succeeded", 100
                    job.source_revision = extracted.source_revision
                    job.extraction_id = extraction.id
                    return deck.id, saved.id, doc

            for kind, path in paths.items():
                deck, analysis_id, doc = client.portal.call(seed, kind, path)
                fixtures[kind] = dict(deck=deck, analysis_id=analysis_id, document=doc, path=path)
                if not args.native_validation and not args.speech_only:
                    raise RuntimeError(
                        "Native validation is required before claiming eligible actions"
                    )
                for slide in doc.slides:
                    for action in slide.actions:
                        if args.speech_only:
                            # ASR-only mode makes no action eligibility/execution claim.
                            continue
                        if (
                            (args.limit or args.case_ids)
                            and action.instance_id
                            not in {c["expected"].get("action_id") for c in corpus}
                            and not any(c["focus"] != "slide" for c in corpus)
                        ):
                            continue
                        profile = pdf_profile
                        request = PreflightRequest(
                            instance_id=action.instance_id,
                            profile=profile,
                            viewport=(1280, 720) if kind == "pdf" else native_viewport,
                            correct=False,
                        )
                        check = client.portal.call(
                            app.state.preflight.request, owner, analysis_id, uuid4(), request
                        )
                        manifest = doc.manifest.model_dump(mode="json")
                        snapshot = dict(
                            owner=str(owner),
                            source_format=kind,
                            source_sha256=doc.source_sha256,
                            revision=doc.source_revision,
                            deck_id=str(deck),
                            slides={
                                s["slide_id"]: dict(
                                    number=i + 1,
                                    native_id=s["native_slide_id"],
                                    width=s["source"]["width"],
                                    height=s["source"]["height"],
                                )
                                if kind == "pptx"
                                else {}
                                for i, s in enumerate(manifest["slides"])
                            },
                            action=action.model_dump(mode="json"),
                            model_id=config.preflight.model,
                            profile=profile,
                            viewport=request.viewport,
                            correct=False,
                        )
                        for attempt in range(3):
                            bundle = run_preflight(
                                path.read_bytes(),
                                snapshot,
                                config.preflight,
                                Event(),
                                lambda attempt: None,
                            )
                            with (folder / "validation-attempts.jsonl").open("a") as log:
                                log.write(
                                    json.dumps(
                                        dict(
                                            source=kind,
                                            attempt=attempt + 1,
                                            action=action.instance_id,
                                            report=bundle.report,
                                        )
                                    )
                                    + "\n"
                                )
                            preflight = bundle.report.get("preflight", {})
                            if not (
                                bundle.report["state"] == "unavailable"
                                and bundle.report.get("reason") == "execution_failed"
                                and preflight.get("failed_tool_index") == 1
                            ):
                                break
                            time.sleep(1)
                        client.portal.call(app.state.preflight.publish, check.id, bundle)
                        print(
                            json.dumps(
                                dict(
                                    stage="native_validation",
                                    source=kind,
                                    action=action.instance_id,
                                    state=bundle.report["state"],
                                    reason=bundle.report.get("reason"),
                                )
                            ),
                            flush=True,
                        )
                        if bundle.report["state"] != "validated":
                            (folder / f"failed-{kind}-{action.instance_id}.json").write_text(
                                json.dumps(bundle.report, indent=2)
                            )
                            raise RuntimeError(
                                "Native validation did not establish a usable fixture"
                            )
                session_response = client.post(
                    "/api/v1/sessions",
                    json=dict(
                        deck_id=str(deck),
                        runtime=dict(
                            platform="win32",
                            renderer_version=doc.renderer_version,
                            display_profile=pdf_profile,
                        ),
                    ),
                )
                assert session_response.status_code == 200, session_response.text
                session = session_response.json()["data"]
                autonomous = client.patch(
                    f"/api/v1/sessions/{session['id']}/state",
                    json={
                        **session["state"],
                        "autonomy_mode": "autonomous",
                        "expected_state_version": session["state_version"],
                    },
                )
                assert autonomous.status_code == 200, autonomous.text
                fixtures[kind]["session"] = autonomous.json()["data"]
            semantic = app.state.semantic_candidates
            baseline = app.state.presentation_agent
            direct_settings = replace(config.agent, api_key="", candidate_mode="direct_current")
            direct = AgentRuntime(direct_settings, *decision_providers(direct_settings, None))
            embedding_calls = []
            embed = semantic.provider.embed

            async def measured_embed(texts, kind):
                started = time.perf_counter()
                try:
                    return await embed(texts, kind)
                finally:
                    embedding_calls.append(
                        dict(kind=kind, ms=(time.perf_counter() - started) * 1000)
                    )

            semantic.provider.embed = measured_embed
            (folder / "checkpoint.json").write_text(
                json.dumps(
                    dict(
                        schema=schema,
                        owner=str(owner),
                        prefixes=receipt,
                        profile=pdf_profile,
                        viewport=native_viewport,
                        fixtures={
                            kind: dict(
                                deck=str(f["deck"]),
                                analysis_id=str(f["analysis_id"]),
                                path=str(f["path"].resolve()),
                                session=f["session"],
                                document=f["document"].model_dump(mode="json"),
                            )
                            for kind, f in fixtures.items()
                        },
                    ),
                    indent=2,
                )
            )
            audio_cases = {}
            if args.audio_replay or args.also_audio:
                from tests.integration.jev_speech_replay import replay

                for case in corpus:
                    if args.also_audio and case["id"] not in {
                        "case-001",
                        "case-002",
                        "case-017",
                        "case-020",
                        "case-026",
                        "case-028",
                        "case-036",
                        "case-038",
                    }:
                        continue
                    audio_cases[case["id"]] = replay(
                        client, fixtures["pdf"]["session"], case, folder / "speech"
                    )
                    print(json.dumps(dict(stage="speech_replay", case=case["id"])), flush=True)
                (folder / "speech.json").write_text(json.dumps(audio_cases, indent=2))
                if args.speech_only:
                    print(
                        json.dumps(dict(stage="speech_complete", cases=len(audio_cases))),
                        flush=True,
                    )
                    succeeded = True
                    return
                if args.also_audio:
                    corpus += [
                        {
                            **case,
                            "id": case["id"] + "-audio",
                            "input_mode": "speech",
                            "text": audio_cases[case["id"]]["text"],
                            "earlier": [],
                        }
                        for case in list(corpus)
                        if case["id"] in audio_cases
                    ]
            for kind, fixture in fixtures.items():
                session = fixture["session"]
                baseline.settings = replace(
                    config.agent,
                    diagnostics_enabled=True,
                    diagnostics_session_id=UUID(session["id"]),
                )
                direct.settings = replace(
                    direct_settings,
                    diagnostics_enabled=True,
                    diagnostics_session_id=UUID(session["id"]),
                )
                app.state.presentation_agent, app.state.semantic_candidates = baseline, semantic
                started = time.perf_counter()
                response = client.post(f"/api/v1/sessions/{session['id']}/candidates/prepare")
                assert response.status_code == 200, response.text
                fixture["prepare_ms"] = (time.perf_counter() - started) * 1000
                identifiers = [s.slide_id for s in fixture["document"].slides]
                if args.execute:
                    native_player = NativeExecution(
                        gui,
                        fixture,
                        kind,
                        folder / ("execution-" + kind),
                        pdf_profile,
                        (1280, 720) if kind == "pdf" else native_viewport,
                    )
                for repeat in range(args.repeats):
                    ordered = list(corpus)
                    random.Random(252 + repeat).shuffle(ordered)
                    for index, case in enumerate(ordered):
                        pair_started = time.perf_counter()
                        if args.audio_replay:
                            case = {**case, "text": audio_cases[case["id"]]["text"], "earlier": []}
                        for mode in (
                            ("semantic", "direct_current")
                            if (index + repeat) % 2 == 0
                            else ("direct_current", "semantic")
                        ):
                            if (
                                native_player is not None
                                and repeat == 0
                                and case.get("input_mode", "text") == "text"
                            ):
                                native_player.prepare(case)
                            app.state.presentation_agent, app.state.semantic_candidates = (
                                (baseline, semantic) if mode == "semantic" else (direct, None)
                            )
                            segments = [*case["earlier"], case["text"]]
                            focus_region = (
                                fixture["document"]
                                .slides[case["current"]]
                                .actions[0]
                                .params.region.model_dump()
                                if case["focus"] != "slide"
                                else None
                            )
                            body = dict(
                                protocol="presentation-agent.v1",
                                expected_state_version=session["state_version"],
                                context_epoch=1,
                                stream_id=str(uuid4()),
                                sequence=len(segments),
                                sent_at=__import__("datetime")
                                .datetime.now(__import__("datetime").UTC)
                                .isoformat(),
                                text=" ".join(segments),
                                latest_utterance=case["text"],
                                segments=[
                                    dict(
                                        sequence=i + 1,
                                        text=text,
                                        final=not case["provisional"]
                                        if i == len(segments) - 1
                                        else True,
                                    )
                                    for i, text in enumerate(segments)
                                ],
                                active_slide_id=identifiers[case["current"]],
                                focus_view=case["focus"],
                                focus_region=focus_region,
                                provisional=case["provisional"],
                                decision_model="jev",
                            )
                            before = len(embedding_calls)
                            started = time.perf_counter()
                            response = client.post(
                                f"/api/v1/sessions/{session['id']}/agent", json=body
                            )
                            wall = (time.perf_counter() - started) * 1000
                            value = response.json()
                            diagnostics = value.get("diagnostics") or {}
                            target = value.get("target", {})
                            error = response.status_code != 200 or value.get("reason") in {
                                "disabled",
                                "deadline",
                                "stale",
                                "busy",
                                "budget",
                                "paced",
                                "provider_unavailable",
                                "rate_limited",
                                "invalid_output",
                                "candidates_unavailable",
                            }
                            row = dict(
                                case=case["id"],
                                source_format=kind,
                                mode=mode,
                                repeat=repeat,
                                category=case["category"],
                                input_mode=case.get(
                                    "input_mode", "speech" if args.audio_replay else "text"
                                ),
                                expected=case["expected"],
                                target=target,
                                correct=not error
                                and correct(case["expected"], target, identifiers),
                                error=value.get("reason", response.status_code) if error else None,
                                wall_ms=wall,
                                server_ms=value.get("elapsed_ms"),
                                phases=diagnostics.get("phase_timings_ms") or {},
                                option_count=len(diagnostics.get("candidate_options") or []),
                                route=diagnostics.get("route"),
                                decision_source=value.get("decision_source"),
                                embedding_calls=len(embedding_calls) - before,
                                diagnostics=diagnostics,
                                speech=audio_cases.get(case["id"].removesuffix("-audio")),
                            )
                            records.append(row)
                            if (
                                native_player is not None
                                and repeat == 0
                                and case.get("input_mode", "text") == "text"
                            ):
                                try:
                                    row["execution"] = native_player.execute(
                                        client, session, body, value, case["id"] + "-" + mode
                                    )
                                    row["decision_to_visible_ms"] = wall + row["execution"].get(
                                        "dispatch_to_visible_ms", 0
                                    )
                                except Exception as exc:
                                    row["execution"] = dict(
                                        completed=False, error=type(exc).__name__
                                    )
                            with (folder / "records.jsonl").open("a", encoding="utf-8") as output:
                                output.write(json.dumps(row) + "\n")
                        # Pacing respects account limits and is not request latency.
                        time.sleep(
                            max(0, args.pair_interval - (time.perf_counter() - pair_started))
                        )
                        print(
                            json.dumps(
                                dict(
                                    stage="comparison",
                                    source=kind,
                                    repeat=repeat + 1,
                                    completed=index + 1,
                                    total=len(corpus),
                                )
                            ),
                            flush=True,
                        )
                    if native_player is not None:
                        native_player.close()
                        native_player = None
                app.state.presentation_agent, app.state.semantic_candidates = baseline, semantic
            client.portal.call(direct.aclose)
            summary = dict(
                schema="buddy.jev-comparison.v1",
                corpus_sha256=hashlib.sha256(corpus_raw).hexdigest(),
                cases=len(corpus),
                repeats=args.repeats,
                groups=summarize(records),
                text_groups=summarize([r for r in records if r["input_mode"] == "text"]),
                speech_groups=summarize([r for r in records if r["input_mode"] == "speech"]),
                prepare_ms={k: round(v["prepare_ms"], 2) for k, v in fixtures.items()},
                scope=(
                    "Synthetic inputs; real local API/PostgreSQL, AWS S3/Bedrock/native QA, "
                    "Cohere embeddings and Jev. Microphone and deployment not measured."
                ),
            )
            (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary), flush=True)
            succeeded = True
    finally:
        if native_player is not None:
            native_player.close()
        if not succeeded and args.preserve_on_error:
            storage.close()
            print(
                json.dumps(dict(stage="recovery_preserved", schema=schema, prefixes=receipt)),
                flush=True,
            )
        else:
            cleanup_run(cloud, upload_settings, owner, receipt, admin, schema, folder, storage)


def cleanup_run(cloud, upload_settings, owner, receipt, admin, schema, folder, storage):
    cleanup_errors = []
    cleanup_storage = SourceStorage(upload_settings, cloud.client("s3"))
    try:
        for prefix in receipt:
            if owner is None or not prefix.startswith(f"sources/{owner}/"):
                raise AssertionError("Cleanup escaped the synthetic owner's scope")
            try:
                cleanup_storage.remove_prefix(prefix)
            except Exception as exc:
                cleanup_errors.append(dict(prefix=prefix, error=type(exc).__name__))
    finally:
        cleanup_storage.close()

    async def cleanup():
        engine = create_engine(admin)
        try:
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        finally:
            await engine.dispose()

    asyncio.run(cleanup())
    storage.close()
    (folder / "cleanup.json").write_text(
        json.dumps(dict(schema_dropped=schema, prefixes=receipt, errors=cleanup_errors), indent=2)
    )
    if cleanup_errors:
        raise RuntimeError("Synthetic S3 cleanup incomplete; inspect cleanup.json")


if __name__ == "__main__":
    raise SystemExit(main())
