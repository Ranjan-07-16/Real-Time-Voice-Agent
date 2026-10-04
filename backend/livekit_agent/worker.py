"""The LiveKit Agent worker: a separate process from `app.main`, run from its own environment
(`backend/.venv`, `backend/requirements-livekit.txt`). It imports nothing from `app.*` - not
`app.main`, `app.db`, `app.auth`, `app.session`, `app.telephony` or `app.brains` - so it can start,
and eventually be deployed, entirely independently of the FastAPI application (see
`backend/livekit_agent/config.py`'s own docstring for the same reasoning about its configuration).

This module is intentionally the smallest possible scaffold: it proves that a worker built from this
package can register with a LiveKit server and be handed a job, and nothing more. It still does not
touch tools, persistence, interruption/barge-in or a real multi-turn conversation loop - all of that
is later work. Audio/STT/LLM/TTS are now covered, minimally, by Steps 5A-5D below - each a disposable,
one-shot diagnostic, not production voice handling.

Step 4F adds one thing on top of the plain connect-log-and-wait round trip: after connecting to the
room, it runs exactly one deterministic `gemini_adk.ask(...)` call (Step 4E's verified
`Agent -> Runner -> DirectGeminiModel -> google-genai -> Gemini Developer API` path) as an integration
check, then falls back into the same wait-to-be-cancelled behavior as before. No audio, no per-turn
conversation loop, no persistence - this proves the control/reasoning path reaches Gemini from inside
a real LiveKit job, nothing more.

Step 5A adds a second, independent diagnostic alongside Test E, not instead of it: when a remote
participant (the browser test page's "Start Microphone Test" button) publishes an audio track, the
worker subscribes to it (the default `auto_subscribe=SUBSCRIBE_ALL` on `ctx.connect()` already
handles this - confirmed from `JobContext.connect`'s installed signature) and counts the audio frames
actually received via `rtc.AudioStream`, for a bounded window, then logs only the count. It never
reads, logs, or stores a frame's audio data - just that frames arrived and how many. No STT, TTS or
Gemini is reachable from this path; it is wired up independently of the Test E block above so a
failure in either never affects the other.

Step 5B adds a third, independent diagnostic: on the same `track_subscribed` event Step 5A already
handles, a *second, separate* `rtc.AudioStream` is opened on the same track (confirmed this is a
supported pattern - a track supports multiple concurrent readers, not just one) and its real PCM
audio is streamed into Deepgram's real-time STT API (see `deepgram_stt.py`), logging only the
resulting transcript text and metadata - never storing or persisting it. This is registered as its
own `_watch_for_stt`, not by editing `_watch_for_audio`, specifically so Step 5A's already-verified
frame-counting behavior is provably untouched by this addition. If `DEEPGRAM_API_KEY` is not
configured, this diagnostic logs one line and does nothing further - it does not block the room
connection, Test E, or the Step 5A frame count.

Step 5C connects Step 5B's output to Step 4F's path, still inside `_run_stt`: each time a *final*
(not interim) transcript arrives, its real text - never a hardcoded string - is passed as the user
message to the same proven `Agent -> Runner -> DirectGeminiModel -> google-genai -> Gemini Developer
API` path Test E already uses, via `gemini_adk.ask(text, model, instruction=_STT_LLM_INSTRUCTION)` -
the same function, a different instruction, `DirectGeminiModel` itself untouched. A same-text guard
prevents a duplicate Gemini call if the identical final transcript were ever reported twice; each
call is independent of the fixed 30s audio window (not cancelled if the window ends first), so a
reply that takes a few seconds still completes and gets logged.

Step 5D closes the loop: Gemini's real text response (never hardcoded) is sent to Deepgram's TTS
REST API (see `deepgram_tts.py`), and the resulting linear16 PCM is published back into the same
LiveKit room as a new local audio track, using the installed `rtc.AudioSource`/`rtc.LocalAudioTrack`/
`rtc.AudioFrame` APIs (confirmed from their installed signatures, not assumed) - so the browser can
subscribe to and play the agent's spoken reply. Buffered, not streamed: the complete utterance is
synthesized first, then pushed as a sequence of 20ms frames via `AudioSource.capture_frame` (which
itself awaits until queue space is available - no manual pacing needed), followed by
`wait_for_playout()` before the diagnostic considers the reply "ready". One TTS call per final
transcript, governed by the same Step 5C dedup guard - no continuous synthesis loop, no interruption
handling, no echo-cancellation changes.

Two things worth knowing about the installed livekit-agents==1.8.0 API, verified against the installed
package itself, not assumed from another version's documentation:

  * `WorkerOptions.entrypoint_fnc` is `Callable[[JobContext], Awaitable[None]]`: an async function that
    receives the job's `JobContext` and runs for the life of that job. The framework cancels it when the
    job ends (the room is closed, the callee leaves, or the worker is asked to drain) - the entrypoint
    itself does not need to detect that on its own; it only needs to behave correctly when cancelled.

  * `livekit.agents.cli.run_app(server: AgentServer | WorkerOptions)` is what actually starts a worker
    from a script (`python myfile.py dev|start|console`). Its own docstring in this installed version
    calls it "the (deprecated) rich Python CLI", in favour of a newer mechanism: a module-level
    `AgentServer` instance (built with `AgentServer.from_server_options(WorkerOptions(...))` - confirmed
    `ServerOptions is WorkerOptions`, i.e. the same class under two names) discovered by
    `python -m livekit.agents <file> <command>`. `cli.run_app` is still fully functional in this version
    and is the one that supports running this file directly (`python -m livekit_agent.worker ...`), which
    is what this scaffold uses; migrating to the `AgentServer`/`python -m livekit.agents` form is worth
    doing before a future LiveKit release actually removes the older path, but is not needed for this
    milestone's purpose (proving the worker starts and registers).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli

from livekit_agent import deepgram_stt, deepgram_tts, gemini_adk
from livekit_agent.config import AgentConfig, MissingConfiguration, load, load_deepgram_stt, load_deepgram_tts

log = logging.getLogger("livekit_agent.worker")

_TEST_PROMPT = "Respond with exactly: LIVEKIT_ADK_TEST_OK"
_TEST_MARKER = "LIVEKIT_ADK_TEST_OK"

# Safety cap on the Step 5A frame-counting window and the Step 5B STT window. Widened from the
# original 30s diagnostic value for the frontend-integration milestone: a real voice session in the
# product UI needs more than 30 seconds to be usable, not a real turn-taking timeout, just the
# outer bound on one realtime job.
_AUDIO_TEST_WINDOW_SECONDS = 600

# Deepgram's real-time API accepts any sample rate; 16 kHz mono is its documented recommendation
# for speech (half the bandwidth of 48 kHz with no accuracy loss for a single speaker).
# `rtc.AudioStream`'s own `sample_rate`/`num_channels` arguments resample to this - confirmed from
# its installed docstring ("customizable sample rates and channel configurations").
_STT_SAMPLE_RATE = 16000

# Step 5C: a minimal, deterministic instruction for the ADK agent that answers the real STT
# transcript - deliberately different from Test E's echo-back instruction above.
_STT_LLM_INSTRUCTION = (
    "You are a realtime voice assistant test agent.\n"
    "Respond concisely to the user's message.\n"
    "Do not mention implementation details."
)

# Step 5D: the rate rtc.AudioSource publishes at and Deepgram TTS is asked to render at - matching
# the two avoids any resampling step. 48 kHz is also what WebRTC/LiveKit audio typically runs at.
_TTS_SAMPLE_RATE = 48000
_TTS_FRAME_MS = 20
_TTS_FRAME_BYTES = int(_TTS_SAMPLE_RATE * _TTS_FRAME_MS / 1000) * 2  # mono int16


@dataclass(frozen=True)
class _RealtimeAgent:
    """What a job's room metadata resolved to: a plain, per-job value computed once in entrypoint()
    and threaded through as an ordinary argument - never module-level state (every job must stay
    isolated from every other; see the Step 4F concurrency audit). None of this ever reaches a log
    line beyond id/name: `instruction` is the full system-prompt text, `voice` a Deepgram TTS model
    name - both configuration, not secrets, but still kept out of logs on general principle."""

    id: int
    name: str
    instruction: str
    voice: str | None


def _resolve_realtime_agent(ctx: JobContext) -> _RealtimeAgent | None:
    """Reads the agent brief the control plane (app/main.py's /api/livekit/token route) already
    resolved and authorized before issuing this room's token, carried here as the room's own
    metadata - never a raw agent_id trusted from the browser, and never a database lookup from this
    package (backend/livekit_agent/ imports nothing from app.*, see config.py's own docstring).
    Returns None - the existing default/test instruction (_STT_LLM_INSTRUCTION) - when no agent was
    selected, when the metadata is absent, or when it fails to parse; a malformed or missing agent
    brief must never block a realtime session, only fall back to the existing behavior."""
    raw = ctx.job.room.metadata if ctx.job.room else ""
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("realtime agent metadata was not valid JSON; using default instructions")
        return None

    if not isinstance(payload, dict):
        return None

    agent_id, name, brief = payload.get("agent_id"), payload.get("agent_name"), payload.get("brief")
    voice = payload.get("voice")

    if not isinstance(agent_id, int) or not isinstance(name, str) or not isinstance(brief, str) or not brief:
        return None

    return _RealtimeAgent(id=agent_id, name=name, instruction=brief, voice=voice if isinstance(voice, str) and voice else None)


async def _publish_event(ctx: JobContext, payload: dict[str, Any]) -> None:
    """Sends one small JSON event over LiveKit's own data channel (topic="transcript"), so the
    frontend can show live transcript/state without a second, separate websocket architecture -
    LiveKit's existing realtime capability already does this. Never raw audio, never a secret, only
    the same text this module already logs. Best-effort: a participant that has already left (the
    browser navigated away mid-reply) must never turn this into an unhandled task exception."""
    try:
        await ctx.room.local_participant.publish_data(json.dumps(payload), topic="transcript")
    except Exception as error:  # the room/participant may already be gone; never crash the pipeline
        log.warning("could not publish realtime event (%s): %s", type(error).__name__, error)


async def _count_audio_frames(track: rtc.Track, participant_identity: str) -> None:
    """Step 5A: count frames the worker actually receives from one subscribed remote audio track,
    for a bounded window, then log only the count. Never reads/logs/stores a frame's audio data."""
    audio_stream = rtc.AudioStream(track)
    frame_count = 0
    try:
        async with asyncio.timeout(_AUDIO_TEST_WINDOW_SECONDS):
            async for _event in audio_stream:
                frame_count += 1
    except TimeoutError:
        pass  # window elapsed; report whatever was received, not a failure
    finally:
        await audio_stream.aclose()

    log.info(
        "AUDIO_TEST: participant=%s track_kind=audio subscribed=true frames_received=%d",
        participant_identity,
        frame_count,
    )


def _watch_for_audio(ctx: JobContext) -> None:
    """Registers the Step 5A subscription handler. Independent of Test E: neither affects the
    other's success or failure."""

    def _on_track_subscribed(
        track: rtc.Track, _publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        log.info("AUDIO_TEST: participant=%s track_kind=audio subscribed=true", participant.identity)
        asyncio.create_task(_count_audio_frames(track, participant.identity))

    ctx.room.on("track_subscribed", _on_track_subscribed)


async def _speak_response(
    ctx: JobContext, text: str, participant_identity: str, realtime_agent: _RealtimeAgent | None = None
) -> None:
    """Step 5D: synthesize Gemini's real text response and publish it as a new LiveKit audio track,
    so the browser can subscribe to and play it. One-shot - called once per final transcript's
    Gemini response, never in a loop. `realtime_agent.voice`, when set, selects the Deepgram TTS
    voice (its `model` query param - see deepgram_tts.py) instead of the configured default, so
    different agents can sound different; absent, behavior is exactly as before this milestone."""
    try:
        config = load_deepgram_tts()
    except MissingConfiguration as error:
        log.error("STT_LLM_TTS_TEST: configuration error: %s", error)
        return

    log.info("STT_LLM_TTS_TEST: event=tts_started")
    await _publish_event(ctx, {"type": "state", "value": "speaking"})

    voice = realtime_agent.voice if realtime_agent is not None and realtime_agent.voice else config.model

    async with httpx.AsyncClient() as http:
        try:
            pcm = await deepgram_tts.synthesize(http, config.api_key, voice, text, _TTS_SAMPLE_RATE)
        except deepgram_tts.DeepgramTtsError as error:
            log.error("STT_LLM_TTS_TEST: %s", error)
            return

    log.info("STT_LLM_TTS_TEST: event=tts_audio_received bytes=%d", len(pcm))

    source = rtc.AudioSource(_TTS_SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("agent-voice", source)
    await ctx.room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    log.info("STT_LLM_TTS_TEST: event=livekit_audio_published")

    for offset in range(0, len(pcm), _TTS_FRAME_BYTES):
        chunk = pcm[offset : offset + _TTS_FRAME_BYTES]
        frame = rtc.AudioFrame(
            data=chunk, sample_rate=_TTS_SAMPLE_RATE, num_channels=1, samples_per_channel=len(chunk) // 2
        )
        await source.capture_frame(frame)

    await source.wait_for_playout()
    log.info("STT_LLM_TTS_TEST: event=tts_playback_ready")
    await _publish_event(ctx, {"type": "state", "value": "listening"})


async def _ask_llm_from_transcript(
    ctx: JobContext, text: str, participant_identity: str, realtime_agent: _RealtimeAgent | None = None
) -> None:
    """Step 5C: one real Gemini request for one real final transcript - never a hardcoded message.
    Reuses the exact Test E path (gemini_adk.ask -> Agent -> Runner -> DirectGeminiModel ->
    google-genai -> Gemini Developer API). `realtime_agent`, when resolved (see
    _resolve_realtime_agent), supplies the agent's own configured instruction in place of the
    existing deterministic test instruction (_STT_LLM_INSTRUCTION) - the Milestone's whole point:
    different agents produce different instructions and so different behavior, while a session with
    no agent selected keeps exactly the existing regression-tested behavior. Step 5D then speaks the
    real response back (see _speak_response), with the agent's own voice if it configured one."""
    log.info("STT_LLM_TEST: event=final_transcript text=%r", text)
    await _publish_event(ctx, {"type": "state", "value": "thinking"})

    try:
        model = load().google.model
    except MissingConfiguration as error:
        log.error("STT_LLM_TEST: configuration error: %s", error)
        return

    instruction = realtime_agent.instruction if realtime_agent is not None else _STT_LLM_INSTRUCTION

    try:
        response = await gemini_adk.ask(text, model, instruction=instruction)
    except gemini_adk.GeminiAdkError as error:
        log.error("STT_LLM_TEST: %s", error)
        return

    log.info("STT_LLM_TEST: event=gemini_response text=%r", response)
    log.info("STT_LLM_TTS_TEST: event=gemini_response text=%r", response)
    await _publish_event(ctx, {"type": "transcript", "speaker": "agent", "text": response})

    await _speak_response(ctx, response, participant_identity, realtime_agent)


async def _run_stt(
    ctx: JobContext, track: rtc.Track, participant_identity: str, realtime_agent: _RealtimeAgent | None = None
) -> None:
    """Step 5B: feed one subscribed remote audio track's real audio into Deepgram and log the
    transcripts received, for a bounded window. Never reads/logs/stores raw audio - only text."""
    try:
        config = load_deepgram_stt()
    except MissingConfiguration as error:
        log.error("STT_TEST: configuration error: %s", error)
        return

    try:
        stt_stream = await deepgram_stt.open_stream(config.api_key, config.model, _STT_SAMPLE_RATE)
    except deepgram_stt.DeepgramSttError as error:
        log.error("STT_TEST: %s", error)
        return

    audio_stream = rtc.AudioStream(track, sample_rate=_STT_SAMPLE_RATE, num_channels=1)

    async def _send_audio() -> None:
        async for event in audio_stream:
            await stt_stream.send_audio(event.frame.data.tobytes())

    async def _receive_transcripts() -> None:
        seen_final: set[str] = set()

        async for transcript in stt_stream.events():
            log.info(
                "STT_TEST: participant=%s event=transcript text=%r final=%s",
                participant_identity,
                transcript.text,
                transcript.is_final,
            )
            await _publish_event(
                ctx, {"type": "transcript", "speaker": "user", "text": transcript.text, "final": transcript.is_final}
            )
            if transcript.is_final and transcript.text and transcript.text not in seen_final:
                seen_final.add(transcript.text)
                # Not awaited here on purpose: a Gemini reply (and the TTS reply after it) can take
                # longer than the rest of this window, and must not block receiving further
                # transcript events in the meantime.
                asyncio.create_task(
                    _ask_llm_from_transcript(ctx, transcript.text, participant_identity, realtime_agent)
                )

    send_task = asyncio.create_task(_send_audio())
    receive_task = asyncio.create_task(_receive_transcripts())

    try:
        async with asyncio.timeout(_AUDIO_TEST_WINDOW_SECONDS):
            await asyncio.gather(send_task, receive_task)
    except TimeoutError:
        pass  # window elapsed; whatever transcripts arrived were already logged, not a failure
    finally:
        send_task.cancel()
        receive_task.cancel()
        await audio_stream.aclose()
        await stt_stream.aclose()


def _watch_for_stt(ctx: JobContext, realtime_agent: _RealtimeAgent | None = None) -> None:
    """Registers the Step 5B subscription handler, separately from `_watch_for_audio` on purpose -
    see the module docstring. A track supports more than one concurrent `rtc.AudioStream` reader, so
    this and the Step 5A frame counter both run off the same subscription event without conflict.
    `realtime_agent` is this job's own resolved value (or None), captured once by this closure and
    never looked up again - no module-level state, so concurrent jobs stay isolated."""

    def _on_track_subscribed(
        track: rtc.Track, _publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        asyncio.create_task(_run_stt(ctx, track, participant.identity, realtime_agent))

    ctx.room.on("track_subscribed", _on_track_subscribed)


async def entrypoint(ctx: JobContext) -> None:
    """Runs for the life of one job. Connects, wires up the Step 5A audio-frame and Step 5B STT
    diagnostics, runs the one-shot Step 4F ADK/Gemini integration check, then waits to be cancelled
    like the earlier connect-only milestone did."""

    async def _job_ended(reason: str) -> None:
        # `reason` is LiveKit's own short shutdown code (e.g. "room deleted"), never anything from a
        # participant or the room's contents. Must be async: JobContext.add_shutdown_callback
        # schedules it with asyncio.create_task(callback(reason)), which requires a coroutine.
        log.info("job ended (%s)", reason)

    ctx.add_shutdown_callback(_job_ended)

    await ctx.connect()
    log.info("connected to room %s", ctx.room.name)
    log.info("LiveKit job connected")

    realtime_agent = _resolve_realtime_agent(ctx)
    if realtime_agent is not None:
        log.info("realtime session using agent_id=%s", realtime_agent.id)
    else:
        log.info("realtime session using default test instructions (no agent selected)")

    _watch_for_audio(ctx)
    _watch_for_stt(ctx, realtime_agent)

    try:
        # config.load() only to read the configured Gemini model name; entrypoint_fnc's signature is
        # fixed by the framework (JobContext only), so main()'s own already-loaded config can't be
        # threaded through - re-reading is cheap and side-effect-free (see config.py's own docstring).
        model = load().google.model
        log.info("ADK test started")
        response = await gemini_adk.ask(_TEST_PROMPT, model)
    except MissingConfiguration as error:
        log.error("Test E FAILED: configuration error: %s", error)
    except gemini_adk.GeminiAdkError as error:
        log.error("Test E FAILED: %s", error)
    else:
        log.info("ADK Gemini response received")
        if _TEST_MARKER in response:
            log.info("Test E PASSED (response=%r)", response)
        else:
            log.error("Test E FAILED: unexpected response %r", response)

    try:
        # The framework cancels this task when the job ends; there is nothing else to do yet.
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info("job cancelled; exiting cleanly")
        raise


def _worker_options(config: AgentConfig) -> WorkerOptions:
    """The Google model side of `config` is loaded and validated (see config.load()) but not used yet:
    this milestone proves the LiveKit connection only. Passing the LiveKit credentials explicitly, rather
    than leaving them for the SDK's own environment-variable auto-detection, means the clean failure in
    main() below (via config.load()) is the one that fires when they are missing, not a less specific
    one from inside the SDK."""
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        ws_url=config.livekit.url,
        api_key=config.livekit.api_key,
        api_secret=config.livekit.api_secret,
    )


def main() -> None:
    try:
        config = load()
    except MissingConfiguration as error:
        # A clean, one-line, value-free failure - not a traceback - matching config.py's own contract.
        raise SystemExit(f"livekit_agent.worker: {error}")

    cli.run_app(_worker_options(config))


if __name__ == "__main__":
    main()
