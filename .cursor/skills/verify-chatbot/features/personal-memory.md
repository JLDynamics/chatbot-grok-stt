# Personal memory

Personal memory is the durable Markdown profile in Tools. It loads into every new conversation and can be edited manually or via the assistant's remember/forget tools.

## Sub-features

- `memory-open` opens the Tools modal and shows the profile editor.
- `memory-edit` updates the textarea contents.
- `memory-save` persists the profile to the server.
- `memory-reload` shows the saved profile after a page refresh.

## How to get to it (user POV)

- Choose the `Tools` button in the top bar, then edit **Personal memory** and choose `Save profile`.
- Say "remember …" during a voice or text turn (assistant tool path; needs an active session).

## Driving it with browser-use

Preconditions:

- `doctor.sh` is green for this run.
- `DATA_DIR/personal-memory.md` is empty or does not contain the verification marker yet.

- **Open Tools.** Choose `Tools`. Run `browser_exec` to click the button with accessible name `Tools`. Dialog heading `Tools` appears and `#personal-memory-editor` is visible.
- **Enter profile text.** Type a unique marker such as `verify-profile-$RUN_ID` plus a short sentence. Run `js('document.querySelector("#personal-memory-editor").value = "...")` or type into the textarea. `#personal-memory-count` updates.
- **Save profile.** Choose `Save profile`. Run a click on `#personal-memory-save`. `#personal-memory-count` shows `characters saved`.
- **API read-back.** Run `curl -sf "$BASE_URL/api/personal-memory"`. JSON `content` includes the marker.
- **File read-back.** Run `cat "$DATA_DIR/personal-memory.md"`. File content includes the marker.
- **Reload proof.** Refresh the tab, reopen Tools, and confirm the textarea still shows the marker.
- **API shortcut.** Run `.cursor/skills/verify-chatbot/scripts/prove-personal-memory.sh "$STATE_DIR/state.env"` when browser driving is unnecessary. It writes `before.json`, `put-response.json`, `after.json`, and `proof.txt` under `artifacts/.../personal-memory/`.

## Gotchas

- The editor loads on Tools open via `GET /api/personal-memory`. Saving without opening Tools first is valid only on the API path.
- `PUT` rejects content longer than `max_chars` (10,000). Keep verification text short.
- This feature writes to `DATA_DIR`, not `localStorage`. Clearing browser storage does not erase saved profile text.
- Remove verification markers during fixture cleanup, but keep proof artifacts.
