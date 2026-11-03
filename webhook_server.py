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
import json, os, time, urllib.request
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
ALLOWED = set(x.strip() for x in ENV.get("ALLOWED_USER_IDS", "72551872").split(",") if x.strip())

def send_message(chat_id, text):
    if not TOKEN:
        return
    for i in range(0, len(text), 4000):
        data = json.dumps({"chat_id": chat_id, "text": text[i:i+4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
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
        self.send_response(501); self.end_headers(); return
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/outbox":
            # Muse -> Telegram: Muse posts a reply, server sends it via the bot.
            if not _muse_authorized(self):
                self.send_response(403); self.end_headers(); return
            try:
                item = json.loads(body)
                if item.get("chat_id") and item.get("text"):
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
        except Exception as e:
            print("poll error:", e, flush=True)
        time.sleep(2)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("WEBHOOK_PORT", "8443")))
    if os.environ.get("TELEGRAM_POLLING") == "1":
        import threading
        threading.Thread(target=_poll_loop, daemon=True).start()
        print("Polling mode ON (n8n-style, every 2s)", flush=True)
    print(f"Webhook server listening on 0.0.0.0:{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
