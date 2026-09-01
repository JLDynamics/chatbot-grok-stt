# Saved sessions

Saved sessions let a user create named conversation histories, reopen them later, search transcript text, and delete old chats.

## Sub-features

- `sessions-open` opens the Saved conversations modal.
- `sessions-create` starts a new empty chat.
- `sessions-list` shows prior sessions with title and preview.
- `sessions-delete` removes a session after confirmation.

## How to get to it (user POV)

- Choose the `Saved conversations` button in the top bar.
- Choose `New chat` inside the modal.
- Choose a session row to reopen it.
- Choose `Delete` on a session row and confirm the browser dialog.

## Driving it with browser-use

Preconditions:

- `doctor.sh` is green for this run.
- No session is titled `Verify session $RUN_ID`.

- **Open modal.** Choose `Saved conversations`. Click the button with accessible name `Saved conversations`. Heading `Saved conversations` appears.
- **Create session.** Choose `New chat`. Click `#new-session`. Modal closes and the transcript shows the empty state (`No messages yet`).
- **API create (alternate entry).** Run `curl -sf -X POST "$BASE_URL/api/sessions" -H 'Content-Type: application/json' -d '{"title":"Verify session RUN_ID"}'`. Response JSON includes a new `session.id`.
- **List sessions.** Reopen the modal. Run `curl -sf "$BASE_URL/api/sessions"`. Browser list and API list both include `Verify session $RUN_ID`.
- **Patch messages.** Run `curl -sf -X PATCH "$BASE_URL/api/sessions/<id>" -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","text":"verification transcript"}]}'`. File `$DATA_DIR/sessions/<id>.json` contains the message.
- **Search.** Type `verification` into `#sessions-search`. A result row appears or the region shows `No matching saved conversation.`
- **Delete.** Click `Delete` on the row and accept the confirm dialog. `GET /api/sessions` no longer lists the id.
- **Proof.** Save `sessions-list.png`, `search.png`, and `sessions-after-delete.json` under `artifacts/verify-chatbot/runs/$RUN_ID/saved-sessions/`.

## Gotchas

- Session ids are random hex strings. Take the id from the create response; do not guess filenames.
- Autosave debounces transcript writes by 500 ms. Wait briefly before asserting PATCH results from live orb use.
- Delete uses `window.confirm`. Browser automation must accept the dialog.
- Opening a session replaces the visible transcript. Reopen the modal before proving another query.
