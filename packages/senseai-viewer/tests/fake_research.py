# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""A stand-in for senseai-research (``/api/research/*``) for the Research page tests.

Canned data in the shapes the real server returns, and a scripted conversation: a question
gets a thinking summary, two tool chips, an answer and (for "optics") an Approve/Reject card.
It records the ``X-SenseAI-User`` header of every request.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

CLAIM = "lib:felidae.senses.vision.detection_threshold"
QUOTE = (
    "Expressed in retinal illuminance, after correcting for the larger pupil and ocular "
    "transmission, the cat's scotopic threshold was -0.08 log units re human."
)


def make_app() -> FastAPI:
    app = FastAPI()
    app.state.users = []
    app.state.decisions = []
    state: dict[str, Any] = {"claim_value": "-0.74 log10 re human", "revision": 0}

    @app.middleware("http")
    async def record(request: Request, call_next: Any) -> Any:
        app.state.users.append(request.headers.get("x-senseai-user"))
        return await call_next(request)

    @app.get("/api/research/start")
    def start() -> dict[str, Any]:
        return {
            "open_questions": [
                {
                    "hypothesis_id": "hyp:request:felidae-vision-q-1",
                    "statement": "Flicker-fusion frequency (temporal bandwidth).",
                    "priority": "critical",
                    "target": "felidae.senses.vision",
                    "read_by": ["panther.frame.gain_lut (vision_supply_vs_demand)"],
                }
            ],
            "least_verified": [
                {
                    "claim_id": CLAIM,
                    "library_path": "felidae.senses.vision.detection_threshold",
                    "value": state["claim_value"],
                    "status": "unverified",
                    "why": "CITATIONS.md says the source was checked as: abstract.",
                    "species": "Felis catus",
                    "read_by": ["panther.frame.gain_lut (vision_supply_vs_demand)"],
                }
            ],
        }

    @app.get("/api/research/claims/{claim_id:path}")
    def claim(claim_id: str) -> dict[str, Any]:
        history = [
            {
                "revision": 0,
                "at": "2026-10-17T10:00:00+00:00",
                "by": "pipeline:import@0.1.0",
                "summary": "Imported: -0.74 log10 re human (unverified)",
                "reason": "",
            },
        ]
        if state["revision"]:
            history.append(
                {
                    "revision": 1,
                    "at": "2026-10-17T10:05:00+00:00",
                    "by": "user:senseai",
                    "summary": (
                        "value -0.74 log10 re human → -0.08 log10 re human; "
                        "status unverified → extracted"
                    ),
                    "reason": "Optics removed for a camera.",
                }
            )
        return {
            "value": state["claim_value"],
            "history": history,
            "sources": [
                {
                    "source_id": "src:10.1152/jn.90641.2008",
                    "title": "Contrast sensitivity of cats and humans",
                    "authors": "Kang I, Reem RE, Kaczmarowski AL, Malpeli JG",
                    "year": 2009,
                    "venue": "J Neurophysiol",
                    "doi": "10.1152/jn.90641.2008",
                    "link": "https://doi.org/10.1152/jn.90641.2008",
                    "full_text": False,
                    "checked": "abstract",
                }
            ],
            "passage": None,
            "claim": {
                "id": claim_id,
                "library_path": "felidae.senses.vision.detection_threshold",
                "status": "extracted" if state["revision"] else "unverified",
                "species": "Felis catus",
                "conditions": "Behavioural, scotopic, low spatial frequencies.",
                "method": "behavioural",
                "mapping": None,
                "library_notes": "Most of the advantage is optics.",
                "notes_by_reviewer": "",
                "unverified_reason": None if state["revision"] else "Abstract only.",
                "revision": state["revision"],
            },
        }

    @app.post("/api/research/conversations")
    def new(x_senseai_user: str | None = Header(None)) -> dict[str, str]:
        if not x_senseai_user:
            raise HTTPException(401, "no user")
        return {"id": "conv-1"}

    @app.get("/api/research/conversations/{cid}")
    def conv(cid: str) -> dict[str, Any]:
        return {"id": cid, "items": []}

    def events(text: str) -> Iterator[bytes]:
        def ev(kind: str, data: dict[str, Any]) -> bytes:
            return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()

        yield ev("thinking_summary", {"text": "Look up the claim, then the paper."})
        yield ev(
            "tool_call",
            {"id": "t1", "name": "claim_get", "label": "Reading claim " + CLAIM, "kind": "read"},
        )
        yield ev(
            "tool_result_summary",
            {"id": "t1", "ok": True, "summary": "Read it.", "refs": {"claim_id": CLAIM}},
        )
        yield ev(
            "tool_call",
            {
                "id": "t2",
                "name": "source_passage",
                "label": "Looking up the passage",
                "kind": "read",
            },
        )
        yield ev(
            "tool_result_summary",
            {
                "id": "t2",
                "ok": True,
                "summary": "Found the passage in Results",
                "refs": {
                    "source_id": "src:x",
                    "doi": "10.1152/jn.90641.2008",
                    "section": "Results",
                    "text": QUOTE,
                    "context_before": "Pupil area was measured. ",
                    "context_after": " Most of it is optics.",
                },
            },
        )
        for chunk in (
            "The paper says the advantage is mostly optics. ",
            "With optics removed the value is -0.08.",
        ):
            yield ev("text_delta", {"text": chunk})
        if "optics" in text.lower():
            yield ev(
                "pending_action",
                {
                    "action": {
                        "id": "act-1",
                        "kind": "claim_revision",
                        "status": "pending",
                        "card": {
                            "title": "Change felidae.senses.vision.detection_threshold",
                            "summary": "For a camera the lens replaces the pupil.",
                            "rows": [
                                {
                                    "label": "Value",
                                    "before": "-0.74 log10 re human",
                                    "after": "-0.08 log10 re human",
                                }
                            ],
                            "quote": QUOTE,
                            "note": "Nothing changes until you approve.",
                        },
                    }
                },
            )
            yield ev(
                "tool_call",
                {
                    "id": "t3",
                    "name": "experiment_run",
                    "label": "Running the bench",
                    "kind": "compute",
                },
            )
            yield ev(
                "job_progress",
                {"job_id": "exp-1", "message": "patched: running panther_prior", "frac": 0.5},
            )
            yield ev(
                "tool_result_summary",
                {
                    "id": "t3",
                    "ok": True,
                    "summary": "Experiment exp-1: done",
                    "refs": {"experiment_id": "exp-1"},
                },
            )
        yield ev("done", {"stop": "end_turn", "pending": [], "usage": {}})

    @app.post("/api/research/conversations/{cid}/messages")
    async def messages(cid: str, request: Request) -> StreamingResponse:
        body = await request.json()
        return StreamingResponse(events(body["text"]), media_type="text/event-stream")

    @app.post("/api/research/actions/{aid}/{decision}")
    async def decide(
        aid: str, decision: str, request: Request, x_senseai_user: str | None = Header(None)
    ) -> dict[str, Any]:
        body = await request.json()
        app.state.decisions.append((aid, decision, body["rationale"], x_senseai_user))
        if decision == "approve":
            state.update(claim_value="-0.08 log10 re human", revision=1)
        return {
            "message": f"{x_senseai_user} {decision}d it. Their reason: {body['rationale']}",
            "action": {
                "id": aid,
                "kind": "claim_revision",
                "status": f"{decision}d" if decision == "approve" else "rejected",
                "card": {},
                "result": {"claim_id": CLAIM},
            },
        }

    @app.get("/api/research/experiments/{eid}")
    def experiment(eid: str) -> dict[str, Any]:
        return {
            "experiment": {
                "id": eid,
                "status": "done",
                "sequences": ["zurich_city_03_a", "zurich_city_09_b"],
                "arms": ["panther_prior", "null_profile"],
                "error": None,
                "profiles_sha": "018d2d6",
                "bench_sha": "d7c963a",
                "world_sha": "8ec2cfd",
            },
            "comparison": [
                {
                    "arm": "panther_prior",
                    "objects": 24,
                    "judged": True,
                    "verdict": "",
                    "baseline_hit_rate": 0.5,
                    "baseline_chance_hit_rate": 0.49,
                    "patched_hit_rate": 0.667,
                    "patched_chance_hit_rate": 0.64,
                    "baseline_median_delay_s": 0.3,
                    "patched_median_delay_s": 0.3,
                    "baseline_fires_per_h": 120.0,
                    "patched_fires_per_h": 120.0,
                    "baseline_latency_p99_ms": 2.0,
                    "patched_latency_p99_ms": 2.0,
                },
                {
                    "arm": "null_profile",
                    "objects": 5,
                    "judged": False,
                    "verdict": "too few objects to judge",
                    "baseline_hit_rate": None,
                    "baseline_chance_hit_rate": None,
                    "patched_hit_rate": None,
                    "patched_chance_hit_rate": None,
                    "baseline_median_delay_s": 0.5,
                    "patched_median_delay_s": 0.5,
                    "baseline_fires_per_h": 90.0,
                    "patched_fires_per_h": 90.0,
                    "baseline_latency_p99_ms": 2.0,
                    "patched_latency_p99_ms": 2.0,
                },
            ],
            "runlogs": [],
            "progress": None,
        }

    return app
