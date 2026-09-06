# Chrome page bridge

This Manifest V3 extension gives the local chatbot bounded, read-only main text from the visible Chrome tab. It supports ordinary public webpages, news stories, documentation, blog posts, dedicated X long-form Articles, and one canonical X status post at a time.

## Install

1. Start the sidecar (`./run-browser.sh`) so `http://127.0.0.1:7860` answers.
2. Open `chrome://extensions` and enable **Developer mode**.
3. Select **Load unpacked** and choose this `chrome_article_bridge` folder.
4. Open the public page you want the chatbot to read, then click the bridge
   toolbar icon once. The green check means the session is active. If the
   extension was just reloaded, the click also replaces the stale script
   inside the open tab, so no page reload is needed.

If Chrome already has an older unpacked copy from the former `demo/` folder,
remove that broken entry and load `web_app/chrome_article_bridge` instead. After
source updates, click **Reload** on the extension card and then click the bridge
toolbar icon once on the article tab; the click re-injects the new content
script without a page reload.
The expected version is **0.4.3**. A page reload also works and is never wrong.
The toolbar badge is a session switch:

- green `✓`: the bridge remains enabled across reloads, tab switches, and app focus changes
- red `!`: the local sidecar is not reachable on port 7860
- no badge: click the toolbar icon once on the page to enable the session

The icon title explains whether the current page is ready, blocked, unsupported,
or has no bounded main text. Those page states do not turn off the green session
switch. A blocked, sensitive, unsupported, or unreadable current page clears any
older cached page text so it cannot be read accidentally.

The extension automatically republishes whenever a tab becomes visible, and a
30-second heartbeat keeps the visible page fresh. Clicking the icon enables the
session (or retries the sidecar connection / republishes the visible page); it
does not disable an active session.

The enabled setting uses Chrome's session storage, so reloading the article
does not lose it. The extension checks the sidecar at startup, on page
delivery, and with a metadata-only 30-second health alarm. A failed check ends
the session and shows the red badge.

## What to ask the chatbot

- “Read/summarize/analyze the article, news, webpage, or page on my screen” uses
  the native client's `read_page` workflow, starting with this extension's DOM text.
  X/Twitter links also prefer the bridge. Other URLs start with web fetch, unless
  the assistant identifies a known login-dependent page already open in Chrome.
  Explicit bridge requests can still use `read_article` directly.
- “Check my screen,” “look at this app/window,” or requests about a layout,
  image, chart, visual appearance, or front page use an explicit Desktop
  screenshot when Desktop Control is enabled.
- “Take a screenshot” always means a screenshot.
- If the wording does not distinguish article text from visual screen state,
  the chatbot first runs a metadata-only context preflight. It checks whether
  this bridge has a fresh readable page and, only when Desktop Control is
  enabled, the frontmost app/window name. It returns no page body, labels,
  form values, or pixels and takes no screenshot. The chatbot then routes
  automatically when the context is clear, otherwise it asks one short question.

The extension itself only extracts text. The native reading workflow tries each
enabled text method at most once, including when the first result is partial.
It verifies that a bridge result matches the requested URL and retains failure
details. If text methods fail, the assistant can use Desktop screenshots and
scrolling on the requested visible page, with a six-capture limit and repeated-image
detection. Screen excerpts must be identified as partial; login forms and paywall
teasers are not the article. Clicking or typing is a separate desktop-control request.

## Safety boundary

- Only visible `http://` and `https://` tabs are considered.
- Login, password, authentication, checkout, billing, and payment contexts are blocked.
- Background tabs are not shared.
- The chatbot receiver page is never shared with itself.
- Reader-style extraction prefers semantic article/main containers, scores the strongest content region, and falls back to a cleaned page body when necessary.
- Navigation, forms, ads, related/recommended content, paywalls, and comment sections are removed where identifiable.
- Generic pages are capped at 60,000 characters.
- X Articles use their dedicated article-body boundary and exclude replies. X timelines and ordinary feed posts are not treated as complete documents.
- An individual `x.com/<user>/status/<id>` page returns only its primary post text. Replies and the surrounding timeline are excluded; image-only posts still require an explicit visual screenshot request.

The only host permission is `http://127.0.0.1:7860/*`. If the web app port changes, update `ENDPOINT` in `background.js` and the matching host permission in `manifest.json`.
