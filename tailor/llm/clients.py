"""Model access behind one small interface, so Qwen can be swapped for Llama (or anything else).

Four adapters:
  * HttpChatClient - any OpenAI-compatible endpoint (Ollama, llama.cpp server, LM Studio)
  * AnthropicClient - Claude through the Anthropic API, only when the person sets it up (their resume facts and the
    postings then leave the Mac); the key comes from $ANTHROPIC_API_KEY or <data_dir>/anthropic_key.txt
  * ShellClient    - a console command that reads the prompt on stdin and prints the reply
  * StubClient     - canned replies, for building and testing without a model
"""
from __future__ import annotations

import re
import subprocess
from typing import Protocol

import httpx

from ..config import ModelSettings

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_think(text: str) -> str:
    """Qwen3 can emit a <think>...</think> block before its answer; drop it."""
    return _THINK.sub("", text).strip()


class LLMClient(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str: ...


class HttpChatClient:
    def __init__(self, base_url: str, model: str, timeout_s: float = 600.0, think: bool = False):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.think = think

    def complete(self, prompt, *, system=None, json_mode=False, max_tokens=1024, temperature=0.2):
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if not self.think:
            body["reasoning_effort"] = "none"  # Ollama: turns Qwen3's thinking off; other servers ignore it
        r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout_s)
        r.raise_for_status()
        choice = r.json()["choices"][0]
        text = strip_think(choice["message"].get("content") or "")
        if not text and choice.get("finish_reason") == "length":
            # a thinking-only model (e.g. Ollama's qwen3:4b, now Qwen3-4B-Thinking-2507) spent the whole budget thinking
            raise RuntimeError(f"{self.model} used all {max_tokens} tokens before answering; use an instruct model "
                               "such as qwen3:4b-instruct")
        return text


class AnthropicClient:
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str = "claude-sonnet-5-5", api_key: str = "", timeout_s: float = 120.0):
        if not api_key:
            raise ValueError("The Claude API needs a key: set ANTHROPIC_API_KEY or put it in <data_dir>/anthropic_key.txt")
        self.model, self.api_key, self.timeout_s = model, api_key, timeout_s

    def complete(self, prompt, *, system=None, json_mode=False, max_tokens=1024, temperature=0.2):
        content = prompt + ("\n\nReply with one JSON object only, no other text." if json_mode else "")
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": "user", "content": content}]}
        if system:
            body["system"] = system
        r = httpx.post(self.URL, json=body, timeout=self.timeout_s,
                       headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
        if json_mode:  # a ```json fence around the object
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        return text


class ShellClient:
    def __init__(self, command: list[str], timeout_s: float = 600.0):
        if not command:
            raise ValueError("ShellClient needs a command, e.g. ['python', 'run_qwen.py']")
        self.command = command
        self.timeout_s = timeout_s

    def complete(self, prompt, *, system=None, json_mode=False, max_tokens=1024, temperature=0.2):
        full = f"{system}\n\n{prompt}" if system else prompt
        done = subprocess.run(
            self.command,
            input=full,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=self.timeout_s,
            check=True,
        )
        return strip_think(done.stdout)


class StubClient:
    """Returns canned text: a fixed string, or the first value whose key appears in the prompt."""

    def __init__(self, canned: str | dict[str, str] = "[stub reply]"):
        self.canned = canned
        self.calls: list[str] = []

    def complete(self, prompt, *, system=None, json_mode=False, max_tokens=1024, temperature=0.2):
        self.calls.append(prompt)
        if isinstance(self.canned, str):
            return self.canned
        for key, reply in self.canned.items():
            if key in prompt:
                return reply
        return "[stub reply]"


def make_client(m: ModelSettings, data_dir=None) -> LLMClient:
    if m.kind == "http":
        return HttpChatClient(m.base_url, m.model, m.timeout_s, m.think)
    if m.kind == "anthropic":
        import os
        from pathlib import Path

        key = os.environ.get("ANTHROPIC_API_KEY", "")
        key_file = Path(data_dir) / "anthropic_key.txt" if data_dir else None
        if not key and key_file and key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
        return AnthropicClient(m.model if m.model.startswith("claude") else "claude-sonnet-5-5", key, m.timeout_s)
    if m.kind == "shell":
        return ShellClient(m.command, m.timeout_s)
    if m.kind == "stub":
        return StubClient()
    raise ValueError(f"Unknown model kind: {m.kind!r} (use 'http', 'anthropic', 'shell' or 'stub')")
