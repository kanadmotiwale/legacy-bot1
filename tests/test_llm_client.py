"""AnthropicLLM request/response handling against a fake SDK client (no network, no key)."""

from __future__ import annotations

from types import SimpleNamespace as NS

from cua.agent.llm import AnthropicLLM
from cua.agent.tools import TOOLS
from cua.surface.types import FrameInfo, Observation, UIElement


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def tool_use(name, **inp):
    return NS(type="tool_use", name=name, input=inp, id=f"tu_{name}")


def reply(*blocks):
    return NS(content=list(blocks), stop_reason="tool_use", usage=NS(input_tokens=10, output_tokens=5))


OBS = Observation(url="u", frames=[FrameInfo(path=[], url="u")],
                  elements=[UIElement(ref="f0e1", frame_path=[], role="button", name="Search")], screenshot=b"\x89PNG")


def test_tool_loop_shapes_and_append_only_history():
    msgs = FakeMessages([
        reply(tool_use("click", ref="f0e1", reason="search")),
        reply(NS(type="text", text="thinking out loud")),          # no tool call -> nudged once
        reply(tool_use("finish", capability_name="x.y", title="t", summary="s", success_text="ok", inputs=[], reason="done")),
    ])
    llm = AnthropicLLM(model="claude-opus-5-5", client=NS(beta=NS(messages=msgs), messages=msgs))
    llm.start("goal", "http://app")
    d1 = llm.decide(OBS, None)
    assert (d1.tool, d1.args, d1.reason) == ("click", {"ref": "f0e1"}, "search")
    first = msgs.calls[0]
    assert first["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert first["tools"] == TOOLS and all(t["strict"] for t in first["tools"])
    assert first["fallbacks"] == "default" and first["output_config"] == {"effort": "medium"}
    content = first["messages"][0]["content"]
    assert [c["type"] for c in content] == ["text", "image", "text"]  # goal, screenshot, outline
    snapshot = [dict(m) for m in msgs.calls[0]["messages"]]

    d2 = llm.decide(OBS, "ok: click done")
    assert d2.tool == "finish"
    hist = msgs.calls[-1]["messages"]
    assert hist[: len(snapshot)] == snapshot  # earlier turns are never edited
    tool_result = hist[2]["content"][0]
    assert tool_result == {"type": "tool_result", "tool_use_id": "tu_click", "content": "ok: click done"}
    nudges = [m for m in hist if m["role"] == "user" and isinstance(m["content"], list)
              and m["content"] and m["content"][0].get("text") == "Respond with exactly one tool call."]
    assert len(nudges) == 1 and hist[-1]["role"] == "assistant"
