"""Minimal Deepgram real-time STT client for the LiveKit worker. Step 5B: prove real microphone
audio produces a real transcript - nothing else, no TTS, no Gemini, no persistence.

Hand-rolled over a raw WebSocket (`websockets`, a transitive dependency of `livekit-agents`, already
installed in `backend/.venv` - confirmed via `pip show`), the same approach the existing production
integration (`app/telephony/deepgram.py`) uses for the Twilio call path. Deliberately not imported
from here: `backend/livekit_agent/` imports nothing from `app.*` (see `config.py`'s own docstring),
so this is a small, independent client, not a refactor of the production one. It reuses the SAME
`DEEPGRAM_API_KEY`/`DEEPGRAM_STT_MODEL` environment variables as that production path (see
`config.py`'s `DeepgramSttConfig`) - not a second, separately-configured credential.

The one real difference from the production query string: this is configured for linear16/mono audio
(what `rtc.AudioStream` delivers - see worker.py) rather than the production path's 8 kHz mu-law
(what Twilio carries). Everything else about how a transcript event is read back is the same shape.

Connection retry: a controlled investigation (five sequential, fully isolated connection attempts to
Deepgram's real-time endpoint, no LiveKit/Gemini/AudioStream involved at all) measured 4/5 successes
at ~0.9-1.0s and 1/5 hitting the full `open_timeout` - identical variability with or without any of
this project's own code in the loop. That points at Deepgram's own connection acceptance, not
anything here, so `open_stream` tolerates exactly one such failure with a short, bounded backoff
rather than treating an occasional slow accept as a hard error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import connect

log = logging.getLogger("livekit_agent.deepgram_stt")

_STT_URL = "wss://api.deepgram.com/v1/listen"
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.25


class DeepgramSttError(Exception):
    """The Deepgram STT connection failed. Safe to log: never constructed from a credential value."""


@dataclass(frozen=True)
class Transcript:
    text: str
    is_final: bool


def _parse_event(raw: str | bytes) -> Transcript | None:
    """One Deepgram message -> a Transcript, or None for anything else (metadata, empty results)."""
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return None

    if message.get("type") != "Results":
        return None

    alternatives = (message.get("channel") or {}).get("alternatives") or [{}]
    text = (alternatives[0].get("transcript") or "").strip()

    if not text:
        return None

    return Transcript(text=text, is_final=bool(message.get("is_final")))


class SttStream:
    """One track's live transcription."""

    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def send_audio(self, pcm16: bytes) -> None:
        await self._socket.send(pcm16)

    async def events(self) -> AsyncIterator[Transcript]:
        try:
            async for raw in self._socket:
                event = _parse_event(raw)
                if event is not None:
                    yield event
        except Exception:  # the connection dropped: the diagnostic ends, nothing else depends on it
            return

    async def aclose(self) -> None:
        try:
            await self._socket.send(json.dumps({"type": "CloseStream"}))
            await self._socket.close()
        except Exception:
            pass


async def open_stream(api_key: str, model: str, sample_rate: int) -> SttStream:
    query = urlencode(
        {
            "model": model,
            "language": "en",
            "encoding": "linear16",
            "sample_rate": sample_rate,
            "channels": 1,
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
        }
    )
    uri = f"{_STT_URL}?{query}"
    headers = {"Authorization": f"Token {api_key}"}

    last_error: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        start = time.monotonic()
        try:
            socket = await connect(uri, additional_headers=headers, open_timeout=10, max_size=2**20)
            log.info(
                "Deepgram connection attempt %d/%d succeeded in %.3fs",
                attempt, _RETRY_ATTEMPTS, time.monotonic() - start,
            )
            return SttStream(socket)
        except Exception as error:
            last_error = error
            log.warning(
                "Deepgram connection attempt %d/%d failed (%s) after %.3fs",
                attempt, _RETRY_ATTEMPTS, type(error).__name__, time.monotonic() - start,
            )
            if attempt < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)

    raise DeepgramSttError(f"could not start speech recognition ({type(last_error).__name__})") from last_error
