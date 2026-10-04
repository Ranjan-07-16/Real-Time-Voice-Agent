"""Minimal Google ADK -> Gemini Developer API adapter. Step 4E: proves one deterministic text round
trip, nothing else - no tools, no memory, no multi-agent orchestration, no persistence beyond what
ADK's own `InMemorySessionService` needs to make a single call.

Deliberately isolated from the existing production `GeminiBrain` (app/brains/gemini.py), which stays
exactly as it is: this reuses the same GEMINI_API_KEY/GEMINI_MODEL (see config.py), but talks to the
model through Google ADK's own `Agent`/`Runner` instead of calling `google-genai` directly, because
that is the layer the LiveKit worker is meant to build on (see Step 3's architecture review: ADK
should sit behind a narrow adapter, never duplicate Session's consent gate or GeminiBrain's tool loop).

How it authenticates: `google.adk.models.google_llm.Gemini` (which `Agent(model=<str>)` builds
internally for a plain model name) constructs its own `google.genai.Client` "from environment
variables" when none is passed explicitly - confirmed from that class's installed source - and
`google.genai`'s client already reads `GEMINI_API_KEY` from the process environment on its own. So
this module never constructs a client and never touches the key directly; setting the environment
variable (already done by config.py's `GoogleModelConfig`) is the entire authentication step.

Step 4E also diagnosed a `503 UNAVAILABLE` seen consistently on this path but not on a standalone
`google-genai` call: the two requests turned out to differ only in their `user-agent`/
`x-goog-api-client` header (`google-adk/2.8.0 ...` vs `google-genai-sdk/2.20.0 ...`). Passing a
pre-built `Client` via `Gemini`'s documented `client=` field was tried as the least invasive way to
test that - but `Gemini.generate_content_async` unconditionally re-merges its own tracking headers
into the per-call request config regardless of which client is used (see
`google.adk.utils._google_client_headers.merge_tracking_headers`, called from inside
`generate_content_async` itself - by the method's own comment, specifically so a custom client can't
drop them). There is no supported way in this installed version to suppress that header from
inside `Gemini` itself.

`DirectGeminiModel` below tests that hypothesis a different, fully-supported way: `google.adk.models
.base_llm.BaseLlm` is ADK's own public extension point for a custom model backend (confirmed public,
documented, and the base class `Gemini` itself extends; `LlmAgent.model` is typed
`Union[str, BaseLlm]`; ADK's own bundled `LiteLlm` is a real precedent of a non-Gemini-class
`BaseLlm` implementation). `Agent`/`Runner` still do all the orchestration - only the model
transport underneath is swapped. Because the tracking-header injection lives solely inside
`Gemini.generate_content_async` (not in ADK's shared flow code), a `BaseLlm` subclass that never
calls into `Gemini` never carries that header - not by suppressing it, but because that code path is
simply never reached.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.agents import Agent
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import Client, types

_APP_NAME = "livekit-agent-step4e"
_USER_ID = "livekit-worker"

# Step 4E/4F/5A/5B's own deterministic-echo instruction, unchanged, still the default so every
# existing caller (Test E) behaves identically. Step 5C passes its own conversational instruction
# instead - see worker.py's _STT_LLM_INSTRUCTION.
_DEFAULT_INSTRUCTION = "Reply exactly as the user asks, with nothing else added."


class GeminiAdkError(Exception):
    """The ADK/Gemini round trip failed. Safe to log: never constructed from a credential value."""


class DirectGeminiModel(BaseLlm):
    """A `BaseLlm` implementation that calls `google-genai` directly, bypassing ADK's built-in
    `Gemini` model wrapper entirely - see the module docstring for why. Non-streaming only: this
    milestone never sets `stream=True`, and a caller that did would get a clear error rather than a
    silent, wrong response."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if stream:
            raise GeminiAdkError("DirectGeminiModel does not support streaming (Step 4E scope: one deterministic non-streaming call).")

        # ADK's flow populates `LlmRequest.config.labels` (billing-breakdown metadata), but
        # google-genai==2.20.0 rejects `labels` outright in Gemini Developer API mode ("only
        # supported in Gemini Enterprise Agent Platform mode") - confirmed by a live 4xx-before-any-
        # network-call failure. `Gemini.generate_content_async` evidently strips or special-cases this
        # before calling google-genai; this does the same, narrowly. `model_copy(update=...)` is
        # `GenerateContentConfig`'s own pydantic v2 API (confirmed from its installed source) and does
        # not mutate `llm_request.config` - every other field is preserved unchanged.
        outgoing_config = None
        if llm_request.config is not None:
            outgoing_config = llm_request.config.model_copy(update={"labels": None})

        client = Client()  # reads GEMINI_API_KEY from the environment; never touches the value itself
        response = await client.aio.models.generate_content(
            model=llm_request.model,
            contents=llm_request.contents,
            config=outgoing_config,
        )

        if not response.candidates or not response.candidates[0].content:
            raise GeminiAdkError("Gemini response had no candidates/content")

        candidate = response.candidates[0]
        yield LlmResponse(content=candidate.content, finish_reason=candidate.finish_reason, partial=False)


async def ask(prompt: str, model: str, instruction: str = _DEFAULT_INSTRUCTION) -> str:
    """One text round trip: `prompt` in, Gemini's reply out, using `instruction` to shape the agent's
    behavior (defaults to Step 4E's deterministic-echo instruction - existing callers are unaffected).
    Raises GeminiAdkError on any failure (auth, network, an empty response) rather than returning a
    placeholder answer - a caller must never mistake this for a real response."""
    agent = Agent(
        name="step4e_test_agent",
        model=DirectGeminiModel(model=model),
        instruction=instruction,
    )
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name=_APP_NAME, session_service=session_service)
    session = await session_service.create_session(app_name=_APP_NAME, user_id=_USER_ID)
    message = types.Content(role="user", parts=[types.Part(text=prompt)])

    try:
        final_text: str | None = None

        async for event in runner.run_async(user_id=_USER_ID, session_id=session.id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(part.text or "" for part in event.content.parts)
    except Exception as error:  # auth, network, model errors: never treated as success
        raise GeminiAdkError(f"{type(error).__name__}: {error}") from error

    if not final_text:
        raise GeminiAdkError("Gemini returned no text response")

    return final_text
