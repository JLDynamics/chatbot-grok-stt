# Handoff: "read the whole article on my screen" still misbehaves

For an agent picking this up fresh. Written 2026-08-11. Everything below is from
real logs on Jack's machine, not assumed.

---

## 1. The ask, in Jack's words

> "Can you get me the full article on my screen?"

Expected: agent says one short line, goes quiet, sweeps the page top to bottom,
then explains the whole article **once**.

Actual: it narrates while scrolling, gets a partial article, and on sites that
block text it gives up until told by name to use screenshots.

## 2. Environment

- Project: `/Users/jack/Documents/chatbot` (fork of huggingface/speech-to-speech,
  renamed; package is `chatbot`, CLI is `chatbot`)
- Launch: `./run-browser.sh` → voice UI on `http://localhost:7860`, pipeline on `8766`
- LLM: `openai/gpt-5.6-luna` via OpenRouter. Vision-capable.
- Desktop control: `desktop-harness` v0.4.2 at `~/Documents/desktop-harness`,
  binary at `~/.local/bin/desktop-harness`. Accessibility granted.
- Logs: pipeline `/tmp/s2s-server.log`, web/tools `/tmp/s2s-web.log`

Reproduce: open a long X/Twitter thread in Chrome, then say
"get me the full article on my screen".

## 3. What was built (and where)

`read_article` — one tool meant to remove all model discretion.

| Piece | Location |
|---|---|
| Tool definition | `demo/main.js`, `TOOL_DEFS.read_article` |
| Tool execution | `demo/main.js`, `runTool()` → `name === "read_article"` |
| Endpoint | `demo/server.py`, `POST /api/desktop/article` |
| Text sweep script | `demo/server.py`, `_READ_FULL_SCRIPT` |
| Screenshot sweep | `demo/server.py`, `_ARTICLE_SHOT_SCRIPT` |
| Stitcher | `demo/server.py`, `_stitch()` |

Intended flow: sweep AX text page by page → if under `ARTICLE_MIN_CHARS` (600),
sweep again capturing a screenshot per page → stitch into one tall JPEG →
return once, with the image as the tool's `result.image`.

## 4. What is CONFIRMED WORKING — do not re-debug

- `read_article` **is** being selected by the model (log: `name='read_article'`)
- The endpoint runs and returns: `read_article: text sweep ok (7936 chars, 3 rounds)`
- `_stitch()` verified on synthetic input: 3 × 1512×900 → one 1100×1965 JPEG, 18 KB
- Credential guard is narrow and correct now (was blocking every page; fixed).
  Test: `PASS X thread allowed / PASS sign-in blocked / PASS 1Password blocked`
- Both files compile; generated harness scripts parse for all actions

## 5. Bug A — the sweep exits early and reports success

```
read_article: text sweep ok (7936 chars, 3 rounds)
```

**3 rounds.** The loop stops after two consecutive rounds that add no new lines
(`_READ_FULL_SCRIPT`, `dry >= 2`). On a long X thread it should take 10–30.

Hypothesis: **X virtualises the feed.** As you scroll, DOM nodes are recycled, so
`labels()` returns items already in `seen`, `fresh == 0` fires twice early, and
the loop concludes it reached the bottom. 7936 chars is plausibly just the top
of the thread.

Consequence beyond truncation: because it returns `method: "text"` and looks
successful, **the screenshot fallback never triggers** — `ARTICLE_MIN_CHARS` is
600 and 7936 sails past it.

Things worth trying:
- Detect real scroll position instead of inferring from text novelty
  (`AXScrollBar` value, or `screen_info` window bounds vs content height)
- Require N consecutive dry rounds where N scales with rounds so far
- Compare a cheap perceptual hash of successive screenshots — identical frames
  mean genuinely at the bottom; changed frames with no new labels means
  virtualised content, so keep going
- Treat "text stopped growing but the frame is still changing" as the signal to
  switch to the screenshot path mid-sweep

## 6. Bug B — the model discards the result and free-styles

Actual tool sequence from one request:

```
read_article        ← succeeded, returned 7936 chars
screen_snapshot
read_article        ← called again
control_screen      ← scroll
screen_snapshot
control_screen      ← scroll
screen_snapshot
control_screen      ← scroll
screen_snapshot
```

It got the text, then ignored it and hand-rolled a scroll+screenshot loop
anyway. **Every one of those calls produces a spoken response** — that is the
narration Jack hears, and why it feels slow.

Prompt-level steering has been tried and is insufficient:
- `read_article`'s description says to stay silent and answer once
- `control_screen`'s description says never to scroll for reading
- `web_fetch`'s failure text names `read_article` explicitly

Hypothesis: the model does not believe it has the whole article (correct — see
Bug A), so it keeps going. Fixing Bug A may dissolve Bug B. If it does not,
the tool surface itself probably needs to shrink.

Things worth trying:
- Have `read_article` return an explicit completeness claim the model can trust,
  e.g. `"reached_bottom": true` plus "this is the complete article, do not scroll
  or screenshot further"
- Hide `control_screen`/`screen_snapshot`/`read_screen` while a `read_article`
  turn is in flight, so free-styling is impossible rather than discouraged
- Return the stitched image *and* the text together so there is nothing left to
  go looking for

## 7. Bug C — narration between tool calls (architectural)

Each tool call yields a spoken turn. Any multi-call strategy is therefore
audible. This is why the fix direction has been "one call does everything".

Worth checking whether the Realtime layer can suppress speech between chained
tool calls, in `src/chatbot/api/openai_realtime/handlers/response.py`.

## 8. Definition of done

Say "get me the full article on my screen" on a long X thread and get:

1. one short acknowledgement
2. silence while it works
3. one answer covering the **whole** thread, including the end
4. no manual scrolling, no per-screen commentary
5. same behaviour without the user ever mentioning screenshots

## 9. Notes

- `grep read_article /tmp/s2s-web.log` shows which path fired: `text sweep ok`
  vs `falling back to screenshots` vs `stitched N screenshots`
- `grep -oE "name='[a-z_]+'" /tmp/s2s-server.log | tail -20` shows tool sequence
- Restart `./run-browser.sh` after server changes; hard-refresh (Cmd+Shift+R)
  after client changes — asset version is in `demo/index.html` (`?v=17`)
- Screenshot sweep path has **never actually run** on a real page, because the
  text sweep always claims success. It is verified only on synthetic input.
