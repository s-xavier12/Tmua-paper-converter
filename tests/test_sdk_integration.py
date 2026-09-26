"""Run the real Anthropic SDK (streaming) against a local mock of the Messages API.

Checks the request the SDK actually sends (headers, body) and that streamed
tool calls, thinking blocks and multi-turn history round-trip correctly.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import pytest

from tmua_converter.llm import AnthropicTransport, ClaudeRunner, Task, Tool, ToolResult, text_block

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}, "n": {"type": "integer"}},
          "required": ["answer", "n"], "additionalProperties": False}
ZOOM = {"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"],
        "additionalProperties": False}


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def tool_turn(tool_id, name, partial_json_chunks, text_before=None):
    events = [{"type": "message_start", "message": {
        "id": f"msg_{tool_id}", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 40,
                  "cache_creation_input_tokens": 10}}}]
    idx = 0
    events += [
        {"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": "",
                                                                          "signature": ""}},
        {"type": "content_block_delta", "index": idx, "delta": {"type": "signature_delta", "signature": "sig123"}},
        {"type": "content_block_stop", "index": idx},
    ]
    idx += 1
    if text_before:
        events += [
            {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text_before}},
            {"type": "content_block_stop", "index": idx},
        ]
        idx += 1
    events.append({"type": "content_block_start", "index": idx,
                   "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}})
    for chunk in partial_json_chunks:
        events.append({"type": "content_block_delta", "index": idx,
                       "delta": {"type": "input_json_delta", "partial_json": chunk}})
    events += [
        {"type": "content_block_stop", "index": idx},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 50}},
        {"type": "message_stop"},
    ]
    return sse(events)


@pytest.fixture()
def mock_api():
    state = {"requests": [], "responses": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
            payload = state["responses"].pop(0)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


def test_real_sdk_roundtrip(mock_api):
    # turn 1: a zoom call (JSON split across chunks, LaTeX escapes), turn 2: the submission
    mock_api["responses"] = [
        tool_turn("toolu_1", "zoom", ['{"pa', 'ge": 3}'], text_before="Zooming in."),
        tool_turn("toolu_2", "submit", ['{"answer": "$\\\\frac{1}{2}$\\n\\nnext', '", "n": 7}']),
    ]
    client = anthropic.Anthropic(api_key="test-key", base_url=mock_api["url"], max_retries=0)
    transport = AnthropicTransport(client=client)
    runner = ClaudeRunner(transport, eager_tools=True)
    zooms = []
    task = Task(name="t", system="system prompt", content=[text_block("go")],
                tools=[Tool("zoom", "zoom in", ZOOM, handler=lambda i: zooms.append(i) or ToolResult("ok"))],
                submit=Tool("submit", "submit", SCHEMA))
    result = runner.run(task)

    # the streamed JSON was reassembled and the LaTeX/newline escapes decoded exactly once
    assert result == {"answer": "$\\frac{1}{2}$\n\nnext", "n": 7}
    assert zooms == [{"page": 3}]

    req1, req2 = mock_api["requests"]
    assert req1["path"].startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in req1["headers"].get("anthropic-beta", "")
    b = req1["body"]
    assert b["model"] == "claude-opus-5" and b["stream"] is True and b["fallbacks"] == "default"
    assert b["thinking"] == {"type": "adaptive"} and b["output_config"] == {"effort": "high"}
    assert b["cache_control"] == {"type": "ephemeral"}
    assert b["tools"][0]["strict"] is True and b["tools"][0]["eager_input_streaming"] is True

    # second request replays the assistant turn unchanged (thinking signature included) + the tool result
    msgs = req2["body"]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assistant = msgs[1]["content"]
    assert assistant[0]["type"] == "thinking" and assistant[0]["signature"] == "sig123"
    assert assistant[-1] == {"type": "tool_use", "id": "toolu_1", "name": "zoom", "input": {"page": 3}}
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_1"
    assert runner.usage.requests == 2 and runner.usage.cache_read_input_tokens == 80
