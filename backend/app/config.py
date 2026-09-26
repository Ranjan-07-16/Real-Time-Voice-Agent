from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    app_name: str = "Real-Time Voice Agent"
    app_env: str = "development"  # development | staging | production
    # Development aid: unhandled errors render a traceback page. Never honoured in production.
    debug: bool = False
    log_level: str = "INFO"

    # Server-side only. The key never goes to the browser.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    # 0 = no thinking (fastest first word, right for voice); -1 = model decides.
    gemini_thinking_budget: int = 0

    # A single LLM round-trip (including streaming it) must finish within this.
    brain_timeout_seconds: float = 30.0

    # The Vite dev server proxies /api, so CORS only matters for other origins. A JSON list in the environment:
    # CORS_ORIGINS=["https://app.example.com"]. Each entry is an exact origin (scheme + host, no path, no trailing
    # slash). These origins are also the ones allowed to make signed-in writes (see Auth.origin_ok), including when a
    # frontend host forwards /api to this server, because the browser still sends the frontend's Origin.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Persistence. Required, so a missing value fails loudly instead of quietly using another
    # database. PostgreSQL: postgresql+psycopg://user:password@host:5432/dbname
    database_url: str

    # Outbound calling. The business API is closed until an API key is set.
    api_key: str | None = Field(default=None, validation_alias="VOICE_AGENT_API_KEY")
    # Signs result callbacks; defaults to the API key.
    webhook_secret: str | None = Field(default=None, validation_alias="VOICE_AGENT_WEBHOOK_SECRET")
    public_base_url: str = "http://localhost:5173"  # where callees open their answer link
    allow_private_callbacks: bool = False  # only for local development
    callback_attempts: int = 3
    callback_backoff_seconds: float = 2.0
    sweep_interval_seconds: float = 5.0
    # The workflow scheduler: every interval it runs the active workflows and then places the calls
    # that are due. OFF by default, because switching it on lets this process ring people unprompted.
    # Enable it on exactly the processes that should do so (several may: see README).
    workflow_scheduler_enabled: bool = False
    # A polling interval, not a trigger system: never faster than once a minute.
    workflow_scheduler_interval_seconds: float = Field(default=60.0, ge=60.0)
    workflow_scheduler_workflow_limit: int = Field(default=100, ge=1)  # active workflows run per tick
    workflow_scheduler_dispatch_limit: int = Field(default=100, ge=1)  # scheduled jobs looked at per tick
    call_idle_timeout_seconds: int = 120

    # Operator sign-in. On by default: the console and its call endpoints need a login.
    # Create the first user with: python -m app.cli create-user
    google_client_id: str | None = None
    # Self-service sign-up (POST /api/auth/signup): anyone may create an account, which comes with a
    # workspace of its own and the "operator" role (never admin). Set SIGNUP_ENABLED=false to close it,
    # e.g. once real telephony is configured and you do not want strangers able to place calls.
    signup_enabled: bool = True
    auth_required: bool = True
    auth_session_hours: int = 12
    cookie_secure: bool = False  # set true when served over https (always, in production)
    # Behind a reverse proxy, how many proxies to trust for X-Forwarded-For. 0 = none,
    # use the socket address. Wrong values let clients pick their own rate-limit identity.
    # Set it to the number of proxies in YOUR deployment's chain (it differs between hosts); with 0 behind a
    # proxy, every client shares the proxy's address and therefore one rate-limit bucket.
    trusted_proxy_hops: int = 0

    # Requests per minute. The window is one minute.
    limit_login_per_ip: int = 10
    limit_signup_per_ip: int = 5
    limit_login_per_email: int = 5
    limit_callee_per_ip: int = 30
    limit_jobs_per_principal: int = 60
    limit_calls_per_user: int = 20
    limit_turns_per_call: int = 30

    # --- Telephony: Twilio carries the call, Deepgram does speech-to-text and text-to-speech.
    # Phone calls switch on only when all of these are set (see telephony_configured).
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None  # your Twilio number, e.g. +14155550123
    # Where Twilio can reach THIS server over the internet (https), e.g. an ngrok URL.
    public_api_url: str | None = None
    deepgram_api_key: str | None = None
    deepgram_stt_model: str = "nova-3"
    deepgram_tts_model: str = "aura-2-thalia-en"
    # Silence, in ms, before Deepgram decides the caller has finished a sentence.
    deepgram_endpointing_ms: int = 400
    deepgram_utterance_end_ms: int = 1000
    # The caller must say this many words over the agent to cut it off.
    barge_in_min_words: int = 2
    # Which voice runtime conducts a phone call, chosen once when the call starts. "legacy" is the runtime in
    # production use; "pipecat" is opt-in (see app/pipecat_runtime). With "pipecat", the agent ids below (comma
    # separated) limit it to those agents' calls; empty means every call. Anything else stays on legacy.
    voice_runtime: Literal["legacy", "pipecat"] = "legacy"
    voice_runtime_pipecat_agent_ids: str = ""
    # Experimental, and only with voice_runtime "pipecat": "observe" runs Pipecat's Silero VAD alongside a call and records
    # when speech started and stopped. It is observation only: it does not affect turn detection, interruptions or
    # anything else the call does. "off" (the default) creates no VAD at all.
    # "interrupt" also wires VAD's speech-started signal into SessionProcessor as an accepted, inert diagnostic hint:
    # it never itself interrupts, clears playback or calls process_turn (see app/pipecat_runtime/session_processor.py).
    voice_pipecat_vad: Literal["off", "observe", "interrupt"] = "off"
    # Which recognizer a Pipecat call uses: "compat" wraps the existing Deepgram Listener (the reference behavior,
    # unchanged since Step 10); "native" uses Pipecat's own DeepgramSTTService, configured to match it as closely
    # as that service allows (see app/pipecat_runtime/native_stt.py). Experimental; off (compat) by default.
    voice_pipecat_stt: Literal["compat", "native"] = "compat"
    # Off by default: caller ID proves nothing, and inbound calls have no identity
    # check, so an open phone line would hand demo data to anyone who rings.
    twilio_inbound_enabled: bool = False
    twilio_inbound_profile: str = "bank"

    max_sessions: int = 200
    session_ttl_seconds: int = 3600

    @field_validator("voice_runtime_pipecat_agent_ids")
    @classmethod
    def _agent_ids_are_integers(cls, value: str) -> str:
        pipecat_agent_ids(value)  # a typo must fail at startup, not silently select nothing
        return value

    @property
    def is_production(self) -> bool:
        """APP_ENV=production, however it is capitalised or padded: what switches off the development aids
        (tracebacks in the browser, the interactive API documentation)."""
        return self.app_env.strip().lower() == "production"


def production_warnings(settings: Settings) -> list[str]:
    """Settings that are legal but almost certainly wrong for a production deployment: one readable line each,
    naming the variable and never its value. Only advice, never a refusal to start: whoever runs the server
    knows things this cannot (for example that no proxy sits in front of it)."""
    if not settings.is_production:
        return []

    warnings = []

    if not settings.cookie_secure:
        warnings.append("COOKIE_SECURE is false, so the session cookie would also travel over plain http: set COOKIE_SECURE=true.")

    if not settings.auth_required:
        warnings.append("AUTH_REQUIRED is false, so the console endpoints need no sign-in: set AUTH_REQUIRED=true.")

    if settings.database_url.startswith("sqlite"):
        warnings.append("DATABASE_URL is a SQLite file: hosting disks are usually wiped on every deploy and one file cannot be shared. Use PostgreSQL.")
    elif not settings.database_url.startswith("postgresql+psycopg://"):
        warnings.append("DATABASE_URL should start with postgresql+psycopg:// (postgres:// and postgresql:// select a driver that is not installed).")

    if settings.trusted_proxy_hops == 0:
        warnings.append(
            "TRUSTED_PROXY_HOPS is 0: every client is rate-limited by the address of whatever connects to this server. "
            "Behind a reverse proxy, set it to the number of proxies in front of this process."
        )

    if settings.allow_private_callbacks:
        warnings.append("ALLOW_PRIVATE_CALLBACKS is true: result callbacks may reach private addresses. It is for local development only.")

    if urlsplit(settings.public_base_url).hostname in ("localhost", "127.0.0.1", "::1"):
        warnings.append("PUBLIC_BASE_URL still points at localhost: set it to the frontend's public https address (answer links and the origin check use it).")

    return warnings


def pipecat_agent_ids(value: str) -> frozenset[int]:
    """"3, 7" -> {3, 7}. Blank entries are ignored; anything that is not a whole number is an error."""
    try:
        return frozenset(int(item) for item in value.split(",") if item.strip())
    except ValueError:
        raise ValueError("VOICE_RUNTIME_PIPECAT_AGENT_IDS must be a comma-separated list of agent ids (whole numbers)") from None


def telephony_configured(settings: Settings) -> bool:
    return all(
        (
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.twilio_from_number,
            settings.public_api_url,
            settings.deepgram_api_key,
        )
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
