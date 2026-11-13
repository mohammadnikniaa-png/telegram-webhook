# Telegram webhook server

Lightweight Python webhook server for @gholi_listener_bot.

## Quick-action widget (Telegram Bot API 10.4)

`quick_actions_widget.html` is a small HTML widget (3 buttons: چک کن،
صورتحساب بگیر، وضعیت سیستم) sent with `sendWidget`. It works only in the
private chat with the bot.

- Send it: POST to the outbox route (`/outbox-<OBSCURE_PATH>` or `/outbox`)
  with `{"platform": "telegram", "chat_id": 72551872, "kind": "widget"}`.
  No `text` field is needed for a widget item.
- A tap arrives as a `widget_query` update. The server checks the user is
  whitelisted, maps the tap data to the matching command text
  (`check` → چک کن, `statement` → صورتحساب بگیر, `status` → وضعیت سیستم),
  writes it to the inbox exactly like a typed message (with `"via": "widget"`),
  answers the query, and updates the widget state with the last command.
- Unknown tap data is ignored (still answered so the widget does not hang).
- Bale is untouched: widget items are Telegram-only.

Limits (Telegram): HTML ≤ 32,768 bytes raw, ≤ 4,096 bytes zlib-compressed.
The server checks both at startup and disables the widget if it is too big.

## Reply keyboard fallback (standard Telegram keyboards)

The widget is currently blocked by Telegram: `sendWidget` returns HTTP 400
`INPUT_MEDIA_WIDGET_NOT_ALLOWED`, even for a minimal widget, so this is a
rollout/permission gate on Telegram's side, not a code problem. The fallback
is a plain reply keyboard, which needs no special permission:

- Send the quick-action keyboard: POST to the outbox route
  (`/outbox-<OBSCURE_PATH>` or `/outbox`) with
  `{"platform": "telegram", "chat_id": 72551872, "kind": "keyboard"}`.
  It sends a short caption with 4 buttons in 2 rows of 2: چک کن،
  صورتحساب بگیر، وضعیت سیستم، تراکنش بفرست. A custom layout or caption
  can be given with `"keyboard": [[...], [...]]` and `"text": "..."`.
- A `"keyboard"` field can also be added to any text item; the keyboard is
  then attached to that reply via `reply_markup`.
- A tap sends the button's text as a normal message, so it arrives in the
  inbox exactly like a typed command — no intake change is needed.
- Bale is untouched: keyboard items are Telegram-only.
