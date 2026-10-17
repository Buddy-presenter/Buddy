"""Explicit synthetic contract fixtures, never production/provider/validator results."""

import json
from pathlib import Path
from uuid import UUID, uuid4

from buddy_api.analysis.contracts import AnalysisInput
from buddy_api.extraction.contracts import Extraction, Slide

FIXTURE = Path(__file__).resolve().parents[3] / "docs/fixtures/analysis-v1.json"


def document(deck_id: UUID, extraction_id: UUID, job_id: UUID, *, actions=True) -> AnalysisInput:
    value = json.loads(FIXTURE.read_text())
    value.update(extraction_id=str(extraction_id), job_id=str(job_id))
    value["manifest"]["deck_id"] = str(deck_id)
    for slide in value["slides"]:
        if not actions:
            slide["actions"] = []
        for action in slide["actions"]:
            action["deck_id"] = str(deck_id)
    return AnalysisInput.model_validate(value)


async def prepare(repos, *, owner_id, deck_id, job_id=None, actions=True):
    job_id = job_id or (await repos.jobs.create(owner_id=owner_id, deck_id=deck_id)).id
    output = document(deck_id, uuid4(), job_id, actions=actions)
    extracted = Extraction(
        source_revision=output.source_revision,
        source_sha256=output.source_sha256,
        slides=tuple(
            Slide(
                id=s.slide_id,
                source_id=i + 1,
                index=i,
                width=s.source.width * 6350,
                height=s.source.height * 6350,
                text="Synthetic contract slide",
                notes=None,
                objects=(),
            )
            for i, s in enumerate(output.manifest.slides)
        ),
        warnings=(),
    )
    saved = await repos.extractions.save(owner_id=owner_id, deck_id=deck_id, extraction=extracted)
    return document(deck_id, saved.id, job_id, actions=actions)


async def create(repos, *, owner_id, deck_id, job_id=None, version=1, actions=False):
    output = await prepare(
        repos, owner_id=owner_id, deck_id=deck_id, job_id=job_id, actions=actions
    )
    return await repos.analyses.create(
        owner_id=owner_id, deck_id=deck_id, version=version, document=output
    )
