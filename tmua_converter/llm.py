"""Running Claude tasks: a manual tool-use loop ending in a "submit" tool.

Each pipeline step is a *task*: a user message (images + instructions), some
helper tools (zoom into the page, preview a crop) and one submit tool whose
input is the task's result.  The submit tool's input is validated locally;
if it is rejected the reason goes back to Claude as a tool error so it can fix
it in the same conversation.
"""

from __future__ import annotations

import base64
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import anthropic
from PIL import Image

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_IMAGE_BYTES = 3_700_000  # base64 inflates by 4/3; the API limit per image is 5 MB
PNG_PREFERRED_BYTES = 1_500_000  # larger renders (scanned pages) go as high-quality JPEG to keep requests small

# Models offered in the UI/CLI help: all accept adaptive thinking + effort + the tools used here.
SUPPORTED_MODELS = ["claude-opus-5", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5", "claude-opus-4-8"]

# USD per million tokens (input, output); cache writes 1.25x input, reads 0.1x.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


# ----------------------------------------------------------------------------- content helpers
def image_block(img: Image.Image | bytes) -> dict:
    """Base64 image content block (PNG, or JPEG if a PNG would be too large)."""
    if isinstance(img, bytes):
        data, media = img, "image/png"
        if len(data) > PNG_PREFERRED_BYTES:
            img = Image.open(io.BytesIO(data))
    if isinstance(img, Image.Image):
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        data, media = buf.getvalue(), "image/png"
        if len(data) > PNG_PREFERRED_BYTES:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=92)
            data, media = buf.getvalue(), "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(data).decode("ascii")}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


# ----------------------------------------------------------------------------- tasks
@dataclass
class ToolResult:
    content: list[dict] | str
    is_error: bool = False


class SubmitRejected(Exception):
    """Raised by a submit validator; the message is shown to Claude."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], ToolResult] | None = None  # None for the submit tool
    validate: Callable[[dict], Any] | None = None  # submit tool only


@dataclass
class Task:
    name: str
    system: str
    content: list[dict]
    submit: Tool
    tools: list[Tool] = field(default_factory=list)
    max_turns: int = 20
    max_rejections: int = 3


class TaskFailed(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    requests: int = 0

    def add(self, u: Any) -> None:
        self.requests += 1
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            setattr(self, k, getattr(self, k) + int(getattr(u, k, 0) or 0))

    def cost_usd(self, model: str) -> float | None:
        price = PRICES.get(model)
        if price is None:
            return None
        pin, pout = price
        return (self.input_tokens * pin + self.cache_creation_input_tokens * pin * 1.25
                + self.cache_read_input_tokens * pin * 0.1 + self.output_tokens * pout) / 1e6

    def to_dict(self, model: str) -> dict:
        d = dict(self.__dict__)
        cost = self.cost_usd(model)
        d["estimated_cost_usd"] = round(cost, 2) if cost is not None else None
        return d


class Transport(Protocol):
    """Sends one Messages API request and returns the final message."""

    def send(self, params: dict) -> Any: ...


class AnthropicTransport:
    """Streams each request (long outputs, big images) and returns the final message."""

    def __init__(self, client: anthropic.Anthropic | None = None, api_key: str | None = None):
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=4)

    @property
    def custom_base_url(self) -> bool:
        return "api.anthropic.com" not in str(self.client.base_url)

    def send(self, params: dict) -> Any:
        with self.client.beta.messages.stream(**params) as stream:
            return stream.get_final_message()


# Optional request features that a proxy/older deployment may reject with a 400;
# each is dropped (with a warning) if the API names it in an error.
_OPTIONAL_FEATURES = ("fallbacks", "eager_input_streaming", "strict")


class ClaudeRunner:
    def __init__(self, transport: Transport, model: str = DEFAULT_MODEL, effort: str = "high",
                 max_tokens: int = 64000, use_fallbacks: bool = True, eager_tools: bool | None = None,
                 strict_tools: bool = True):
        self.transport = transport
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        if eager_tools is None:
            eager_tools = not getattr(transport, "custom_base_url", False)
        self.features = {"fallbacks": use_fallbacks, "eager_input_streaming": eager_tools, "strict": strict_tools}
        self.usage = Usage()
        self._lock = threading.Lock()

    # -- request building ---------------------------------------------------
    def _tool_defs(self, task: Task) -> list[dict]:
        defs = []
        for t in [*task.tools, task.submit]:
            d = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            if self.features["strict"]:
                d["strict"] = True
            if self.features["eager_input_streaming"]:
                d["eager_input_streaming"] = True
            defs.append(d)
        return defs

    def _params(self, task: Task, messages: list[dict]) -> dict:
        p = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": task.system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "tools": self._tool_defs(task),
            "tool_choice": {"type": "auto"},
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.features["fallbacks"]:
            p["betas"] = [FALLBACK_BETA]
            p["fallbacks"] = "default"
        return p

    def _send(self, task: Task, messages: list[dict]) -> Any:
        attempts = 0
        while True:
            attempts += 1
            try:
                resp = self.transport.send(self._params(task, messages))
            except anthropic.BadRequestError as exc:
                dropped = self._features_to_drop(str(exc).lower())
                if not dropped:
                    raise
                with self._lock:
                    for f in dropped:
                        self.features[f] = False
                log.warning("API rejected %s; retrying without it", ", ".join(dropped))
                continue
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
                if attempts >= 4:
                    raise
                delay = min(60, 5 * 2 ** attempts)
                log.warning("%s: %s - retrying in %ss", task.name, type(exc).__name__, delay)
                time.sleep(delay)
                continue
            except ValueError as exc:
                # Eager tool streaming: the SDK could not parse the tool input JSON at all.
                if attempts >= 3:
                    raise TaskFailed(f"{task.name}: unparseable tool input from the API ({exc})") from exc
                log.warning("%s: unparseable streamed tool input, re-issuing request", task.name)
                continue
            with self._lock:
                self.usage.add(getattr(resp, "usage", None))
            return resp

    def _features_to_drop(self, msg: str) -> list[str]:
        """Optional features a 400 error points at (dropped so the request can be retried)."""
        on = [f for f in _OPTIONAL_FEATURES if self.features.get(f)]
        named = [f for f in on if f in msg or (f == "fallbacks" and ("fallback" in msg or "beta" in msg))]
        if named:
            return named
        if "tool" in msg:  # a tool-definition complaint that names neither field: drop one at a time
            return [f for f in ("eager_input_streaming", "strict") if f in on][:1]
        return []

    # -- the loop -----------------------------------------------------------
    def run(self, task: Task) -> Any:
        handlers = {t.name: t for t in task.tools}
        messages: list[dict] = [{"role": "user", "content": task.content}]
        rejections = 0
        nudges = 0
        for _turn in range(task.max_turns):
            resp = self._send(task, messages)
            stop = getattr(resp, "stop_reason", None)
            if stop == "refusal":
                details = getattr(resp, "stop_details", None)
                raise TaskFailed(f"{task.name}: Claude declined the request ({getattr(details, 'category', None)})")
            if stop == "max_tokens":
                raise TaskFailed(f"{task.name}: response hit max_tokens ({self.max_tokens}) before finishing")
            content = list(getattr(resp, "content", []) or [])
            messages.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                nudges += 1
                if nudges > 2:
                    raise TaskFailed(f"{task.name}: Claude stopped without calling {task.submit.name}")
                messages.append({"role": "user", "content": [text_block(
                    f"Please finish by calling the {task.submit.name} tool with your result.")]})
                continue

            results: list[dict] = []
            accepted: Any = None
            got_submit = False
            for tu in tool_uses:
                inp = tu.input if isinstance(tu.input, dict) else {}
                if tu.name == task.submit.name:
                    got_submit = True
                    try:
                        shape = check_schema(task.submit.input_schema, tu.input)
                        if shape:
                            raise SubmitRejected("Malformed input:\n" + "\n".join(shape[:20]))
                        accepted = task.submit.validate(inp) if task.submit.validate else inp
                        results.append(_tool_result(tu.id, "Accepted."))
                    except (SubmitRejected, KeyError, TypeError, ValueError, AttributeError) as exc:
                        rejections += 1
                        reason = str(exc) if isinstance(exc, SubmitRejected) else f"{type(exc).__name__}: {exc}"
                        if rejections > task.max_rejections:
                            raise TaskFailed(f"{task.name}: submission rejected repeatedly: {reason}") from exc
                        results.append(_tool_result(tu.id, f"Not accepted - please fix and call "
                                                           f"{task.submit.name} again:\n{reason}", True))
                elif tu.name in handlers:
                    shape = check_schema(handlers[tu.name].input_schema, tu.input)
                    if shape:
                        out = ToolResult("Malformed input: " + "; ".join(shape[:10]), True)
                    else:
                        try:
                            out = handlers[tu.name].handler(inp)
                        except Exception as exc:  # noqa: BLE001 - reported to Claude
                            out = ToolResult(f"Tool error: {exc}", True)
                    results.append(_tool_result(tu.id, out.content, out.is_error))
                else:
                    results.append(_tool_result(tu.id, f"Unknown tool {tu.name}", True))
            if got_submit and accepted is not None:
                return accepted
            messages.append({"role": "user", "content": results})
        raise TaskFailed(f"{task.name}: no accepted {task.submit.name} call after {task.max_turns} turns")


def check_schema(schema: dict, value: Any, path: str = "input") -> list[str]:
    """Minimal JSON-schema check (types, required keys, enums, array items).

    Tool inputs are streamed eagerly, so the API does not validate them; this
    catches truncated or malformed inputs before they are used.
    """
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t in types and not isinstance(value, types[t]):
        return [f"{path}: expected {t}"]
    if t == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return [f"{path}: expected integer"]
    if t == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        return [f"{path}: expected number"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: must be one of {schema['enum']}"]
    problems: list[str] = []
    if t == "object":
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                problems += check_schema(sub, value[key], f"{path}.{key}")
    elif t == "array" and "items" in schema:
        for i, item in enumerate(value):
            problems += check_schema(schema["items"], item, f"{path}[{i}]")
    return problems


def _tool_result(tool_use_id: str, content: list[dict] | str, is_error: bool = False) -> dict:
    block: dict = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block
