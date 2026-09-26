from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from tmua_converter.llm import (ClaudeRunner, SubmitRejected, Task, TaskFailed, Tool, ToolResult, check_schema,
                                text_block)

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}, "n": {"type": "integer"}},
          "required": ["answer", "n"], "additionalProperties": False}


def msg(*blocks, stop="tool_use"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, stop_details=None)


def tu(name, inp, i=[0]):  # noqa: B006 - counter
    i[0] += 1
    return SimpleNamespace(type="tool_use", id=f"t{i[0]}", name=name, input=inp)


class Scripted:
    custom_base_url = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.params = []

    def send(self, params):
        self.params.append({**params, "messages": list(params["messages"])})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_task(validate=None, tools=()):
    return Task(name="t", system="sys", content=[text_block("go")], tools=list(tools),
                submit=Tool("submit", "submit it", SCHEMA, validate=validate))


def test_request_shape_defaults():
    t = Scripted([msg(tu("submit", {"answer": "x", "n": 1}))])
    r = ClaudeRunner(t)
    assert r.run(make_task()) == {"answer": "x", "n": 1}
    p = t.params[0]
    assert p["model"] == "claude-opus-5"
    assert p["thinking"] == {"type": "adaptive"}
    assert p["output_config"] == {"effort": "high"}
    assert p["fallbacks"] == "default" and p["betas"] == ["server-side-fallback-2026-07-01"]
    assert p["tool_choice"] == {"type": "auto"}
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(d["strict"] and d["eager_input_streaming"] for d in p["tools"])


def test_rejection_is_sent_back_and_resubmission_accepted():
    def validate(inp):
        if inp["answer"] != "good":
            raise SubmitRejected("answer must be good")
        return inp["answer"]

    t = Scripted([msg(tu("submit", {"answer": "bad", "n": 1})), msg(tu("submit", {"answer": "good", "n": 1}))])
    assert ClaudeRunner(t).run(make_task(validate)) == "good"
    last_user = t.params[1]["messages"][-1]["content"][0]
    assert last_user["is_error"] and "answer must be good" in last_user["content"]


def test_malformed_input_rejected_by_schema_check():
    t = Scripted([msg(tu("submit", {"answer": "x"})), msg(tu("submit", {"answer": "x", "n": 2}))])
    assert ClaudeRunner(t).run(make_task())["n"] == 2
    assert "missing 'n'" in t.params[1]["messages"][-1]["content"][0]["content"]


def test_helper_tool_results_include_images():
    seen = []
    tool = Tool("zoom", "zoom", {"type": "object", "properties": {"k": {"type": "integer"}}, "required": ["k"],
                                 "additionalProperties": False},
                handler=lambda inp: seen.append(inp) or ToolResult([text_block("zoomed")]))
    t = Scripted([msg(tu("zoom", {"k": 3})), msg(tu("submit", {"answer": "x", "n": 1}))])
    ClaudeRunner(t).run(make_task(tools=[tool]))
    assert seen == [{"k": 3}]
    result = t.params[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["content"][0]["text"] == "zoomed"


def test_refusal_and_max_tokens_fail_cleanly():
    with pytest.raises(TaskFailed, match="declined"):
        ClaudeRunner(Scripted([msg(stop="refusal")])).run(make_task())
    with pytest.raises(TaskFailed, match="max_tokens"):
        ClaudeRunner(Scripted([msg(tu("submit", {"answer": "x", "n": 1}), stop="max_tokens")])).run(make_task())


def test_nudged_when_no_tool_called():
    text = SimpleNamespace(type="text", text="done!")
    t = Scripted([msg(text, stop="end_turn"), msg(tu("submit", {"answer": "x", "n": 1}))])
    assert ClaudeRunner(t).run(make_task())["answer"] == "x"
    assert "submit" in t.params[1]["messages"][-1]["content"][0]["text"]


def test_optional_feature_dropped_after_400():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("fallbacks: not supported here", response=httpx2.Response(400, request=req),
                                    body=None)
    t = Scripted([err, msg(tu("submit", {"answer": "x", "n": 1}))])
    runner = ClaudeRunner(t)
    runner.run(make_task())
    assert "fallbacks" not in t.params[1] and not runner.features["fallbacks"]


def test_usage_and_cost():
    t = Scripted([msg(tu("submit", {"answer": "x", "n": 1}))])
    r = ClaudeRunner(t)
    r.run(make_task())
    d = r.usage.to_dict("claude-opus-5")
    assert d["requests"] == 1 and d["input_tokens"] == 10 and d["estimated_cost_usd"] is not None


def test_check_schema():
    assert check_schema(SCHEMA, {"answer": "a", "n": 1}) == []
    assert check_schema(SCHEMA, {"answer": 3, "n": 1}) == ["input.answer: expected string"]
    assert check_schema(SCHEMA, {"answer": "a", "n": True}) == ["input.n: expected integer"]
    arr = {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}
    assert check_schema(arr, ["a", "c"]) == ["input[1]: must be one of ['a', 'b']"]


def test_generic_tool_error_drops_one_feature_at_a_time():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("tools.0: Extra inputs are not permitted", response=httpx2.Response(400, request=req),
                                    body=None)
    t = Scripted([err, err, msg(tu("submit", {"answer": "x", "n": 1}))])
    runner = ClaudeRunner(t)
    runner.run(make_task())
    assert not runner.features["eager_input_streaming"] and not runner.features["strict"]
    assert runner.features["fallbacks"]
    assert "strict" not in t.params[2]["tools"][0]
