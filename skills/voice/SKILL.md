---
name: voice
description: Start talk mode. Hear Ying through Luna, think here, speak the answer. Use when the user says /voice, voice mode, or talk.
---

# /voice

Pi is the face. Luna is ears and mouth only.

From this repo:

```bash
python3 /Users/jack/Documents/chatbot-grok-stt-orchestrator/skills/voice/voice-loop.py
```

Build Voice first if the binary is missing:

```bash
/Users/jack/Documents/chatbot-grok-stt-orchestrator/macos/Voice/scripts/build.sh
```

The loop starts headless Voice (which starts `run-browser.sh --reuse-running` when ports are free). Speak. Pi answers through Siri. Say `quit` or send SIGINT to stop. Voice then stops services it started.

Do not call Luna tools. Use this session's tools (browser-control, pi-crew, bash) when the spoken question needs them. Memory for this talk lives in the `voice-talk` Pi session (`VOICE_PI_SESSION` overrides the name).
