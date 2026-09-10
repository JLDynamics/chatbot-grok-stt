# Verify Voice

Click-through and API checks for the native panel and local services.

```bash
# From this checkout:
python3 scripts/verify-voice.py
./macos/Voice/scripts/verify.sh

# Live server-side search/read_page and a Chinese-name TTS turn (no UI tour):
python3 scripts/verify-voice.py --skip-ui --research

# Quit Voice, restart sidecar + voice from this tree, then click through:
./macos/Voice/scripts/verify.sh --cold
```

What it covers:

- Voice `/health`, including source fingerprint, `stale`, and `server_tools`
- Sidecar `/api/config` with the same current-code contract
- Personal memory, saved sessions, Chrome bridge status
- Web search + fetch when a search key is configured
- Panel buttons: Settings, Conversations, theme, On top, orb, End
- A short live model reply over the realtime WebSocket (`ping` → `pong`)
- With `--research`: dated news RSS checks, a turn that must `bash`/`curl` a
  page on the server, a verify-first turn that must research without being
  told the command, and a mixed-script TTS turn (`华为` on the Mandarin pipeline)

Screenshots land in `/tmp/voice-verify-<timestamp>/`. The tool does not overwrite personal memory or delete saved chats.
