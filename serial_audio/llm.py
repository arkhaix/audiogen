"""LLM passes with two interchangeable backends.

subscription  shells out to headless Claude Code (`claude -p`) under the user's
              claude.ai login, so the work draws on subscription usage.
api           the Anthropic Python SDK, billed to API credits.

Both take the same system prompt, user text, and JSON schema and return the
validated object plus provenance, so artifacts are comparable whichever ran.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

# alias -> (Claude Code --model value, API model id)
MODELS = {
    "opus": ("opus", "claude-opus-5-5"),
    "sonnet": ("sonnet", "claude-sonnet-5-5"),
    "haiku": ("haiku", "claude-haiku-5-5"),
}
# USD per million tokens (input, output, cache write, cache read) for the estimate only
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
LIMIT_RE = re.compile(r"hit your (?P<kind>[\w ]+?) limit(?:\s*[·•-]\s*resets\s+(?P<reset>[^\n\"]+))?", re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class UsageLimitError(LLMError):
    def __init__(self, message: str, reset_text: str | None):
        super().__init__(message)
        self.reset_text = reset_text


@dataclass
class PassResult:
    data: dict
    backend: str
    model: str
    cost_estimate_usd: float | None
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def provenance(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model,
            "cost_estimate_usd": self.cost_estimate_usd,
            "usage": self.usage,
            "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }


def _estimate_cost(model_id: str, usage: dict) -> float | None:
    prices = PRICES.get(model_id)
    if not prices:
        return None
    i, o, cw, cr = prices
    return round(
        usage.get("input_tokens", 0) * i / 1e6
        + usage.get("output_tokens", 0) * o / 1e6
        + usage.get("cache_creation_input_tokens", 0) * cw / 1e6
        + usage.get("cache_read_input_tokens", 0) * cr / 1e6,
        5,
    )


class ClaudeCodeBackend:
    """Headless Claude Code under the subscription login.

    The chapter goes in on stdin (Windows caps argument length), the instruction is the
    -p argument, the system prompt replaces Claude Code's default through a temp file,
    all tools are off, and the schema answer comes back in `structured_output`. The
    child environment must carry no ANTHROPIC_API_KEY: a key outranks the login.
    """

    name = "subscription"

    def __init__(self, executable: str | None = None, max_turns: int = 4):
        self.executable = executable or shutil.which("claude")
        if not self.executable:
            raise LLMError("claude CLI not found on PATH")
        self.max_turns = max_turns

    def run(self, system_prompt: str, user_text: str, schema: dict, model: str, effort: str | None,
            instruction: str) -> PassResult:
        alias, model_id = MODELS[model]
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDECODE")}
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(system_prompt)
            prompt_path = fh.name
        try:
            cmd = [
                self.executable, "-p", instruction,
                "--output-format", "json",
                "--json-schema", json.dumps(schema, separators=(",", ":")),
                "--system-prompt-file", prompt_path,
                "--tools", "",
                "--no-session-persistence",
                "--max-turns", str(self.max_turns),
                "--model", alias,
            ]
            if effort:
                cmd += ["--effort", effort]
            proc = subprocess.run(cmd, input=user_text, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", env=env)
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass

        stdout = proc.stdout.strip()
        try:
            raw = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            raw = {}
        message = (raw.get("result") or "") if raw else ""
        for text in (message, stdout, proc.stderr or ""):
            m = LIMIT_RE.search(text)
            if m:
                raise UsageLimitError(f"subscription {m.group('kind')} limit reached", m.group("reset"))
        if proc.returncode != 0 or not raw or raw.get("is_error"):
            detail = message or proc.stderr.strip() or stdout[:2000]
            raise LLMError(f"claude -p failed (exit {proc.returncode}, subtype {raw.get('subtype')}): {detail}")
        data = raw.get("structured_output")
        if data is None:
            raise LLMError(f"claude -p returned no structured_output (subtype {raw.get('subtype')}): {message[:500]}")
        usage = raw.get("usage") or {}
        used_model = next(iter((raw.get("modelUsage") or {}).keys()), model_id)
        return PassResult(
            data=data, backend=self.name, model=used_model,
            cost_estimate_usd=raw.get("total_cost_usd"),
            usage={k: usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                  "cache_read_input_tokens")},
            raw={k: raw.get(k) for k in ("session_id", "num_turns", "duration_ms", "subtype", "stop_reason")},
        )


class AnthropicBackend:
    """The Anthropic SDK with structured outputs (output_config.format)."""

    name = "api"

    def __init__(self):
        import anthropic  # imported lazily so the subscription path never needs it
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()

    def run(self, system_prompt: str, user_text: str, schema: dict, model: str, effort: str | None,
            instruction: str) -> PassResult:
        _, model_id = MODELS[model]
        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        kwargs = dict(
            model=model_id,
            max_tokens=32000,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"{user_text}\n\n{instruction}"}],
            output_config=output_config,
        )
        a = self.anthropic
        try:
            if model in ("opus", "sonnet"):
                # server-side refusal fallback; not offered for Haiku
                with self.client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"],
                                                      fallbacks="default", **kwargs) as stream:
                    msg = stream.get_final_message()
            else:
                with self.client.messages.stream(**kwargs) as stream:
                    msg = stream.get_final_message()
        except a.AuthenticationError as e:
            raise LLMError(f"API authentication failed (set ANTHROPIC_API_KEY): {e.message}") from e
        except a.RateLimitError as e:
            raise LLMError(f"API rate limited: {e.message}") from e
        except a.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"API connection error: {e}") from e
        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise LLMError(f"API refused the request ({getattr(details, 'category', None)})")
        if msg.stop_reason == "max_tokens":
            raise LLMError("API response hit max_tokens before the JSON was complete")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"API returned invalid JSON: {e}") from e
        u = msg.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        return PassResult(data=data, backend=self.name, model=msg.model, cost_estimate_usd=_estimate_cost(msg.model, usage),
                          usage=usage, raw={"request_id": msg._request_id, "stop_reason": msg.stop_reason})


def _parse_reset(reset_text: str | None) -> _dt.datetime | None:
    """'3:45pm' or 'Mon 12:00am' -> the next matching local time; None if unparseable."""
    if not reset_text:
        return None
    text = reset_text.strip().lower()
    m = re.search(r"(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)\w*\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)", text)
    if not m:
        return None
    hour = int(m.group("h")) % 12 + (12 if m.group("ap") == "pm" else 0)
    minute = int(m.group("m") or 0)
    now = _dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if m.group("day"):
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        ahead = (days.index(m.group("day")) - now.weekday()) % 7
        target += _dt.timedelta(days=ahead)
    if target <= now:
        target += _dt.timedelta(days=1 if not m.group("day") else 7)
    return target


def run_pass(system_prompt: str, user_text: str, schema: dict, *, model: str = "opus", effort: str | None = None,
             backend: str = "auto", on_limit: str = "api", instruction: str = "Carry out your instructions on the "
             "input above and answer with the JSON object only.", log=print) -> PassResult:
    """Run one pass. backend: auto | subscription | api. on_limit (auto only): api | wait | fail."""
    if model not in MODELS:
        raise ValueError(f"unknown model alias {model!r}; choose from {', '.join(MODELS)}")
    if backend == "api":
        return AnthropicBackend().run(system_prompt, user_text, schema, model, effort, instruction)
    if backend not in ("auto", "subscription"):
        raise ValueError(f"unknown backend {backend!r}")
    cc = ClaudeCodeBackend()
    while True:
        try:
            return cc.run(system_prompt, user_text, schema, model, effort, instruction)
        except UsageLimitError as e:
            if backend == "subscription" or on_limit == "fail":
                raise
            if on_limit == "api":
                log(f"{e}; falling back to the API")
                return AnthropicBackend().run(system_prompt, user_text, schema, model, effort, instruction)
            target = _parse_reset(e.reset_text)
            delay = max(60, (target - _dt.datetime.now()).total_seconds() + 30) if target else 15 * 60
            log(f"{e}; waiting {delay / 60:.0f} min (resets {e.reset_text or 'unknown'})")
            time.sleep(delay)
