"""Configuration for the LiveKit Agent worker: a separate process from `app.main`, running in its own
virtual environment (`backend/.venv`, see `backend/requirements-livekit.txt`), with its own settings.

This module imports nothing from `app.*`, on purpose: the worker must be able to start without the
FastAPI application's dependencies (SQLAlchemy, Alembic, pydantic-settings, ...) ever being present,
and without pulling in auth, the database, Twilio or Session as a side effect of reading its own
configuration. `backend/.venv` does not have `pydantic-settings` installed (only the LiveKit/Google
packages and what they pull in transitively), so this is a plain, dependency-light dataclass, not
`app.config.Settings`'s `BaseSettings` pattern - the same idea (typed, validated, fails loudly on a
missing required value, `.env`-driven), without the import.

Two config groups, read from environment variables (optionally via a `.env` file - see `load()`):

    LiveKitConfig       how the worker connects to LiveKit: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET.
                         The two credentials are server-side only; the browser is never given them
                         (it eventually gets a short-lived room token instead - not implemented yet).

    GoogleModelConfig    which Gemini model the worker's ADK-backed agent calls (see gemini_adk.py),
                         and how it authenticates: GEMINI_API_KEY, GEMINI_MODEL - the SAME names and
                         the SAME value app/config.py's existing Settings already read for the
                         production GeminiBrain. Step 4A originally gave this runtime a separate
                         GOOGLE_API_KEY, reasoning the two might one day run against different keys or
                         projects; Step 4E explicitly supersedes that (it is the same Google account
                         either way) and reuses the existing configuration instead of asking for a
                         second one. Vertex AI mode, also introduced in Step 4A, is deliberately not
                         used by this integration (Google Cloud billing currently blocks the project;
                         see gemini_adk.py) and has been removed from here - nothing else in the
                         codebase ever referenced GOOGLE_API_KEY/GOOGLE_GENAI_USE_VERTEXAI/
                         GOOGLE_CLOUD_PROJECT/GOOGLE_CLOUD_LOCATION, so removing them is safe.

    DeepgramSttConfig    Step 5B: which Deepgram speech-to-text model the worker's audio diagnostic
                         uses (see deepgram_stt.py), and how it authenticates: DEEPGRAM_API_KEY,
                         DEEPGRAM_STT_MODEL - the SAME names app/config.py's existing Settings already
                         read for the production Twilio/Deepgram call path (app/telephony/deepgram.py).
                         Reused, not duplicated, for the same reason GEMINI_API_KEY is reused above.
                         Validated separately (see load_deepgram_stt()) so a missing key only disables
                         the Step 5B diagnostic - it never blocks load()/load_livekit() or Test E.

    DeepgramTtsConfig    Step 5D: which Deepgram voice the worker's TTS diagnostic uses (see
                         deepgram_tts.py) - the SAME DEEPGRAM_API_KEY/DEEPGRAM_TTS_MODEL names
                         app/config.py's existing Settings already read for the production
                         Twilio/Deepgram call path (app/telephony/deepgram.py's DeepgramTTS). Reused,
                         not duplicated. Validated separately (see load_deepgram_tts()) so a missing
                         key only disables the Step 5D diagnostic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a direct dependency of this runtime
    load_dotenv = None

# backend/livekit_agent/config.py -> backend/.env. Resolved from this file's own location, not the
# current working directory, so the worker reads the right file however (and from wherever) it is
# started - unlike app/config.py's Settings, whose env_file=".env" is relative to the process's cwd.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Used only if GEMINI_MODEL is unset (the real .env has it set, reused as-is - see GoogleModelConfig).
# Not invented and not copied from an unrelated example: it is the installed google-adk==2.8.0
# package's own hardcoded default for its Gemini model wrapper (google.adk.models.google_llm.Gemini's
# `model` field), confirmed directly from that class's source - the most defensible fallback available
# without a live API call to verify a model name against.
_DEFAULT_MODEL = "gemini-2.5-flash"

# Used only if DEEPGRAM_STT_MODEL is unset - matches app/config.py's Settings.deepgram_stt_model
# default and the commented-out default already shown in backend/.env.example.
_DEFAULT_DEEPGRAM_MODEL = "nova-3"

# Used only if DEEPGRAM_TTS_MODEL is unset - matches app/config.py's Settings.deepgram_tts_model
# default and the commented-out default already shown in backend/.env.example.
_DEFAULT_DEEPGRAM_TTS_MODEL = "aura-2-thalia-en"


class MissingConfiguration(Exception):
    """A required environment variable is not set. The message names the variable only, never a value."""


def _require(name: str) -> str:
    value = os.environ.get(name)

    if not value:
        raise MissingConfiguration(f"{name} is not set. See backend/.env.example.")

    return value


@dataclass(frozen=True)
class LiveKitConfig:
    """How the worker registers with LiveKit and joins rooms. All three are server-side secrets
    (the API key and secret sign LiveKit access tokens); none of them belong in the browser."""

    url: str
    api_key: str
    api_secret: str

    @classmethod
    def from_env(cls) -> "LiveKitConfig":
        return cls(
            url=_require("LIVEKIT_URL"),
            api_key=_require("LIVEKIT_API_KEY"),
            api_secret=_require("LIVEKIT_API_SECRET"),
        )

    def __repr__(self) -> str:  # defence in depth: an accidental print/log must not leak the secret
        return f"LiveKitConfig(url={self.url!r}, api_key=<redacted>, api_secret=<redacted>)"


@dataclass(frozen=True)
class GoogleModelConfig:
    """Which Gemini model the worker's ADK-backed agent calls, via the Gemini Developer API - the
    same GEMINI_API_KEY / GEMINI_MODEL app/config.py's Settings already read for the production
    GeminiBrain (see the module docstring above for why this reuses them rather than a second,
    separately-configured key). google.adk's own Gemini model wrapper constructs its
    google.genai.Client from the process environment when none is passed explicitly, and picks up
    GEMINI_API_KEY the same way google-genai's SDK always has - this dataclass exists to fail
    clearly, naming the variable, if it is missing, not to hand the key to anything itself."""

    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> "GoogleModelConfig":
        model = os.environ.get("GEMINI_MODEL", "").strip() or _DEFAULT_MODEL
        return cls(api_key=_require("GEMINI_API_KEY"), model=model)

    def __repr__(self) -> str:  # defence in depth: an accidental print/log must not leak the key
        return f"GoogleModelConfig(api_key=<redacted>, model={self.model!r})"


@dataclass(frozen=True)
class DeepgramSttConfig:
    """Step 5B: which Deepgram model the worker's audio diagnostic uses, via the same real-time
    speech-to-text API app/telephony/deepgram.py already talks to for phone calls - see the module
    docstring above for why this reuses DEEPGRAM_API_KEY/DEEPGRAM_STT_MODEL rather than a second,
    separately-configured key."""

    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> "DeepgramSttConfig":
        model = os.environ.get("DEEPGRAM_STT_MODEL", "").strip() or _DEFAULT_DEEPGRAM_MODEL
        return cls(api_key=_require("DEEPGRAM_API_KEY"), model=model)

    def __repr__(self) -> str:  # defence in depth: an accidental print/log must not leak the key
        return f"DeepgramSttConfig(api_key=<redacted>, model={self.model!r})"


@dataclass(frozen=True)
class DeepgramTtsConfig:
    """Step 5D: which Deepgram voice the worker's TTS diagnostic uses, via the same real-time
    text-to-speech API app/telephony/deepgram.py already talks to for phone calls - see the module
    docstring above for why this reuses DEEPGRAM_API_KEY/DEEPGRAM_TTS_MODEL rather than a second,
    separately-configured key."""

    api_key: str
    model: str

    @classmethod
    def from_env(cls) -> "DeepgramTtsConfig":
        model = os.environ.get("DEEPGRAM_TTS_MODEL", "").strip() or _DEFAULT_DEEPGRAM_TTS_MODEL
        return cls(api_key=_require("DEEPGRAM_API_KEY"), model=model)

    def __repr__(self) -> str:  # defence in depth: an accidental print/log must not leak the key
        return f"DeepgramTtsConfig(api_key=<redacted>, model={self.model!r})"


@dataclass(frozen=True)
class AgentConfig:
    livekit: LiveKitConfig
    google: GoogleModelConfig

    @classmethod
    def from_env(cls) -> "AgentConfig":
        return cls(livekit=LiveKitConfig.from_env(), google=GoogleModelConfig.from_env())


def _load_env_file(env_file: Path | None) -> None:
    """`env_file`, if it exists, is loaded with `override=False`: a value already set in the real
    process environment always wins over the .env file, matching app/config.py's own Settings
    precedence."""
    if env_file is not None and load_dotenv is not None and env_file.is_file():
        load_dotenv(env_file, override=False)


def load(env_file: Path | None = _ENV_FILE) -> AgentConfig:
    """Load and validate the worker's full configuration (LiveKit and the Google model together).
    Raises MissingConfiguration, naming the variable, if something required is absent - never
    partially configured, never a silent default for a secret."""
    _load_env_file(env_file)
    return AgentConfig.from_env()


def load_livekit(env_file: Path | None = _ENV_FILE) -> LiveKitConfig:
    """Load and validate only the LiveKit connection settings, without requiring the Google model
    configuration `load()` also validates. For anything that only needs to talk to LiveKit (a
    token-issuing endpoint, for instance) and has no reason to know or care whether a Google model
    is configured yet - see app/main.py's /api/livekit/token."""
    _load_env_file(env_file)
    return LiveKitConfig.from_env()


def load_deepgram_stt(env_file: Path | None = _ENV_FILE) -> DeepgramSttConfig:
    """Load and validate only the Deepgram STT settings - separate from load()/load_livekit() so a
    missing DEEPGRAM_API_KEY only disables the Step 5B audio-diagnostic, never Test E or the LiveKit
    connection itself (see worker.py's _watch_for_stt)."""
    _load_env_file(env_file)
    return DeepgramSttConfig.from_env()


def load_deepgram_tts(env_file: Path | None = _ENV_FILE) -> DeepgramTtsConfig:
    """Load and validate only the Deepgram TTS settings - separate from everything else so a missing
    DEEPGRAM_API_KEY only disables the Step 5D voice-reply diagnostic (see worker.py's
    _speak_response)."""
    _load_env_file(env_file)
    return DeepgramTtsConfig.from_env()
