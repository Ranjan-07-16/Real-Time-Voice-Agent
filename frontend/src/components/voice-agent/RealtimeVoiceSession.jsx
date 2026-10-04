import { useCallback, useEffect, useRef, useState } from "react";
import { LiveKitRoom, RoomAudioRenderer, useRoomContext } from "@livekit/components-react";
import { RoomEvent } from "livekit-client";
import Icon from "./Icon";
import CallVisualizer from "../voice/CallVisualizer";
import AiActivity from "../voice/AiActivity";
import { visualForCallState } from "../../runtime/voiceState.js";
import { CALL_STATE_TEXT, formatTime } from "../../runtime/format.js";
import { post } from "../../runtime/api.js";

// The real realtime voice session: Browser -> POST /api/livekit/token?agent_id=<agent> -> LiveKit
// room -> the separate LiveKit Agent worker (backend/livekit_agent/worker.py) -> Deepgram STT -> ADK
// -> Gemini -> Deepgram TTS -> LiveKit audio -> here. Replaces the old browser-speech/text-turn voice
// engine (runtime/useVoiceAgent.js, still used unchanged by CalleeRoute.jsx for the Twilio callee
// screen - this component never touches that file or its transports) for the operator's own voice
// test inside the dashboard. Visually: the same dock/orb/transcript language as the engine it
// replaces (CallVisualizer, AiActivity, AgentResponse, the same voice-panel/dock-* CSS classes) -
// the design is unchanged, only what powers it.
//
// The worker publishes small JSON events over LiveKit's own data channel (topic="transcript" - see
// worker.py's _publish_event) instead of a second websocket architecture: {type:"transcript",
// speaker:"user"|"agent", text, final?} and {type:"state", value:"thinking"|"speaking"|"listening"}.
// This component only ever reads that channel; it never calls Gemini or Deepgram directly.

const DECODER = new TextDecoder();

// "thinking" (the worker's own vocabulary) maps to the existing "processing" callState so
// CALL_STATE_TEXT/visualForCallState (already shared with the old voice engine) need no changes.
const STATE_EVENT_TO_CALL_STATE = { thinking: "processing", speaking: "speaking", listening: "listening" };

function IdleDock({ agentName, onStart, connecting, error, onViewHistory }) {
  return (
    <section className="voice-panel voice-dock state-idle" aria-label="Call controls">
      <div className="dock-main">
        <div className="dock-stage is-idle">
          <button
            type="button"
            className="voice-orb-button"
            onClick={onStart}
            disabled={connecting}
            aria-label="Start conversation"
          >
            <CallVisualizer state={connecting ? "connecting" : "idle"} orbIcon="microphone" size="xl" />
          </button>
        </div>

        <div className="dock-meta">
          <div className="dock-hint">{agentName ? `Speak with ${agentName}` : "Speak to start"}</div>
        </div>

        <div className="dock-actions">
          <button type="button" className="start-button" onClick={onStart} disabled={connecting}>
            <Icon name="microphone" size={18} />
            {connecting ? "Connecting..." : "Start Conversation"}
          </button>

          {onViewHistory && (
            <button type="button" className="history-button" onClick={onViewHistory}>
              <Icon name="clock" size={18} />
              View Call History
            </button>
          )}
        </div>
      </div>

      {error && <p className="voice-notice">{error}</p>}
    </section>
  );
}

function ActiveDock({ callState, micState, duration, interim, onStop }) {
  return (
    <section className={`voice-panel voice-dock state-${callState}`} aria-label="Call controls">
      <div className="dock-main">
        <div className={`dock-stage is-${callState}`}>
          <button type="button" className="voice-orb-button" onClick={onStop} aria-label="Stop conversation">
            <CallVisualizer
              state={visualForCallState(callState)}
              orbIcon="microphone"
              orbLabel={CALL_STATE_TEXT[callState]}
              statusLabel={CALL_STATE_TEXT[callState]}
              size="xl"
            />
          </button>
        </div>

        <AiActivity state={visualForCallState(callState)} acting={false} />

        <div className="dock-meta">
          <div className="timer">{formatTime(duration)}</div>
          <div className="live-caption" aria-live="polite">
            {interim ? `"${interim}"` : " "}
          </div>
        </div>

        <div className="dock-actions">
          <button type="button" className="stop-button" onClick={onStop}>
            <span className="stop-square" />
            Tap to stop
          </button>
        </div>
      </div>

      {micState === "blocked" && (
        <p className="voice-notice">Microphone access is blocked. Allow it in your browser to speak.</p>
      )}
      {micState === "error" && <p className="voice-notice">Could not use the microphone on this device.</p>}
    </section>
  );
}

// Lives inside <LiveKitRoom>: has room context, owns the mic + the data-channel transcript/state
// parsing. One instance per connected session - nothing here is module-level, so concurrent browser
// tabs/sessions never share state (matches the worker's own per-job isolation).
function ActiveSession({ onReady, onStop }) {
  const room = useRoomContext();
  const [micState, setMicState] = useState("requesting"); // requesting | on | blocked | error
  const [callState, setCallState] = useState("listening");
  const [messages, setMessages] = useState([]);
  const [interim, setInterim] = useState("");
  const [duration, setDuration] = useState(0);
  const startedRef = useRef(Date.now());
  const nextIdRef = useRef(1);

  useEffect(() => {
    let cancelled = false;

    room.localParticipant
      .setMicrophoneEnabled(true)
      .then(() => {
        if (!cancelled) setMicState("on");
      })
      .catch((err) => {
        if (!cancelled) setMicState(err?.name === "NotAllowedError" ? "blocked" : "error");
      });

    return () => {
      cancelled = true;
    };
  }, [room]);

  useEffect(() => {
    const timer = setInterval(() => setDuration(Math.floor((Date.now() - startedRef.current) / 1000)), 500);

    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const onData = (payload) => {
      let event;

      try {
        event = JSON.parse(DECODER.decode(payload));
      } catch {
        return; // never let a malformed/foreign data packet break the session
      }

      if (event.type === "transcript") {
        const time = formatTime(Math.floor((Date.now() - startedRef.current) / 1000));

        if (event.speaker === "user") {
          if (event.final) {
            setInterim("");
            if (event.text) setMessages((prev) => [...prev, { id: nextIdRef.current++, speaker: "You", text: event.text, time }]);
          } else {
            setInterim(event.text || "");
          }
        } else if (event.speaker === "agent" && event.text) {
          setMessages((prev) => [...prev, { id: nextIdRef.current++, speaker: "Agent", text: event.text, time }]);
        }
      } else if (event.type === "state" && STATE_EVENT_TO_CALL_STATE[event.value]) {
        setCallState(STATE_EVENT_TO_CALL_STATE[event.value]);
      }
    };

    room.on(RoomEvent.DataReceived, onData);

    return () => room.off(RoomEvent.DataReceived, onData);
  }, [room]);

  useEffect(() => {
    onReady({ messages, interim, callState });
  }, [messages, interim, callState, onReady]);

  return <ActiveDock callState={callState} micState={micState} duration={duration} interim={interim} onStop={onStop} />;
}

// Render-prop: the caller places `dock` and `transcript` into its own layout (OperatorConsole keeps
// its existing two-column grid - this component never decides page layout, only session behavior).
export default function RealtimeVoiceSession({ agentId, agentName, onViewHistory, children }) {
  const [session, setSession] = useState(null); // {url, token, room, agent_id, agent_name} | null
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(null);
  const [live, setLive] = useState({ messages: [], interim: "", callState: "listening" });

  const connect = useCallback(async () => {
    setError(null);
    setConnecting(true);

    try {
      const path = agentId ? `/api/livekit/token?agent_id=${encodeURIComponent(agentId)}` : "/api/livekit/token";
      const response = await post(path);

      setSession(await response.json());
      setLive({ messages: [], interim: "", callState: "listening" });
    } catch (err) {
      setError(err.message || "Could not start the voice session.");
    } finally {
      setConnecting(false);
    }
  }, [agentId]);

  const reset = useCallback(() => setSession(null), []);

  if (!session) {
    return children({
      dock: (
        <IdleDock agentName={agentName} onStart={connect} connecting={connecting} error={error} onViewHistory={onViewHistory} />
      ),
      transcript: { messages: [], interim: "", agentName },
    });
  }

  return (
    <LiveKitRoom
      serverUrl={session.url}
      token={session.token}
      connect
      audio={false}
      video={false}
      onDisconnected={reset}
      onError={(err) => {
        setError(err.message || "The voice session disconnected unexpectedly.");
        reset();
      }}
    >
      <RoomAudioRenderer />
      <ActiveSessionBridge onReady={setLive} reset={reset} agentDisplayName={session.agent_name || agentName}>
        {({ dock }) =>
          children({
            dock,
            transcript: {
              messages: live.messages,
              interim: live.interim,
              callState: live.callState,
              agentName: session.agent_name || agentName,
            },
          })
        }
      </ActiveSessionBridge>
    </LiveKitRoom>
  );
}

// Thin adapter so ActiveSession's room-context-only logic can still hand its dock element back out
// through the same render-prop contract as the idle state.
function ActiveSessionBridge({ onReady, reset, children }) {
  const room = useRoomContext();
  const stop = useCallback(() => room.disconnect(), [room]);

  return children({
    dock: <ActiveSession onReady={onReady} onStop={stop} />,
  });
}
