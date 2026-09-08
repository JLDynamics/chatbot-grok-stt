# Verify Voice

Click-through and API checks for the native panel and local services.

```bash
# From this checkout:
python3 scripts/verify-voice.py
./macos/Voice/scripts/verify.sh

# Quit Voice, restart sidecar + voice from this tree, then click through:
./macos/Voice/scripts/verify.sh --cold
```

What it covers:

- Voice `/health` (or `/openapi.json` fallback on an older server)
- Sidecar config, personal memory, saved sessions, Chrome bridge status
- Web search + fetch when a search key is configured
- Panel buttons: Settings, Conversations, theme, On top, orb, End
- A short live model reply over the realtime WebSocket (`ping` → `pong`)

Screenshots land in `/tmp/voice-verify-<timestamp>/`. The tool does not overwrite personal memory or delete saved chats.
