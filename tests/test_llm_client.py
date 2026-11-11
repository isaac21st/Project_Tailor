"""The HTTP model client: Qwen3 thinking is off unless asked for, and a budget spent thinking is an error, not ''."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tailor.llm import HttpChatClient  # noqa: E402


def reply(content, finish="stop"):
    r = mock.Mock()
    r.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    return r


class HttpChatClientTests(unittest.TestCase):
    def test_thinking_is_off_by_default(self):
        with mock.patch("httpx.post", return_value=reply("Hello.")) as post:
            self.assertEqual(HttpChatClient("http://x/v1", "qwen3:8b").complete("hi"), "Hello.")
        self.assertEqual(post.call_args.kwargs["json"]["reasoning_effort"], "none")

    def test_thinking_can_be_turned_on(self):
        with mock.patch("httpx.post", return_value=reply("<think>hmm</think>Hello.")) as post:
            self.assertEqual(HttpChatClient("http://x/v1", "qwen3:8b", think=True).complete("hi"), "Hello.")
        self.assertNotIn("reasoning_effort", post.call_args.kwargs["json"])

    def test_budget_spent_thinking_raises_instead_of_returning_nothing(self):
        with mock.patch("httpx.post", return_value=reply("", finish="length")):
            with self.assertRaisesRegex(RuntimeError, "before answering"):
                HttpChatClient("http://x/v1", "qwen3:4b").complete("hi")


if __name__ == "__main__":
    unittest.main()


class AnthropicClientTests(unittest.TestCase):
    def test_request_shape_and_json_fence(self):
        from unittest import mock

        from tailor.llm.clients import AnthropicClient

        seen = {}

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"content": [{"type": "text", "text": '```json\n{"options": ["a"]}\n```'}]}

        def post(url, json=None, timeout=None, headers=None):
            seen.update(url=url, body=json, headers=headers)
            return R()

        with mock.patch("tailor.llm.clients.httpx.post", post):
            out = AnthropicClient("claude-sonnet-5-5", "k-test").complete("Q", system="S", json_mode=True, max_tokens=50)
        self.assertEqual(out, '{"options": ["a"]}')
        self.assertEqual(seen["body"]["system"], "S")
        self.assertEqual(seen["body"]["model"], "claude-sonnet-5-5")
        self.assertEqual(seen["headers"]["x-api-key"], "k-test")

    def test_no_key_is_a_clear_error(self):
        from tailor.config import ModelSettings
        from tailor.llm.clients import make_client
        import os
        import tempfile
        from unittest import mock

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                make_client(ModelSettings(kind="anthropic", model="claude-sonnet-5-5"), d)
