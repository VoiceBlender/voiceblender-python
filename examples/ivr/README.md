# Example: Company IVR (VSI / Python)

A multi-department IVR (Interactive Voice Response) that answers inbound calls,
greets the caller with a TTS prompt, presents a DTMF menu, and routes the
caller to a department room — wired up over a **single outbound WebSocket** to
VoiceBlender's `/v1/vsi` endpoint.

This is a VSI redesign of [`../../../voiceblender-go/examples/ivr`](../../../voiceblender-go/examples/ivr),
which uses REST + HTTP webhooks. The behaviour is identical; the I/O substrate
is different.

## Call flow

```
Inbound call
  └─ leg.ringing      → early media: UK ringback for 3 s → answer
  └─ leg.connected    → "Thank you for calling Acme Corp. Please hold…"
  └─ tts.finished     → main menu prompt
  └─ dtmf.received
       1 → Sales queue
       2 → Support queue
       3 → Billing queue
       0 → Operator queue (Deepgram AI agent)
       9 → Repeat menu
       * → Goodbye → hang up
       ? → "Invalid option, please try again" (max 3 attempts, then goodbye)
  └─ leg.disconnected → cleanup
```

Once a caller is routed, they are added to the department's persistent room
where agents can join to handle the call. Hold music is played in the room
while they wait.

## Architecture

```
SIP carrier
    │  inbound INVITE
    ▼
VoiceBlender                      ◄──── outbound WS ◄────  IVR (this program)
    │  events (leg.ringing, dtmf.received, tts.finished, …)
    │
    ▼                             ──── VSI command frames ──►
(same WebSocket, bidirectional)

                                  ◄──── <cmd>.result ────
```

No inbound HTTP server. No public DNS. No ngrok. The IVR is a plain WebSocket
client — deployable behind any firewall that allows outbound connections to
VoiceBlender.

## Prerequisites

- A running [VoiceBlender](https://github.com/VoiceBlender/voiceblender) instance
  reachable on the network from this host
- An [ElevenLabs](https://elevenlabs.io) API key for TTS prompts, unless already
  configured in VoiceBlender
- Python 3.10+

## Configuration

| Environment variable | Required | Default | Description |
|----------------------|----------|---------|-------------|
| `VOICEBLENDER_URL`   | no       | `http://localhost:8080/v1` | VoiceBlender API base URL |
| `TTS_API_KEY`        | no       | — | TTS API key (omit if pre-configured in VoiceBlender) |
| `TTS_VOICE`          | no       | `Rachel` | TTS voice name |
| `TTS_PROVIDER`       | no       | `elevenlabs` | TTS provider name |
| `DEEPGRAM_API_KEY`   | no       | — | Deepgram API key for the operator AI agent |
| `COMPANY_NAME`       | no       | `Acme Corp` | Company name spoken in greeting |

See [`.env.example`](.env.example) for a copy-paste-ready template.

## Running

From the `voiceblender-python` repo root:

```bash
pip install -e ".[dev]"            # one-time
python examples/ivr/main.py
```

That's it. No port to forward, no tunnel to set up.

VoiceBlender must be configured to send inbound SIP calls to the same
instance. The IVR creates the four department rooms (`sales`, `support`,
`billing`, `operator`) on startup if they don't already exist — using the VSI
`create_room` command, no webhook registration required.

## Code structure

Each active call is a `Call` dataclass holding the current IVR state
(`GREETING → MENU → ROUTED/GOODBYE`). Events arrive over the WebSocket and
are dispatched to `asyncio.create_task` handlers so the read loop never
blocks; all state transitions are protected by a per-call `asyncio.Lock`.

| Old (REST + webhook) | New (VSI) |
|----------------------|-----------|
| `aiohttp.web.Application` + `/webhook` route | `client.events_stream()` + `client.subscribe()` |
| `voiceblender.Client._do(...)` (HTTP) | `stream.<vsi_method>(...)` (WS) |
| `leg.early_media(...)` / `leg.play(...)` / … | `stream.leg_early_media(payload)` / `stream.leg_play_start(payload)` / … |
| `room.add_leg(...)` / `room.play(...)` | `stream.add_leg_to_room(payload)` / `stream.room_play_start(payload)` |
| `client.create_room(...)` | `stream.create_room(CreateRoomRequest(...))` |

The TTS sequencing trick is identical: each call tracks its `active_tts_id`
and `tts.finished` events for replaced prompts are silently discarded.

## Reconnect

`main.py` wraps the stream loop in a simple `while True: try / except +
asyncio.sleep(5)` block. On any disconnect (network blip, server restart) the
IVR drops its in-memory call state and reconnects after a fixed 5-second
delay. In-flight calls that survive the disconnect would have to re-ring; that
matches what the server already does on its side.

A production deployment should swap the fixed delay for exponential backoff
and may want to reconcile per-call state across reconnects via `list_legs` /
`list_rooms` VSI commands.
