#!/usr/bin/env python3
"""Mocked tests for the widget fix (no real tokens, no Telegram network).

Run: python3 test_widget_fix.py
Covers:
  - _tg_call logs and returns Telegram's full 400 error body
  - sendWidget payload shape vs the Bot API 10.4 spec
  - _handle_widget_query with a real WidgetQuery object
    (type is {"type": "data"}, not a plain string)
"""
import io, json, os, sys, tempfile, zlib
import contextlib, unittest
from unittest import mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webhook_server as ws

ws.TOKEN = "TESTTOKEN"
ws.ALLOWED = {"72551872"}


def http_error_400(body_dict):
    body = json.dumps(body_dict).encode()
    return urllib.error.HTTPError(
        "https://api.telegram.org/botTESTTOKEN/sendWidget",
        400, "Bad Request", {}, io.BytesIO(body))


class TestTgCallLogging(unittest.TestCase):
    def test_400_body_is_logged_and_returned(self):
        err = http_error_400({"ok": False, "error_code": 400,
            "description": "Bad Request: INPUT_MEDIA_WIDGET_HTML_INVALID"})
        with mock.patch.object(ws.urllib.request, "urlopen", side_effect=err):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                res = ws._tg_call("sendWidget", {"chat_id": 1, "html": "<b>x</b>"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], 400)
        self.assertIn("INPUT_MEDIA_WIDGET_HTML_INVALID", res["description"])
        self.assertIn("INPUT_MEDIA_WIDGET_HTML_INVALID", buf.getvalue())
        self.assertIn("HTTP 400", buf.getvalue())

    def test_400_non_json_body_still_logged(self):
        err = urllib.error.HTTPError("https://x/", 400, "Bad Request", {},
                                     io.BytesIO(b"plain error text"))
        with mock.patch.object(ws.urllib.request, "urlopen", side_effect=err):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                res = ws._tg_call("sendWidget", {"chat_id": 1, "html": "<b>x</b>"})
        self.assertEqual(res["error_code"], 400)
        self.assertIn("plain error text", res["description"])
        self.assertIn("plain error text", buf.getvalue())

    def test_success_path_unchanged(self):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 5}}).encode()
        resp.__enter__.return_value = resp
        with mock.patch.object(ws.urllib.request, "urlopen", return_value=resp):
            res = ws._tg_call("sendWidget", {"chat_id": 1, "html": "<b>x</b>"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["message_id"], 5)

    def test_no_token_short_circuits(self):
        with mock.patch.object(ws, "TOKEN", ""):
            self.assertEqual(ws._tg_call("sendWidget", {})["description"], "no token")


class TestSendWidgetPayload(unittest.TestCase):
    def test_payload_matches_bot_api_10_4_spec(self):
        seen = {}
        def _fake_tg_call(m, p):
            seen["m"] = m
            seen["p"] = p
            return {"ok": True}
        with mock.patch.object(ws, "_tg_call", side_effect=_fake_tg_call):
            res = ws.send_widget(72551872)
        self.assertTrue(res["ok"])
        self.assertEqual(seen["m"], "sendWidget")
        p = seen["p"]
        self.assertIsInstance(p["chat_id"], int)
        self.assertEqual(p["chat_id"], 72551872)
        self.assertIsInstance(p["html"], str)
        self.assertIsInstance(p["chat_data"], str)          # String, not an object
        self.assertEqual(json.loads(p["chat_data"]), {"last": None})
        self.assertIs(p["allow_queries"], True)              # Boolean
        self.assertIsInstance(p["caption"], str)
        # Documented server limits (core.telegram.org/bots/widgets)
        html_b = p["html"].encode("utf-8")
        self.assertLessEqual(len(html_b), 32768)
        self.assertLessEqual(len(zlib.compress(html_b, 9)), 4096)
        self.assertNotIn(b"\x00", html_b)
        self.assertLessEqual(len(p["chat_data"].encode("utf-8")), 1024)
        self.assertLessEqual(len(p["caption"]), 1024)

    def test_widget_unavailable_without_html(self):
        with mock.patch.object(ws, "WIDGET_HTML", ""):
            res = ws.send_widget(72551872)
        self.assertFalse(res["ok"])


class TestHandleWidgetQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8")
        self.tmp.close()
        self._old_inbox = ws.INBOX
        ws.INBOX = self.tmp.name
        self.calls = []
        self._tg = mock.patch.object(
            ws, "_tg_call",
            side_effect=lambda m, p: self.calls.append((m, p)) or {"ok": True})
        self._tg.start()
        self._flush = mock.patch.object(ws, "flush_outbox", lambda: None)
        self._flush.start()

    def tearDown(self):
        self._tg.stop()
        self._flush.stop()
        ws.INBOX = self._old_inbox
        os.unlink(self.tmp.name)

    def _inbox_items(self):
        with open(self.tmp.name, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def _query(self, data="check", qtype=None, user_id=72551872):
        return {"id": "q1", "from": {"id": user_id},
                "chat": {"id": 72551872}, "message_id": 42,
                "type": {"type": qtype} if qtype else {"type": "data"},
                "data": data}

    def test_data_query_object_type_writes_inbox_command(self):
        # Regression: the old code compared the whole {"type": "data"} dict
        # against the string "data" and silently dropped every tap.
        ws._handle_widget_query(self._query("check"))
        items = self._inbox_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "چک کن")
        self.assertEqual(items[0]["via"], "widget")
        self.assertEqual(items[0]["chat_id"], 72551872)
        methods = [m for m, _ in self.calls]
        self.assertIn("answerWidgetQuery", methods)
        self.assertIn("editMessageWidgetData", methods)
        ans = [p for m, p in self.calls if m == "answerWidgetQuery"][0]
        self.assertEqual(json.loads(ans["data"]), {"ok": True, "cmd": "چک کن"})
        edit = [p for m, p in self.calls if m == "editMessageWidgetData"][0]
        self.assertEqual(edit["chat_id"], 72551872)
        self.assertEqual(edit["message_id"], 42)
        self.assertEqual(json.loads(edit["chat_data"])["last"], "چک کن")

    def test_all_three_commands_map(self):
        for data, cmd in (("check", "چک کن"), ("statement", "صورتحساب بگیر"),
                          ("status", "وضعیت سیستم")):
            ws._handle_widget_query(self._query(data))
        self.assertEqual([i["text"] for i in self._inbox_items()],
                         ["چک کن", "صورتحساب بگیر", "وضعیت سیستم"])

    def test_wallet_query_type_is_ignored(self):
        ws._handle_widget_query(self._query("check", qtype="sign_data"))
        ws._handle_widget_query(self._query("check", qtype="sent_transaction"))
        self.assertEqual(self._inbox_items(), [])
        self.assertEqual(self.calls, [])

    def test_unknown_tap_is_acked_but_not_stored(self):
        ws._handle_widget_query(self._query("bogus"))
        self.assertEqual(self._inbox_items(), [])
        self.assertEqual([m for m, _ in self.calls], ["answerWidgetQuery"])
        ans = self.calls[0][1]
        self.assertEqual(json.loads(ans["data"]), {"ok": False})

    def test_non_whitelisted_user_ignored(self):
        ws._handle_widget_query(self._query("check", user_id=12345))
        self.assertEqual(self._inbox_items(), [])
        self.assertEqual(self.calls, [])

    def test_plain_string_type_still_accepted(self):
        q = self._query("status")
        q["type"] = "data"  # defensive: tolerate a bare string too
        ws._handle_widget_query(q)
        self.assertEqual([i["text"] for i in self._inbox_items()], ["وضعیت سیستم"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
