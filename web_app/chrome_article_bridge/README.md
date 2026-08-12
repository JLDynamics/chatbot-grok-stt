# Chrome page bridge

This Manifest V3 extension gives the local chatbot bounded, read-only main text from the visible Chrome tab. It supports ordinary public webpages, news stories, documentation, blog posts, dedicated X long-form Articles, and one canonical X status post at a time.

## Install

1. Start the chatbot on `http://127.0.0.1:7860`.
2. Open `chrome://extensions` and enable **Developer mode**.
3. Select **Load unpacked** and choose this `chrome_article_bridge` folder.
4. Open or reload the chatbot at `http://127.0.0.1:7860`. The bridge detects the
   receiver and enables itself automatically; the green check means the session
   is active.
5. Open or reload the public page you want the chatbot to read. No extension
   click is required.

If Chrome already has an older unpacked copy from the former `demo/` folder,
remove that broken entry and load `web_app/chrome_article_bridge` instead. After
source updates, click **Reload** on the extension card and reload the article tab.
The expected version is **0.4.1**. Reloading the extension invalidates the old
content script already inside open tabs, so the page reload is required.

The toolbar badge is a session switch:

- green `✓`: the bridge remains enabled across reloads, tab switches, and app focus changes
- red `!`: the local chatbot is not reachable on port 7860
- no badge: the chatbot receiver tab is not open

The icon title explains whether the current page is ready, blocked, unsupported,
or has no bounded main text. Those page states do not turn off the green session
switch. A blocked, sensitive, unsupported, or unreadable current page clears any
older cached page text so it cannot be read accidentally.

The extension automatically republishes whenever a tab becomes visible. When
you switch directly from a readable page to the chatbot tab, it preserves that
page for the server's five-minute TTL so `read_article` can consume it. Switching
to another webpage removes the old page immediately. Main-content changes and a
30-second heartbeat keep the visible page fresh. Clicking the icon only retries
the receiver connection or republishes the visible page; it does not disable an
active chatbot session.

The enabled setting uses Chrome's browser-session storage, so refreshing either
the article or chatbot page does not lose it. Opening the exact local receiver
root address (`/` or `/index.html`) activates it; unrelated localhost pages and
other paths do not. Closing or navigating
the chatbot tab away, or ending the Chrome session, disables it and clears cached
text. The extension cannot receive an instant process-shutdown event from the
local server; instead it checks the receiver at startup, when the receiver opens,
on page delivery, and with a metadata-only 30-second extension health alarm. A
failed check ends the session and shows the red badge.

## What to ask the chatbot

- “Read/summarize/analyze the article, news, webpage, or page on my screen” uses
  `read_article` and this extension's full DOM text.
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

Article extraction never falls back automatically to a Desktop screenshot.
Reading a supported public page is read-only and does not require a second approval
after the user asks. If this bridge is unavailable, the chatbot reports that state
and asks for an extension/page reload; it must not substitute a screenshot.

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
