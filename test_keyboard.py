#!/usr/bin/env python3
"""Mocked tests for the reply-keyboard fallback (no real tokens, no Telegram network).

Run: python3 test_keyboard.py
Covers:
  - QUICK_KEYBOARD layout (4 buttons, 2 rows of 2, exact order)
  - _keyboard_markup shape and malformed-input rejection
  - send_message carries reply_markup (last chunk only) or none at all
  - send_keyboard defaults, custom rows/text, malformed rows
  - flush_outbox dispatch: keyboard kind, text+keyboard, plain text,
    widget (unchanged), Bale (untouched)
  - _outbox_accept for the payloads the parent agent posts
"""
import io, json, os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import webhook_server as ws

ws.TOKEN = "TESTTOKEN"


class _FakeResp:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return b'{"ok": true, "result": {}}'


def _capture_urlopen(calls):
    def _fake(req, timeout=30):
        calls.append(json.loads(req.data.decode("utf-8")))
        return _FakeResp()
    return _fake


class TestQuickKeyboard(unittest.TestCase):
    def test_layout_is_two_rows_of_two_in_order(self):
        self.assertEqual(ws.QUICK_KEYBOARD,
                         [["چک کن", "صورتحساب بگیر"],
                          ["وضعیت سیستم", "تراکنش بفرست"]])
        flat = [b for row in ws.QUICK_KEYBOARD for b in row]
        self.assertEqual(flat, ["چک کن", "صورتحساب بگیر",
                                "وضعیت سیستم", "تراکنش بفرست"])
        self.assertIsInstance(ws.KEYBOARD_TEXT, str)
        self.assertTrue(ws.KEYBOARD_TEXT)

    def test_markup_shape(self):
        m = ws._keyboard_markup(ws.QUICK_KEYBOARD)
        self.assertEqual(m, {"keyboard": ws.QUICK_KEYBOARD,
                             "resize_keyboard": True,
                             "one_time_keyboard": False})

    def test_malformed_rows_rejected(self):
        for bad in (None, "x", [], [[]], [["a"], "not-a-list"],
                    [["a"], [123]], [["a"], [""]], [[None]]):
            self.assertIsNone(ws._keyboard_markup(bad), f"should reject {bad!r}")
        # Empty strings are dropped; a row left with only them is rejected.
        self.assertEqual(ws._keyboard_markup([["a", ""], ["b"]])["keyboard"],
                         [["a"], ["b"]])


class TestSendMessageMarkup(unittest.TestCase):
    def test_plain_text_has_no_reply_markup(self):
        calls = []
        with mock.patch.object(ws.urllib.request, "urlopen",
                               side_effect=_capture_urlopen(calls)):
            ws.send_message(72551872, "سلام")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["chat_id"], 72551872)
        self.assertEqual(calls[0]["text"], "سلام")
        self.assertNotIn("reply_markup", calls[0])

    def test_markup_attached_to_the_message(self):
        calls = []
        markup = ws._keyboard_markup(ws.QUICK_KEYBOARD)
        with mock.patch.object(ws.urllib.request, "urlopen",
                               side_effect=_capture_urlopen(calls)):
            ws.send_message(72551872, "دکمه‌ها", reply_markup=markup)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["reply_markup"], markup)
        self.assertEqual(calls[0]["reply_markup"]["keyboard"],
                         ws.QUICK_KEYBOARD)

    def test_long_text_markup_only_on_last_chunk(self):
        calls = []
        markup = ws._keyboard_markup(ws.QUICK_KEYBOARD)
        with mock.patch.object(ws.urllib.request, "urlopen",
                               side_effect=_capture_urlopen(calls)):
            ws.send_message(72551872, "x" * 5000, reply_markup=markup)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("reply_markup", calls[0])
        self.assertEqual(calls[1]["reply_markup"], markup)
        self.assertEqual(len(calls[0]["text"]), 4000)
        self.assertEqual(len(calls[1]["text"]), 1000)

    def test_no_token_sends_nothing(self):
        calls = []
        with mock.patch.object(ws, "TOKEN", ""), \
             mock.patch.object(ws.urllib.request, "urlopen",
                               side_effect=_capture_urlopen(calls)):
            ws.send_message(72551872, "سلام")
        self.assertEqual(calls, [])


class TestSendKeyboard(unittest.TestCase):
    def test_default_sends_caption_and_quick_keyboard(self):
        calls = []
        with mock.patch.object(ws.urllib.request, "urlopen",
                               side_effect=_capture_urlopen(calls)):
            self.assertTrue(ws.send_keyboard(72551872))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["text"], ws.KEYBOARD_TEXT)
        self.assertEqual(calls[0]["reply_markup"]["keyboard"],
                         ws.QUICK_KEYBOARD)

    def test_custom_rows_and_text_override(self):
        calls = []
        rows = [["الف", "ب"]]
        with mock.patch.object(ws.urllib.request, "urlopen",
                               side_effect=_capture_urlopen(calls)):
            self.assertTrue(ws.send_keyboard(72551872, rows=rows, text="متن"))
        self.assertEqual(calls[0]["text"], "متن")
        self.assertEqual(calls[0]["reply_markup"]["keyboard"], rows)

    def test_malformed_rows_send_nothing(self):
        calls = []
        with mock.patch.object(ws.urllib.request, "urlopen",
                               side_effect=_capture_urlopen(calls)):
            self.assertFalse(ws.send_keyboard(72551872, rows=[["a"], "bad"]))
        self.assertEqual(calls, [])


class TestFlushOutboxDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, mode="w", encoding="utf-8")
        self.tmp.close()
        self._old_outbox = ws.OUTBOX
        ws.OUTBOX = self.tmp.name

    def tearDown(self):
        ws.OUTBOX = self._old_outbox
        os.unlink(self.tmp.name)

    def _write(self, *items):
        with open(self.tmp.name, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")

    def test_keyboard_kind_dispatch(self):
        self._write({"platform": "telegram", "chat_id": 72551872,
                     "kind": "keyboard"})
        with mock.patch.object(ws, "send_keyboard") as mk, \
             mock.patch.object(ws, "send_message") as mm, \
             mock.patch.object(ws, "send_widget") as mw:
            ws.flush_outbox()
        mk.assert_called_once_with(72551872, rows=None, text=None)
        mm.assert_not_called()
        mw.assert_not_called()

    def test_keyboard_kind_with_custom_rows_and_text(self):
        rows = [["الف"]]
        self._write({"platform": "telegram", "chat_id": 72551872,
                     "kind": "keyboard", "keyboard": rows, "text": "بزن"})
        with mock.patch.object(ws, "send_keyboard") as mk:
            ws.flush_outbox()
        mk.assert_called_once_with(72551872, rows=rows, text="بزن")

    def test_text_plus_keyboard_field(self):
        self._write({"platform": "telegram", "chat_id": 72551872,
                     "text": "سلام", "keyboard": ws.QUICK_KEYBOARD})
        with mock.patch.object(ws, "send_message") as mm:
            ws.flush_outbox()
        mm.assert_called_once()
        _, kwargs = mm.call_args
        self.assertEqual(kwargs["reply_markup"]["keyboard"], ws.QUICK_KEYBOARD)
        self.assertIs(kwargs["reply_markup"]["resize_keyboard"], True)
        self.assertIs(kwargs["reply_markup"]["one_time_keyboard"], False)

    def test_plain_text_unchanged(self):
        self._write({"platform": "telegram", "chat_id": 72551872,
                     "text": "سلام"})
        with mock.patch.object(ws, "send_message") as mm, \
             mock.patch.object(ws, "send_keyboard") as mk:
            ws.flush_outbox()
        mm.assert_called_once_with(72551872, "سلام")
        self.assertNotIn("reply_markup", mm.call_args.kwargs)
        mk.assert_not_called()

    def test_widget_dispatch_unchanged(self):
        self._write({"platform": "telegram", "chat_id": 72551872,
                     "kind": "widget"})
        with mock.patch.object(ws, "send_widget") as mw, \
             mock.patch.object(ws, "send_message") as mm, \
             mock.patch.object(ws, "send_keyboard") as mk:
            ws.flush_outbox()
        mw.assert_called_once_with(72551872)
        mm.assert_not_called()
        mk.assert_not_called()

    def test_bale_untouched_by_keyboard(self):
        self._write({"platform": "bale", "chat_id": 63920689,
                     "text": "سلام"})
        with mock.patch.object(ws, "send_message_bale") as mb, \
             mock.patch.object(ws, "send_message") as mm:
            ws.flush_outbox()
        mb.assert_called_once_with(63920689, "سلام")
        mm.assert_not_called()


class TestOutboxAccept(unittest.TestCase):
    def test_parent_keyboard_payload_accepted(self):
        # Exact payload the parent posts to send the keyboard panel.
        self.assertTrue(ws._outbox_accept(
            {"platform": "telegram", "chat_id": 72551872, "kind": "keyboard"}))

    def test_text_only_accepted(self):
        self.assertTrue(ws._outbox_accept(
            {"platform": "telegram", "chat_id": 72551872, "text": "سلام"}))

    def test_text_plus_keyboard_accepted(self):
        self.assertTrue(ws._outbox_accept(
            {"platform": "telegram", "chat_id": 72551872,
             "text": "سلام", "keyboard": ws.QUICK_KEYBOARD}))

    def test_widget_kind_still_accepted(self):
        self.assertTrue(ws._outbox_accept(
            {"platform": "telegram", "chat_id": 72551872, "kind": "widget"}))

    def test_rejected_without_anything_to_send(self):
        self.assertFalse(ws._outbox_accept(
            {"platform": "telegram", "chat_id": 72551872}))
        self.assertFalse(ws._outbox_accept(
            {"platform": "telegram", "text": "سلام"}))
        self.assertFalse(ws._outbox_accept(
            {"platform": "telegram", "chat_id": 72551872, "kind": "bogus"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
