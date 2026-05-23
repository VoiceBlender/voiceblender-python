# voiceblender-python

> [!IMPORTANT]
> **This library is auto-generated.** Models, request/response/event types, and
> method bindings are produced by `tools/generate.py` from the OpenAPI/AsyncAPI
> specs (see [Code generation](#code-generation)). Do **not** edit the generated
> sources directly — your changes will be overwritten on the next run. Open pull
> requests against the generator (`tools/generate.py`) and/or the upstream specs
> only.

Python client for the [VoiceBlender](https://voiceblender.com) API.

VoiceBlender bridges SIP and WebRTC voice calls with multi-party audio mixing,
real-time speech-to-text, text-to-speech, AI agent integration, recording, and
webhook-based event delivery.

This is a Python port of [voiceblender-go](https://github.com/VoiceBlender/voiceblender-go).
Models, request types, response types, event types, and method bindings are
**code-generated** from `../VoiceBlender/openapi.yaml` + `asyncapi.yaml` by
`tools/generate.py` — the same spec-driven pattern as the Go SDK.

## Installation

```bash
pip install voiceblender
```

## Quick start (async)

```python
import asyncio
import voiceblender

async def main():
    async with voiceblender.Client(base_url="http://localhost:8080/v1") as c:
        leg = await c.create_leg(voiceblender.CreateLegRequest(
            type=voiceblender.LegType.SIP_OUTBOUND,
            to="sip:alice@example.com",
        ))
        print("Created leg:", leg.id)

asyncio.run(main())
```

## Quick start (sync)

```python
from voiceblender.sync import SyncClient
import voiceblender

with SyncClient(base_url="http://localhost:8080/v1") as c:
    leg = c.create_leg(voiceblender.CreateLegRequest(
        type=voiceblender.LegType.SIP_OUTBOUND,
        to="sip:alice@example.com",
    ))
    print("Created leg:", leg.id)
```

## Code generation

```bash
make generate   # regenerate from openapi.yaml + asyncapi.yaml
make test       # run pytest
make lint       # ruff check
make typecheck  # mypy --strict
```

## License

MIT. See [LICENSE](LICENSE).
