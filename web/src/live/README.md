# Live voice

The React chat uses ElevenLabs' realtime conversational agent over WebRTC through
`@elevenlabs/client`. The SDK streams microphone audio and plays the agent's audio
track directly. Live mode does not call `/api/live/listen`, `/api/live/say`, or the
separate dictation/read-aloud routes.

Clicking Talk live opens a dark, responsive dialog with a procedural cloud orb.
The orb follows microphone or speaker energy. The transcript toggle shows both
sides of the conversation; those messages are also retained in the chat. Back to
chat minimizes the dialog without hanging up, so confirmation cards remain usable.
Mute affects only the microphone. End and Escape release the microphone and stop
playback. The user speaks first; ordinary typed replies remain silent.

## Setup

The server reads ELEVENLABS_API_KEY from the repository or morning-brief .env.
Run the explicit, idempotent setup command once:

```powershell
uv run --with aiohttp python harness/agent_voice.py
```

This creates a private agent and stores its ID in the gitignored
`harness/state/realtime-agent.json`. ELEVENLABS_AGENT_ID can instead select an
existing agent configured with the same ask_workspace client tool. Restart the UI
server after adding backend routes. GET /api/live/agent/status never provisions an
agent or starts a paid conversation. POST /api/live/agent/session obtains a short-lived
WebRTC token; the long-lived API key never leaves the server.

## Workspace actions

The agent answers casual conversation directly. For chart data, posts, projects,
briefs or Telegram actions, it calls ask_workspace. The client runs the existing
Ask workflow, renders its cards and activity, and returns its final answer to the
voice agent, which speaks the result. Transcription events alone never submit a
second local assistant turn. Existing confirmation cards still require the user's
click; the agent cannot approve them. Interrupting a pending workspace request
aborts that local turn and discards stale results.

agentSession.ts owns the lifecycle and can be tested with a fake SDK. useTalkLive.ts
binds it to React. TalkLiveButton.tsx owns the modal and controls. CloudOrb.tsx renders
animated WebGL clouds with a gradient fallback and reduced-motion support. The old
core.ts, devices.ts and their tests remain for the legacy harness widget; they are
not the React voice transport.

## Validation

```powershell
npx -y tsx web/tests/live/agent.test.ts
uv run --with aiohttp python harness/tests/test_agent_voice.py
npm --prefix web run build
npm --prefix web run lint
```

web/tests/realtime-browser.cjs is an explicitly enabled real-provider smoke check.
It needs Playwright, LIVE_VOICE_CHECK=1, a running server (VOICE_TEST_URL), and a spoken
WAV at harness/state/realtime-voice-test.wav with a few seconds of leading silence.
It spends one short voice session, checks user and agent transcripts, nonzero
speaker energy, media playback, desktop/mobile layouts, mute and track cleanup.
It also verifies that no legacy transcription or speech endpoint was called.
