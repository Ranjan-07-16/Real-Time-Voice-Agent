"""Realtime LiveKit milestone: worker.py's half of resolving an Agent for a session.

    app/main.py's /api/livekit/token route resolves+authorizes an agent and puts its brief in the
    room's metadata -> this job's ctx.job.room.metadata -> _resolve_realtime_agent -> _RealtimeAgent

Uses only Python's stdlib unittest - same approach as test_deepgram_stt_retry.py. No network, no
LiveKit connection: ctx.job.room.metadata is faked directly with a minimal stand-in object, since
_resolve_realtime_agent only ever reads that one attribute.

Run with:
    cd backend && .venv/bin/python -m unittest livekit_agent.testing.test_realtime_agent -v
"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from livekit_agent import worker


@dataclass
class _FakeRoom:
    metadata: str


@dataclass
class _FakeJob:
    room: _FakeRoom | None


@dataclass
class _FakeCtx:
    job: _FakeJob


def ctx_with_metadata(metadata: str | None) -> _FakeCtx:
    return _FakeCtx(job=_FakeJob(room=_FakeRoom(metadata=metadata) if metadata is not None else None))


class ResolveRealtimeAgentTests(unittest.TestCase):
    # --- F: no agent selected falls back to the existing default/test behavior ---------------------

    def test_empty_metadata_resolves_to_none(self) -> None:
        self.assertIsNone(worker._resolve_realtime_agent(ctx_with_metadata("")))

    def test_no_room_resolves_to_none(self) -> None:
        self.assertIsNone(worker._resolve_realtime_agent(ctx_with_metadata(None)))

    def test_invalid_json_resolves_to_none_not_an_exception(self) -> None:
        self.assertIsNone(worker._resolve_realtime_agent(ctx_with_metadata("not json{")))

    def test_json_that_is_not_an_object_resolves_to_none(self) -> None:
        self.assertIsNone(worker._resolve_realtime_agent(ctx_with_metadata("[1, 2, 3]")))

    def test_missing_brief_resolves_to_none(self) -> None:
        metadata = json.dumps({"agent_id": 7, "agent_name": "Care Assistant"})
        self.assertIsNone(worker._resolve_realtime_agent(ctx_with_metadata(metadata)))

    def test_wrong_typed_fields_resolve_to_none(self) -> None:
        metadata = json.dumps({"agent_id": "not-an-int", "agent_name": "Care Assistant", "brief": "Be helpful."})
        self.assertIsNone(worker._resolve_realtime_agent(ctx_with_metadata(metadata)))

    # --- A: agent configuration is correctly resolved -----------------------------------------------

    def test_a_valid_agent_brief_resolves_to_a_realtime_agent(self) -> None:
        metadata = json.dumps(
            {"agent_id": 7, "agent_name": "Care Assistant", "voice": "aura-luna-en", "brief": "Be calm and brief."}
        )

        resolved = worker._resolve_realtime_agent(ctx_with_metadata(metadata))

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, 7)
        self.assertEqual(resolved.name, "Care Assistant")
        self.assertEqual(resolved.instruction, "Be calm and brief.")
        self.assertEqual(resolved.voice, "aura-luna-en")

    def test_voice_is_optional(self) -> None:
        metadata = json.dumps({"agent_id": 3, "agent_name": "Support Assistant", "voice": None, "brief": "Help."})

        resolved = worker._resolve_realtime_agent(ctx_with_metadata(metadata))

        self.assertIsNotNone(resolved)
        self.assertIsNone(resolved.voice)

    # --- D/E: two different agents resolve to two different instructions ----------------------------

    def test_two_different_agent_briefs_resolve_to_different_instructions(self) -> None:
        care = worker._resolve_realtime_agent(
            ctx_with_metadata(json.dumps({"agent_id": 1, "agent_name": "Care", "brief": "Be calm."}))
        )
        collections = worker._resolve_realtime_agent(
            ctx_with_metadata(json.dumps({"agent_id": 2, "agent_name": "Collections", "brief": "Be firm."}))
        )

        self.assertNotEqual(care.instruction, collections.instruction)
        self.assertNotEqual(care.id, collections.id)


if __name__ == "__main__":
    unittest.main()
