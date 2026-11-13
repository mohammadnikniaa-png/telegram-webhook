#!/usr/bin/env python3
"""
Telegram webhook server - lightweight, no LLM, no Muse tokens while idle.
- Listens on 0.0.0.0:8443 (put behind TLS on 443, or use a reverse proxy)
- Telegram POSTs updates here; no polling loop at all
- Only user_id 72551872 is accepted (whitelist)
- Writes inbox.jsonl with inbox_at timestamp
- Sends immediate auto-reply: "دریافت شد ✅ در صف پاسخ"
- Sends outbox.jsonl replies, split to 4000 chars
- Token is NEVER in this file. Read from /etc/telegram-listener.env
  (TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET_TOKEN, ALLOWED_USER_IDS)
- Verify Telegram's X-Telegram-Bot-Api-Secret-Token header if WEBHOOK_SECRET_TOKEN is set
"""
import json, os, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox.jsonl")
OUTBOX = os.path.join(BASE, "outbox.jsonl")

def load_env():
    env = {}
    for path in ("/etc/telegram-listener.env", os.path.join(BASE, ".env")):
        try:
            for line in open(path):
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
    env.update(os.environ)
    return env

ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
SECRET = ENV.get("WEBHOOK_SECRET_TOKEN", "")
# Obscure-path inbox read: set in host Settings only, never in code.
# Serves the same inbox as /inbox, but at /inbox-<OBSCURE_PATH> with no
# auth header. The path itself is the secret, so it must be long/random.
# Polling, Bale, /inbox (secret) and /outbox are unchanged.
OBSCURE_PATH = ENV.get("OBSCURE_PATH", "")
ALLOWED = set(x.strip() for x in ENV.get("ALLOWED_USER_IDS", "72551872").split(",") if x.strip())
BALE_TOKEN = ENV.get("BALE_BOT_TOKEN", "")
BALE_ALLOWED = set(x.strip() for x in ENV.get("BALE_ALLOWED_USER_IDS", "63920689").split(",") if x.strip())

# --- Quick-action widget (Telegram Bot API 10.4, Telegram-only; Bale untouched) ---
# One small HTML widget with three buttons: check / statement / status.
# A tap arrives as a widget_query update; it is turned into an inbox item
# with the matching command text, so the main agent answers it exactly like
# a typed message. The widget is sent via the outbox with kind="widget".
WIDGET_COMMANDS = {"check": "چک کن", "statement": "صورتحساب بگیر", "status": "وضعیت سیستم"}
WIDGET_CAPTION = "دکمه‌های سریع — بزن تا دستور برسه: چک کن، صورتحساب بگیر، وضعیت سیستم"

def _load_widget_html():
    try:
        with open(os.path.join(BASE, "quick_actions_widget.html"), encoding="utf-8") as f:
            html = f.read()
        import zlib
        raw = len(html.encode("utf-8"))
        comp = len(zlib.compress(html.encode("utf-8"), 9))
        if raw > 32768 or comp > 4096:
            print(f"widget html too large: raw={raw} compressed={comp}", flush=True)
            return ""
        return html
    except FileNotFoundError:
        return ""

WIDGET_HTML = _load_widget_html()

# --- Reply keyboard fallback (standard Telegram keyboards; Bale untouched) ---
# The HTML widget is blocked by Telegram (sendWidget -> 400
# INPUT_MEDIA_WIDGET_NOT_ALLOWED, even for a minimal widget), so a plain
# reply keyboard is the fallback. A tap sends the button's text as a normal
# message, which arrives in the inbox exactly like a typed command — no
# intake change is needed. The keyboard is sent via the outbox with
# kind="keyboard", or as a "keyboard" field alongside "text".
QUICK_KEYBOARD = [["چک کن", "صورتحساب بگیر"], ["وضعیت سیستم", "تراکنش بفرست"]]
KEYBOARD_TEXT = "دکمه‌های سریع 👇"

def _keyboard_markup(rows):
    """Build a reply_markup dict from button rows, or None if malformed."""
    if not isinstance(rows, list):
        return None
    clean = []
    for row in rows:
        if not isinstance(row, list):
            return None
        clean_row = [b for b in row if isinstance(b, str) and b]
        if not clean_row:
            return None
        clean.append(clean_row)
    if not clean:
        return None
    return {"keyboard": clean, "resize_keyboard": True, "one_time_keyboard": False}

def _outbox_accept(item):
    """Accept an outbox item: it needs a chat_id and something to send
    (text, a keyboard, or a widget/keyboard kind)."""
    return bool(item.get("chat_id")) and bool(
        item.get("text") or item.get("keyboard")
        or item.get("kind") in ("widget", "keyboard"))

def _tg_call(method, params):
    if not TOKEN:
        return {"ok": False, "description": "no token"}
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Telegram answers errors as HTTP 4xx with a JSON body:
        # {"ok": false, "error_code": 400, "description": "Bad Request: ..."}.
        # The description names the real problem (e.g. which field Telegram
        # rejected); str(e) alone is only "HTTP Error 400: Bad Request" and
        # drops it. Log the full body so the FadeHost logs show the cause.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        desc = ""
        try:
            desc = json.loads(body).get("description", "") if body else ""
        except Exception:
            pass
        detail = desc or body[:500]
        print(f"telegram {method} error: HTTP {e.code} {detail}", flush=True)
        return {"ok": False, "error_code": e.code, "description": detail}
    except Exception as e:
        print(f"telegram {method} error:", e, flush=True)
        return {"ok": False, "description": str(e)}

def send_widget(chat_id):
    """Send the quick-action widget to a private Telegram chat."""
    if not TOKEN or not WIDGET_HTML:
        return {"ok": False, "description": "widget unavailable"}
    return _tg_call("sendWidget", {
        "chat_id": chat_id,
        "html": WIDGET_HTML,
        "chat_data": json.dumps({"last": None}, ensure_ascii=False),
        "allow_queries": True,
        "caption": WIDGET_CAPTION,
    })

def _handle_widget_query(query):
    """Turn a widget tap into an inbox command, then ack the widget."""
    user = query.get("from", {})
    if str(user.get("id")) not in ALLOWED:
        return
    # WidgetQuery.type is a WidgetQueryType OBJECT, e.g. {"type": "data"},
    # not a plain string (Bot API 10.4). Only data queries carry a tap
    # command; wallet queries (sign_data / sent_transaction) are ignored.
    qtype = query.get("type") or {}
    if isinstance(qtype, dict):
        qtype = qtype.get("type")
    if qtype not in (None, "data"):
        return
    data = query.get("data") or ""
    cmd = WIDGET_COMMANDS.get(data)
    if not cmd:
        # Unknown/untrusted tap data: still ack so the widget does not hang.
        _tg_call("answerWidgetQuery", {"widget_query_id": query.get("id"),
                                       "data": json.dumps({"ok": False}, ensure_ascii=False)})
        return
    chat = query.get("chat", {})
    chat_id = chat.get("id")
    with open(INBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps({"platform": "telegram", "chat_id": chat_id,
            "user_id": user.get("id"), "text": cmd, "via": "widget",
            "inbox_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")
    # Ack first (15s window), then update the widget state (best effort).
    _tg_call("answerWidgetQuery", {"widget_query_id": query.get("id"),
        "data": json.dumps({"ok": True, "cmd": cmd}, ensure_ascii=False)})
    _tg_call("editMessageWidgetData", {"chat_id": chat_id,
        "message_id": query.get("message_id"),
        "chat_data": json.dumps({"last": cmd, "at": time.strftime("%H:%M")}, ensure_ascii=False)})
    flush_outbox()

def send_message(chat_id, text, reply_markup=None):
    if not TOKEN:
        return
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for idx, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if reply_markup is not None and idx == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

def send_keyboard(chat_id, rows=None, text=None):
    """Send the quick-action reply keyboard (Telegram-only fallback)."""
    markup = _keyboard_markup(rows if rows is not None else QUICK_KEYBOARD)
    if markup is None:
        print("keyboard: bad rows, not sent", flush=True)
        return False
    send_message(chat_id, text or KEYBOARD_TEXT, reply_markup=markup)
    return True

def send_message_bale(chat_id, text):
    if not BALE_TOKEN:
        return
    for i in range(0, len(text), 4000):
        data = json.dumps({"chat_id": chat_id, "text": text[i:i+4000]}).encode()
        req = urllib.request.Request(f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

def flush_outbox():
    if not os.path.exists(OUTBOX):
        return
    lines = [l for l in open(OUTBOX, encoding="utf-8") if l.strip()]
    if not lines:
        return
    open(OUTBOX, "w").close()
    for line in lines:
        item = json.loads(line)
        if item.get("platform") == "bale":
            send_message_bale(item["chat_id"], item["text"])
        elif item.get("kind") == "widget":
            send_widget(item["chat_id"])
        elif item.get("kind") == "keyboard":
            send_keyboard(item["chat_id"], rows=item.get("keyboard"),
                          text=item.get("text"))
        elif item.get("keyboard"):
            send_message(item["chat_id"], item["text"],
                         reply_markup=_keyboard_markup(item["keyboard"]))
        else:
            send_message(item["chat_id"], item["text"])

def _muse_authorized(handler):
    # Muse reads/sends via the same secret the user set as WEBHOOK_SECRET_TOKEN.
    # No token is in this file; it comes only from the host environment.
    if not SECRET:
        return False
    return handler.headers.get("X-Muse-Secret") == SECRET

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        if self.path == "/pending":
            items = []
            if os.path.exists(INBOX):
                for line in open(INBOX, encoding="utf-8"):
                    if line.strip():
                        try:
                            items.append(json.loads(line))
                        except Exception:
                            pass
            last_at = items[-1].get("inbox_at") if items else None
            data = json.dumps({"count": len(items), "last_at": last_at}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        if OBSCURE_PATH and self.path == f"/inbox-{OBSCURE_PATH}":
            items = []
            if os.path.exists(INBOX):
                for line in open(INBOX, encoding="utf-8"):
                    if line.strip():
                        try:
                            items.append(json.loads(line))
                        except Exception:
                            pass
            data = json.dumps({"items": items}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        if self.path.startswith("/inbox"):
            if not _muse_authorized(self):
                self.send_response(403); self.end_headers(); return
            items = []
            if os.path.exists(INBOX):
                for line in open(INBOX, encoding="utf-8"):
                    if line.strip():
                        try:
                            items.append(json.loads(line))
                        except Exception:
                            pass
            data = json.dumps({"items": items}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        self.send_response(501); self.end_headers(); return
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # Obscure-path outbox: same OBSCURE_PATH as the inbox route, no secret
        # header. Anyone with the full obscure URL could send as the bot, so
        # keep that URL private. Secret /outbox below is unchanged.
        if OBSCURE_PATH and self.path == f"/outbox-{OBSCURE_PATH}":
            try:
                item = json.loads(body)
                if _outbox_accept(item):
                    with open(OUTBOX, "a", encoding="utf-8") as f:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    flush_outbox()
            except Exception as e:
                print("obscure outbox error:", e, flush=True)
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        if self.path == "/outbox":
            # Muse -> Telegram: Muse posts a reply, server sends it via the bot.
            if not _muse_authorized(self):
                self.send_response(403); self.end_headers(); return
            try:
                item = json.loads(body)
                if _outbox_accept(item):
                    with open(OUTBOX, "a", encoding="utf-8") as f:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    flush_outbox()
            except Exception as e:
                print("outbox error:", e, flush=True)
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        if SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
            self.send_response(403); self.end_headers(); return
        try:
            update = json.loads(body)
            msg = update.get("message") or {}
            user = msg.get("from", {})
            if str(user.get("id")) in ALLOWED and "text" in msg:
                with open(INBOX, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"platform": "telegram", "chat_id": msg["chat"]["id"],
                        "user_id": user.get("id"), "text": msg["text"],
                        "inbox_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")
                send_message(msg["chat"]["id"], "دریافت شد ✅ در صف پاسخ")
                flush_outbox()
            wq = update.get("widget_query")
            if wq:
                _handle_widget_query(wq)
        except Exception as e:
            print("webhook error:", e, flush=True)
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *args):
        pass

def _poll_loop():
    # n8n-style polling: every 2 seconds ask Telegram for new updates,
    # only whitelisted user_id, write inbox, auto-reply, flush outbox.
    # Runs only when TELEGRAM_POLLING=1 and no webhook is set.
    import threading
    offset = 0
    while True:
        try:
            if TOKEN:
                url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
                data = json.dumps({"offset": offset, "timeout": 0}).encode()
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    user = msg.get("from", {})
                    if str(user.get("id")) in ALLOWED and "text" in msg:
                        with open(INBOX, "a", encoding="utf-8") as f:
                            f.write(json.dumps({"platform": "telegram", "chat_id": msg["chat"]["id"],
                                "user_id": user.get("id"), "text": msg["text"],
                                "inbox_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")
                        send_message(msg["chat"]["id"], "دریافت شد ✅ در صف پاسخ")
                        flush_outbox()
                    wq = update.get("widget_query")
                    if wq:
                        _handle_widget_query(wq)
        except Exception as e:
            print("poll error:", e, flush=True)
        time.sleep(2)

def _bale_poll_loop():
    # Bale polling, same pattern as Telegram: every 2 seconds, whitelisted only.
    offset = 0
    while True:
        try:
            if BALE_TOKEN:
                url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/getUpdates"
                data = json.dumps({"offset": offset, "timeout": 0}).encode()
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message") or {}
                    user = msg.get("from", {})
                    if str(user.get("id")) in BALE_ALLOWED and "text" in msg:
                        with open(INBOX, "a", encoding="utf-8") as f:
                            f.write(json.dumps({"platform": "bale", "chat_id": msg["chat"]["id"],
                                "user_id": user.get("id"), "text": msg["text"],
                                "inbox_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")
                        send_message_bale(msg["chat"]["id"], "دریافت شد ✅ در صف پاسخ")
                        flush_outbox()
        except Exception as e:
            print("bale poll error:", e, flush=True)
        time.sleep(2)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("WEBHOOK_PORT", "8443")))
    if os.environ.get("TELEGRAM_POLLING") == "1":
        import threading
        threading.Thread(target=_poll_loop, daemon=True).start()
        print("Polling mode ON (n8n-style, every 2s)", flush=True)
    if os.environ.get("BALE_POLLING") == "1":
        import threading
        threading.Thread(target=_bale_poll_loop, daemon=True).start()
        print("Bale polling mode ON (every 2s)", flush=True)
    print(f"Webhook server listening on 0.0.0.0:{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
