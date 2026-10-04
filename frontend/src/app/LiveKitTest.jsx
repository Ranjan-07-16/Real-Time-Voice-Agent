import { useCallback, useEffect, useRef, useState } from "react";
import { LiveKitRoom, useRemoteParticipants, useRoomContext } from "@livekit/components-react";
import { RoomEvent, Track } from "livekit-client";
import { getJson, post } from "../runtime/api.js";

// A throwaway development page proving Browser -> POST /api/livekit/token -> LiveKit room ->
// the separate LiveKit Agent worker (backend/livekit_agent/), and nothing else: no camera, no chat,
// no real conversation UI. audio/video auto-publish is explicitly off (see <LiveKitRoom> below) - the
// browser is never asked for a media permission just by connecting; the microphone test and the
// agent's spoken reply (Steps 5A/5D) are separate, explicit user actions. Not linked from anywhere in
// the product; reached only by its own URL (see router/router.js's "livekit-test" entry) while signed
// in - the token endpoint requires the same operator session every other API call does.

// Renders once actually inside the room (has room context): who else is in it tells us whether
// LiveKit dispatched the agent job, straight from the browser's own view of the room.
function AgentStatus() {
  const remoteParticipants = useRemoteParticipants();
  return <p>Agent: {remoteParticipants.length > 0 ? "Dispatched" : "Waiting"}</p>;
}

function DisconnectButton() {
  const room = useRoomContext();

  return (
    <button type="button" onClick={() => room.disconnect()}>
      Disconnect
    </button>
  );
}

// Step 5A: prove browser mic audio reaches the worker, nothing else. `setMicrophoneEnabled` (a
// direct livekit-client 2.22.3 API, confirmed from its installed source) both requests the OS/browser
// microphone permission and creates+publishes the local audio track in one call; passing `false`
// unpublishes and releases it. No STT/TTS/Gemini is reachable from this component.
function MicrophoneTest() {
  const room = useRoomContext();
  const [micStatus, setMicStatus] = useState("idle"); // idle | requesting | granted | published | denied
  const [micError, setMicError] = useState(null);

  const start = useCallback(async () => {
    setMicError(null);
    setMicStatus("requesting");
    try {
      const publication = await room.localParticipant.setMicrophoneEnabled(true);
      setMicStatus(publication ? "published" : "granted");
    } catch (err) {
      setMicStatus("denied");
      setMicError(err.message || "Microphone permission was denied.");
    }
  }, [room]);

  const stop = useCallback(async () => {
    await room.localParticipant.setMicrophoneEnabled(false);
    setMicStatus("idle");
  }, [room]);

  const micLabel = { idle: "Off", requesting: "Requesting...", granted: "Granted", published: "Granted", denied: "Denied" }[micStatus];
  const trackLabel = micStatus === "published" ? "Published" : "Not published";

  return (
    <div style={{ marginTop: 16, borderTop: "1px solid #ccc", paddingTop: 16 }}>
      <p>Microphone: {micLabel}</p>
      <p>Audio Track: {trackLabel}</p>
      {micError && <p style={{ color: "crimson" }}>{micError}</p>}
      {micStatus === "published" ? (
        <button type="button" onClick={stop}>
          Stop Microphone Test
        </button>
      ) : (
        <button type="button" onClick={start} disabled={micStatus === "requesting"}>
          Start Microphone Test
        </button>
      )}
    </div>
  );
}

// Step 5D: the worker publishes its Deepgram-synthesized reply as a new remote audio track (see
// backend/livekit_agent/worker.py's _speak_response) - this just subscribes to it and, on an
// explicit click (browsers block un-gestured autoplay), attaches and plays it. `Track.attach(el)`
// and `RoomEvent.TrackSubscribed` are both direct livekit-client 2.22.3 APIs, confirmed from its
// installed type declarations. No new media framework, no autoplay-failure hiding.
function AgentAudioPlayback() {
  const room = useRoomContext();
  const [agentTrack, setAgentTrack] = useState(null);
  const [playError, setPlayError] = useState(null);
  const audioRef = useRef(null);

  useEffect(() => {
    function onTrackSubscribed(track) {
      if (track.kind === Track.Kind.Audio) {
        setAgentTrack(track);
      }
    }
    room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);
    return () => room.off(RoomEvent.TrackSubscribed, onTrackSubscribed);
  }, [room]);

  const play = useCallback(async () => {
    setPlayError(null);
    try {
      agentTrack.attach(audioRef.current);
      await audioRef.current.play();
    } catch (err) {
      setPlayError(err.message || "Could not play the response.");
    }
  }, [agentTrack]);

  return (
    <div style={{ marginTop: 16, borderTop: "1px solid #ccc", paddingTop: 16 }}>
      <p>Agent Audio: {agentTrack ? "Received" : "Waiting"}</p>
      {playError && <p style={{ color: "crimson" }}>{playError}</p>}
      <audio ref={audioRef} />
      <button type="button" onClick={play} disabled={!agentTrack}>
        Play Response
      </button>
    </div>
  );
}

export default function LiveKitTest() {
  const [status, setStatus] = useState("disconnected"); // disconnected | connecting | connected
  const [session, setSession] = useState(null); // { url, token, room, agent_id, agent_name } from the server
  const [error, setError] = useState(null);
  // The existing Agent configuration (app/models/agent.py), fetched the same way the dashboard's
  // own "Configure Agent" flow does (GET /api/agents, organization-scoped). "" means no agent
  // selected: the existing default/test session (unchanged from before this milestone).
  const [agents, setAgents] = useState([]);
  const [agentId, setAgentId] = useState("");

  useEffect(() => {
    getJson("/api/agents")
      .then(setAgents)
      .catch(() => {}); // the dev page still works with no agent selectable; Connect just uses the default
  }, []);

  const connect = useCallback(async () => {
    setError(null);
    setStatus("connecting");

    try {
      const path = agentId ? `/api/livekit/token?agent_id=${encodeURIComponent(agentId)}` : "/api/livekit/token";
      const response = await post(path);
      setSession(await response.json());
    } catch (err) {
      setStatus("disconnected");
      setError(err.message || "Could not get a LiveKit token.");
    }
  }, [agentId]);

  const reset = useCallback(() => {
    setSession(null);
    setStatus("disconnected");
  }, []);

  const label = status[0].toUpperCase() + status.slice(1);

  return (
    <div style={{ padding: 24, fontFamily: "monospace", maxWidth: 480 }}>
      <h1>LiveKit Connection Test</h1>
      <p>Status: {label}</p>
      {error && <p style={{ color: "crimson" }}>{error}</p>}

      {!session ? (
        <>
          <p>
            <label>
              Agent:{" "}
              <select value={agentId} onChange={(event) => setAgentId(event.target.value)} disabled={status === "connecting"}>
                <option value="">(none - default test behavior)</option>
                {agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </label>
          </p>
          <button type="button" onClick={connect} disabled={status === "connecting"}>
            Connect
          </button>
        </>
      ) : (
        <LiveKitRoom
          serverUrl={session.url}
          token={session.token}
          connect
          audio={false}
          video={false}
          onConnected={() => setStatus("connected")}
          onDisconnected={reset}
          onError={(err) => {
            setError(err.message);
            reset();
          }}
        >
          <p>Room: {session.room}</p>
          <p>Configured agent: {session.agent_name || "(none - default test behavior)"}</p>
          <AgentStatus />
          <DisconnectButton />
          <MicrophoneTest />
          <AgentAudioPlayback />
        </LiveKitRoom>
      )}
    </div>
  );
}
