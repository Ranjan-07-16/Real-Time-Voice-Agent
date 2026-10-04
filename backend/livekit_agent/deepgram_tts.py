"""Minimal Deepgram TTS client for the LiveKit worker. Step 5D: turn Gemini's real text response
into real audio - nothing else, no streaming optimization, no interruption handling.

Hand-rolled over `httpx` (already installed - a transitive dependency of `google-adk`/`google-genai`,
confirmed via `pip show`), the same approach the existing production integration
(`app/telephony/deepgram.py`'s `DeepgramTTS`) uses for the Twilio call path. Deliberately not
imported from here: `backend/livekit_agent/` imports nothing from `app.*` (see `config.py`'s own
docstring), so this is a small, independent client, not a refactor of the production one. It reuses
the SAME `DEEPGRAM_API_KEY`/`DEEPGRAM_TTS_MODEL` environment variables as that production path (see
`config.py`'s `DeepgramTtsConfig`) - not a second, separately-configured credential.

The one real difference from the production query string: this asks for linear16 audio at LiveKit's
own publish rate (what `rtc.AudioSource`/`rtc.AudioFrame` need - see worker.py) rather than the
production path's 8 kHz mu-law/Twilio container. `container=none` means the response body is raw PCM
samples with no header - confirmed from the production implementation's own identical use of that
parameter - so the bytes returned here can be chunked directly into `AudioFrame`s without any parsing.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

_TTS_URL = "https://api.deepgram.com/v1/speak"


class DeepgramTtsError(Exception):
    """The Deepgram TTS request failed. Safe to log: never constructed from a credential value."""


async def synthesize(http: httpx.AsyncClient, api_key: str, model: str, text: str, sample_rate: int) -> bytes:
    """One text -> one complete linear16/mono PCM buffer. Buffered, not streamed - Step 5D's own
    scope is correctness first (see worker.py's module docstring)."""
    url = f"{_TTS_URL}?" + urlencode(
        {"model": model, "encoding": "linear16", "sample_rate": sample_rate, "container": "none"}
    )

    try:
        response = await http.post(
            url,
            json={"text": text},
            headers={"Authorization": f"Token {api_key}"},
            timeout=httpx.Timeout(15.0, read=30.0),
        )
    except httpx.HTTPError as error:
        raise DeepgramTtsError(f"text-to-speech request failed ({type(error).__name__})") from None

    if response.status_code >= 400:
        raise DeepgramTtsError(f"text-to-speech failed with status {response.status_code}")

    return response.content
