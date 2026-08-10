# Learning Guide: `speech-to-speech`

A map of this codebase for someone who knows Python but is new to voice-agent /
audio-ML systems. Read this alongside the source — every claim below points at a
real file you can open.

> Not an official project doc. Written as an orientation aid.

---

## 1. What this project actually is

A **voice agent**: you talk, it thinks, it talks back. Built by Hugging Face,
Apache-2.0, published on PyPI as `speech-to-speech`, currently v0.2.12
(`pyproject.toml`).

Two things make it interesting as a codebase to learn from:

1. **It's a cascade of four swappable models**, not one giant model.
2. **It speaks a protocol you don't own** — the OpenAI Realtime API. That means
   any client already written for OpenAI's realtime voice service can point at
   *your* server instead, with no client changes. That's the whole pitch, and
   it's what the GIF in the README is showing.

The four stages:

```
your voice  →  VAD  →  STT  →  LLM  →  TTS  →  its voice
              (when   (what  (what   (say it
               you     you    to      out
               spoke)  said)  reply)  loud)
```

| Stage | Full name | Job | Default model |
|---|---|---|---|
| VAD | Voice Activity Detection | Find where speech starts/stops in the mic stream | Silero VAD v5 |
| STT | Speech To Text | Transcribe the utterance | Parakeet TDT |
| LLM | Language Model | Generate the reply text | any OpenAI-compatible endpoint |
| TTS | Text To Speech | Synthesize reply audio | Qwen3-TTS |

Defaults live in `src/speech_to_speech/arguments_classes/module_arguments.py`.

---

## 2. Repo map

```
speech-to-speech/
├── src/speech_to_speech/        ← THE LIBRARY. 99% of your reading happens here.
│   ├── cli.py                   ← command-line entry point
│   ├── s2s_pipeline.py          ← assembles everything (the "main")
│   ├── baseHandler.py           ← the one abstraction to understand first
│   ├── backend_registry.py      ← the catalogue of swappable models
│   ├── VAD/  STT/  LLM/  TTS/   ← the four stages, one folder each
│   ├── pipeline/                ← message types + shared coordination state
│   ├── api/openai_realtime/     ← the server: WebSocket/WebRTC, protocol translation
│   ├── arguments_classes/       ← one dataclass per component's config flags
│   └── utils/
├── demo/                        ← browser voice-chat UI (separate app, JS + FastAPI)
├── tests/                       ← pytest suite; also excellent documentation
├── scripts/                     ← benchmarking helpers
├── archive/                     ← retired backends, not wired into the CLI
├── examples/, docs/, assets/
├── pyproject.toml               ← deps, entry point, ruff/mypy/pytest config
├── README.md                    ← user-facing docs (long, good)
└── AGENTS.md                    ← repo conventions + release process
```

**Skip on first pass:** `archive/`, `demo/`, `scripts/`, `docs/`.

Also note the per-folder READMEs — `LLM/README.md`, `STT/README.md`,
`TTS/README.md`, and especially **`api/openai_realtime/README.md`**, which has a
Mermaid architecture diagram. Read that one early.

---

## 3. The one abstraction to understand first

**`src/speech_to_speech/baseHandler.py`** (161 lines — read the whole thing).

Every pipeline stage is a `BaseHandler`. The design is deliberately plain:

```python
class BaseHandler(Generic[InT, OutT]):
    def setup(self, *args, **kwargs): ...      # load the model, once
    def process(self, input) -> Iterator[OutT]: ...  # do the work, yield results
    def run(self):                             # the loop, provided for you
        while not self.stop_event.is_set():
            item = self.queue_in.get(timeout=0.1)
            for output in self.process(item):
                self.queue_out.put(output)
```

So the mental model is:

- **Each stage is a thread.** Started by `utils/thread_manager.py` → `ThreadManager.start()`.
- **Stages are connected by `queue.Queue` objects.** Stage N's `queue_out` *is*
  stage N+1's `queue_in`. Nothing else connects them.
- **`process()` is a generator.** This is the key to low latency: the LLM yields
  a sentence at a time, so TTS can start speaking sentence 1 while the LLM is
  still writing sentence 2. Nothing waits for a full response.

Why threads instead of `async`? The heavy stages are model inference (PyTorch /
MLX), which releases the GIL and blocks. Threads + queues keep that simple. The
*network* layer is async (`api/openai_realtime/`), and the queue is the seam
between the two worlds.

Two special values flow on the queues:

- `PIPELINE_END` (a `bytes` sentinel) — "shut down", prevents deadlock on exit.
- `SESSION_END` (a control message) — "new caller, reset your state", but keep
  the thread and the loaded model alive. See `pipeline/control.py`.

---

## 4. Entry points

`pyproject.toml` declares:

```toml
[project.scripts]
speech-to-speech = "speech_to_speech.cli:main"
```

So `speech-to-speech <cmd>` → `cli.py:main()` (line 156). Three commands:

| Command | What runs | Entry function |
|---|---|---|
| `serve` | The server only. Clients connect over WebSocket/WebRTC. | `s2s_pipeline.py:run_pipeline_command` → `build_pipeline` (line 541) |
| `talk` | Mic/speaker client only — connects to *some* realtime URL | `api/openai_realtime/audio_client.py:run_realtime_audio_client` |
| `local` | Both, wired over loopback. Easiest way to try it. | `s2s_pipeline.py:build_local_pipeline` (line 581) |

Default server address: `ws://127.0.0.1:8765/v1/realtime`
(`arguments_classes/realtime_server_arguments.py`).

Note the deliberate import laziness in `cli.py`: `talk` never imports
`s2s_pipeline`, so the client starts instantly instead of pulling in torch.
Small detail, worth copying in your own CLIs.

---

## 5. Startup, traced

`s2s_pipeline.py` is the file to read after `baseHandler.py`. The chain:

```
run_pipeline_command(command, argv)          # line 606
└── parse_arguments(argv)                    # line 170 — HfArgumentParser over the
│                                            #   dataclasses in arguments_classes/
└── build_pipeline(args, stop_event)         # line 541
    ├── _build_pipeline_unit(index=0..N)     # line 452 — one self-contained pipeline
    │   ├── creates 8 Queues                 # lines 480-488
    │   ├── creates RealtimeService          # per-session conversation state
    │   └── _build_handlers(...)             # line 348 — instantiates the stages
    │       → [VAD, STT, TranscriptionNotifier, LLM, LMOutputProcessor, TTS]
    ├── RealtimeServer(pool=[units...])      # one uvicorn, routes clients to free units
    └── ThreadManager(all handlers + server) # line 578
```

Then `.start()` spawns one thread per handler and `.wait()` joins them.

**`num_pipelines`** is how concurrency works here: N complete copies of the
VAD/STT/LLM/TTS stack, each with its own queues and conversation state, sitting
behind one HTTP port. Client #1 gets unit 0, client #2 gets unit 1. Beyond N,
connections are rejected. Blunt, but it sidesteps every shared-model-state
problem, and models are the memory bottleneck anyway.

---

## 6. One voice turn, end to end

This is the most useful thing to hold in your head. Follow it once with the
files open.

**1 — Audio arrives.**
Client sends `input_audio_buffer.append` (base64 PCM) over WebSocket.
`api/openai_realtime/websocket_router.py` parses it; `service.py` decodes,
resamples to **16 kHz**, and slices into **512-sample chunks** (~32 ms) — the
window size Silero VAD expects. Chunks go onto `recv_audio_chunks_queue`.

**2 — VAD decides you're talking.** (`VAD/vad_handler.py`, `VAD/vad_iterator.py`)
Silero scores each chunk for speech probability. On a rising edge it emits
`speech_started`; after enough trailing silence, `speech_stopped`, and pushes the
buffered utterance as a `VADAudio` message. This stage owns turn-taking — the
hardest UX problem in voice agents, which is why it's ~840 lines for something
that sounds like a boolean.

**3 — STT transcribes.** (`STT/parakeet_tdt_handler.py`, base in `STT/base_stt_handler.py`)
Audio → text. With live transcription on (the default), it also emits
**partial** transcripts every 500 ms while you're still speaking, so the UI can
show words appearing in real time.

**4 — Notification.** (`STT/transcription_notifier.py`)
Turns transcripts into protocol events (`transcription.delta` /
`.completed`) on the side-channel `text_output_queue`, and forwards the final
text onward.

**5 — LLM generates.** (`LLM/language_model.py`, `LLM/responses_api_language_model.py`)
`RealtimeService` builds a `GenerateResponseRequest` with conversation history
(`LLM/chat.py`) and the model streams back `LLMResponseChunk`s — chunked at
sentence boundaries, via NLTK's `punkt` tokenizer, precisely so TTS can start early.

**6 — Output splitting.** (`LLM/lm_output_processor.py`)
A small, very readable handler that forks the stream: clean prose → TTS;
tool calls and `assistant_text` events → the side channel to the client.

**7 — TTS speaks.** (`TTS/qwen3_tts_handler.py`)
Text → PCM chunks onto `send_audio_chunks_queue`.

**8 — Back out.**
The router's async `_send_loop` drains both the audio queue and the event queue,
base64-encodes audio as `response.output_audio.delta` events, and ships
everything to the client.

**And the interrupt case:** if you start talking mid-answer ("barge-in"), VAD
fires while TTS is still playing. `pipeline/cancel_scope.py` bumps a generation
counter; every handler checks it in `should_process_input()` (see
`baseHandler.py:54`) and silently drops now-obsolete in-flight work. That's how
audio already sitting in three queues gets thrown away without tearing down
threads.

---

## 7. Swappable backends

`backend_registry.py` is the catalogue. Each entry is a `BackendSpec` binding a
CLI name → its arguments dataclass → a factory that lazily imports the handler
class.

```
STT: none, whisper, whisper-mlx, mlx-audio-whisper, faster-whisper, parakeet-tdt, paraformer
LLM: transformers, mlx-lm, responses-api, chat-completions
TTS: chatTTS, facebookMMS, pocket, kokoro, qwen3
```

Chosen at runtime: `--stt parakeet-tdt --llm_backend responses-api --tts qwen3`.

Three details worth noticing, because they're reusable patterns:

- **Lazy imports** (`_load_handler`, line 194). Selecting `kokoro` shouldn't
  force you to have `chattts` installed. The import happens at construction time,
  and a missing optional dep is translated into a friendly "run `pip install
  speech-to-speech[kokoro]`" error (`_optional_dependency_error`, line 171).
- **`BackendCapabilities`** (line 50). Instead of `if backend_name == "...":`
  scattered around, backends declare flags like `supports_audio_input` and
  `bypasses_transcription_notifier`, and the wiring code branches on those.
  `_build_handlers` uses exactly this to decide whether a `TranscriptionNotifier`
  is needed at all.
- **`--stt none`** removes a whole stage: raw audio goes straight to an
  audio-capable LLM. The cascade is optional.

---

## 8. Vocabulary you'll hit

| Term | Meaning |
|---|---|
| **PCM** | Raw uncompressed audio samples. No decoding needed, just numbers. |
| **16 kHz mono** | 16,000 samples/sec, one channel. Standard for speech models. |
| **Chunk / frame** | A fixed slice of audio, here 512 samples ≈ 32 ms. |
| **Utterance / turn** | One continuous stretch of a person speaking. |
| **Barge-in** | User interrupts the agent mid-reply; the agent must stop. |
| **Streaming** | Emitting partial results as they're produced, not at the end. Where all the latency wins come from. |
| **Speculative turn** | VAD guesses you're done, work starts, then you keep talking — so the turn is *reopened* and the speculative work discarded. `pipeline/speculative_turns.py`. |
| **MLX / MPS** | Apple Silicon compute backends. MLX is Apple's ML framework; MPS is PyTorch's Metal GPU device. |
| **Realtime API** | OpenAI's bidirectional voice protocol. This project reimplements the server side. |

---

## 9. Where to poke

Ordered easiest → hardest.

1. **Run it.** `speech-to-speech local` after `export OPENAI_API_KEY=...`. Then
   `--log_level debug` and watch the handlers announce themselves. The timing
   logs in `baseHandler.run()` show you exactly which stage is slow.
2. **Change the personality.** `--init_chat_prompt "You are a pirate."` The
   system prompt threading is in `LLM/chat.py`.
3. **Swap a voice or a model.** `--tts kokoro`, `--stt whisper`. See how far a
   registry entry gets you.
4. **Read a whole small handler.** `LLM/lm_output_processor.py` (148 lines) is
   the best one — small, commented, and it makes the queue pattern click.
5. **Read the tests.** `tests/` is the real documentation. `test_chat.py`,
   `test_lm_output_processor.py`, and `test_vad_iterator.py` each isolate one
   idea. Run `uv run pytest tests/test_chat.py -v`.
6. **Add a TTS backend.** The genuine exercise: subclass `BaseHandler`, add a
   `BackendSpec` to `TTS_BACKENDS`, add an arguments dataclass. Copy
   `TTS/facebookmms_handler.py` (222 lines) as the template — it's the simplest.

---

## 10. Suggested reading order

```
1. README.md — "How it works" section only
2. api/openai_realtime/README.md — the Mermaid diagram
3. baseHandler.py — all of it
4. pipeline/messages.py + pipeline/handler_types.py — what flows between stages
5. cli.py
6. s2s_pipeline.py — _build_handlers() and _build_pipeline_unit()
7. LLM/lm_output_processor.py — one complete handler
8. backend_registry.py
9. Pick one stage folder and go deep
```

Steps 1–7 are maybe two focused hours and get you genuinely oriented.

---

## 11. Dev workflow

```bash
uv sync --group dev          # install with dev deps
uv run pytest tests/ -x -q   # tests
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
```

CI (`.github/workflows/ci.yml`) runs exactly these four, plus a package build.
Line length 120; `ruff` lints `E,F,I,W`. Release process is in `AGENTS.md`
(tag `v*` → GitHub Actions publishes to PyPI).

---

## 12. Design decisions worth stealing

Things this codebase does that are worth recognizing in the wild:

- **Uniform stage interface.** Every stage is the same shape, so adding one is
  mechanical rather than architectural.
- **Generators for streaming.** `process()` yielding instead of returning is a
  one-word change that buys pipelined latency across the whole system.
- **Typed messages, not tuples.** `pipeline/messages.py` opens by saying it
  replaced "ad-hoc tuples". Pydantic models with a `tag` discriminator — the
  boring choice that pays off every time you debug.
- **Capability flags over name checks.** Behavior is declared with the backend,
  not scattered through the wiring.
- **Copy-per-unit isolation.** `_build_pipeline_unit` deep-copies configs
  because third-party libraries mutate their arguments. That comment (line 470)
  is a scar from a real bug.
- **Implementing someone else's protocol.** Adopting the OpenAI Realtime API
  meant inheriting an entire client ecosystem for free.
