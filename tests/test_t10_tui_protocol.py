from __future__ import annotations

import unittest

from T10_tui_protocol import build_event, build_request, build_response, decode_message, encode_message


class T10TuiProtocolTests(unittest.TestCase):
    def test_request_roundtrip(self):
        message = build_request("workflow.a00.start", {"argv": ["--yes"]}, message_id="req_1")
        decoded = decode_message(encode_message(message))
        self.assertEqual(decoded["kind"], "request")
        self.assertEqual(decoded["action"], "workflow.a00.start")
        self.assertEqual(decoded["payload"]["argv"], ["--yes"])

    def test_response_roundtrip(self):
        message = build_response("req_1", ok=True, payload={"result": 0})
        decoded = decode_message(encode_message(message))
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["payload"]["result"], 0)

    def test_event_roundtrip(self):
        message = build_event("progress.update", {"line": "running"})
        decoded = decode_message(encode_message(message))
        self.assertEqual(decoded["type"], "progress.update")
        self.assertEqual(decoded["payload"]["line"], "running")

    def test_decode_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            decode_message('{\"kind\": \"oops\", \"version\": \"1.0\"}')


if __name__ == "__main__":
    unittest.main()
