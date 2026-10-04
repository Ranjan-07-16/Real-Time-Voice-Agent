"""Test-only speech-injection harness for Step 5D. Publishes a prerecorded speech WAV into a
LiveKit room as a synthetic "microphone", so the full worker pipeline (Deepgram STT -> ADK ->
Gemini -> Deepgram TTS -> LiveKit audio out) can be exercised without a human speaking into a real
microphone - Claude's own automated browser testing has no way to produce real speech content (see
Step 5D's capability audit: a fake browser mic device produces real audio frames but not
recognizable speech).

NOT part of the production application. Lives under backend/livekit_agent/testing/, a clearly
separate subpackage; nothing in app/, app/telephony/, or the rest of livekit_agent/ imports from
here, and this module imports nothing from app/.* either (same isolation as the rest of
livekit_agent/ - see config.py's own docstring). Uses only what's already a pinned dependency of
backend/requirements-livekit.txt (livekit, livekit-api) plus Python's stdlib `wave` module - no new
dependency was added for this harness.

The fixture (fixtures/test_phrase.wav) is pre-generated, not created at runtime, using two
already-installed, purely local, offline command-line tools - no network call, no new package:

    espeak-ng -s 160 -w raw.wav "Hello, this is the LiveKit speech recognition test."
    ffmpeg -y -i raw.wav -ar 16000 -ac 1 -sample_fmt s16 test_phrase.wav

It is synthetic, computer-generated speech (not a human recording) - already PCM16/16kHz/mono,
exactly what the existing Deepgram STT path (deepgram_stt.py) expects, so this harness publishes it
as-is with no resampling step.

Usage (reads LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET from backend/.env via the existing
config.load_livekit() - no new credential, same mechanism the token endpoint and the worker use):

    cd backend && .venv/bin/python -m livekit_agent.testing.inject_speech [--room ROOM_NAME]

With no --room, a fresh `dev-test-<hex>` room is created (same naming convention as
app/main.py's /api/livekit/token route) and its name is logged, so the worker's own automatic
dispatch picks it up. Passing --room joins a room that's already open (e.g. one a real browser
LiveKitTest.jsx session is already connected to), so the agent's spoken reply can be observed there.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import wave
from datetime import timedelta
from pathlib import Path

from livekit import api as livekit_api
from livekit import rtc

from livekit_agent.config import MissingConfiguration, load_livekit

log = logging.getLogger("livekit_agent.testing.inject_speech")

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "test_phrase.wav"
_IDENTITY = "speech-injector-test"
_FRAME_MS = 20


def _load_fixture(fixture_path: Path = _FIXTURE_PATH) -> tuple[bytes, int, int]:
    """Returns (pcm_bytes, sample_rate, num_channels). Fails loudly if the fixture is missing or
    not 16-bit PCM - never silently resamples or substitutes something else."""
    if not fixture_path.is_file():
        raise FileNotFoundError(f"Speech fixture not found at {fixture_path}")

    with wave.open(str(fixture_path), "rb") as wav_file:
        if wav_file.getsampwidth() != 2:
            raise ValueError(f"Fixture must be 16-bit PCM, got sample width {wav_file.getsampwidth()}")
        sample_rate = wav_file.getframerate()
        num_channels = wav_file.getnchannels()
        pcm_bytes = wav_file.readframes(wav_file.getnframes())

    return pcm_bytes, sample_rate, num_channels


async def inject(
    room_name: str | None,
    fixture_path: Path = _FIXTURE_PATH,
    hold_seconds: float = 0.0,
    preroll_seconds: float = 0.0,
) -> None:
    pcm, sample_rate, num_channels = _load_fixture(fixture_path)
    duration_s = len(pcm) / 2 / num_channels / sample_rate
    log.info("speech fixture loaded (bytes=%d, duration_s=%.2f)", len(pcm), duration_s)

    try:
        config = load_livekit()
    except MissingConfiguration as error:
        log.error("configuration error: %s", error)
        return

    room_name = room_name or f"dev-test-{secrets.token_hex(8)}"
    grants = livekit_api.VideoGrants(room_join=True, room=room_name)
    token = (
        livekit_api.AccessToken(config.api_key, config.api_secret)
        .with_identity(_IDENTITY)
        .with_grants(grants)
        .with_ttl(timedelta(minutes=10))
        .to_jwt()
    )

    room = rtc.Room()
    await room.connect(config.url, token)
    log.info("test participant connected (room=%s, identity=%s)", room_name, _IDENTITY)

    source = rtc.AudioSource(sample_rate, num_channels)
    track = rtc.LocalAudioTrack.create_audio_track("test-speech", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    log.info("speech track published")

    frame_bytes = int(sample_rate * _FRAME_MS / 1000) * 2 * num_channels

    if preroll_seconds > 0:
        # Already-known, out-of-scope latency: the worker's ctx.connect() plus track-subscribe can
        # take several seconds, during which any already-sent real speech is simply missed (never
        # buffered). Silence frames sent first give a slow-connecting worker time to subscribe and
        # open its STT stream before the real fixture content starts, same technique already proven
        # earlier in this project's own diagnostics.
        silence_frame = bytes(frame_bytes)
        preroll_frames = int(preroll_seconds * 1000 / _FRAME_MS)
        for _ in range(preroll_frames):
            frame = rtc.AudioFrame(
                data=silence_frame,
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=frame_bytes // 2 // num_channels,
            )
            await source.capture_frame(frame)
        log.info("silence preroll completed (seconds=%.1f)", preroll_seconds)

    chunks_sent = 0
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        frame = rtc.AudioFrame(
            data=chunk,
            sample_rate=sample_rate,
            num_channels=num_channels,
            samples_per_channel=len(chunk) // 2 // num_channels,
        )
        await source.capture_frame(frame)
        chunks_sent += 1

    await source.wait_for_playout()
    log.info("speech injection completed (chunks_published=%d)", chunks_sent)

    if hold_seconds > 0:
        # The worker's own ctx.connect() can take several seconds (a separate, already-known,
        # out-of-scope latency - not something this harness works around by default); holding the
        # room open gives a slow-connecting worker time to subscribe before this test participant
        # leaves, for tests that need the full STT/Gemini/TTS pipeline to actually run.
        await asyncio.sleep(hold_seconds)

    await room.disconnect()
    log.info("test participant disconnected")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", default=None, help="Join an existing room instead of creating a fresh one")
    parser.add_argument(
        "--fixture", default=None, help="Path to an alternate fixture WAV (defaults to fixtures/test_phrase.wav)"
    )
    parser.add_argument(
        "--hold-seconds", type=float, default=0.0, help="Keep the room open this long after playout before disconnecting"
    )
    parser.add_argument(
        "--preroll-seconds", type=float, default=0.0, help="Send this many seconds of silence before the real fixture audio"
    )
    args = parser.parse_args()
    fixture_path = Path(args.fixture) if args.fixture else _FIXTURE_PATH
    asyncio.run(inject(args.room, fixture_path, args.hold_seconds, args.preroll_seconds))


if __name__ == "__main__":
    main()
