"""Freeze a real search and compare model-facing tokens with the local tokenizer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "external/AstrBot")]

from astrbot.core.agent.tool import ToolSet
from integrations.qq_astrbot.plugin.agent_tools import AgentTools
from integrations.qq_astrbot.plugin.api_client import MediaClient
from integrations.qq_astrbot.plugin.context_budget import tool_result_view
from integrations.qq_astrbot.plugin.prompt_loader import load_prompts

PATH = ROOT / "storage/temporary/rain-love-token-baseline-20261029.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["before", "after"])
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))

    def post(route, value):
        request = Request("http://127.0.0.1:8080" + route,
            data=json.dumps(value, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=10) as response:
            return json.load(response)

    def count(value):
        return len(post("/tokenize", {"content": value, "add_special": False, "parse_special": True})["tokens"])

    def request_count(messages, tools):
        rendered = post("/apply-template", {"messages": messages, "tools": tools,
            "chat_template_kwargs": {"enable_thinking": True}})["prompt"]
        return len(rendered) if isinstance(rendered, list) else count(rendered)

    if args.stage == "before":
        if PATH.exists():
            raise RuntimeError("Baseline already exists; never overwrite the pre-change measurement")
        source = MediaClient(ROOT / "storage/qq_companion")._request("/v1/music/weapi",
            {"action": "search", "query": "雨爱", "page": 1, "scope": "qq:999:dm:101"})
        assert len(source["songs"]) == 20
        for song in source["songs"]:
            song.pop("source_receipt", None)
        prompts = load_prompts()
        persona = (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
        system = "\n\n".join([persona, prompts["agent_policy"], prompts["reply_output"]])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "搜索雨爱，选择合适版本播放。"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "search_rain", "type": "function",
                "function": {"name": "weapi", "arguments": '{"action":"search","query":"雨爱","page":1}'}}]},
            {"role": "tool", "tool_call_id": "search_rain", "content": ""}]
        data = {"query": "雨爱", "measurement": "Active Qwen tokenizer; fixed next-tool request, no QQ sends",
            "source_result": source, "messages": messages, "tools": ToolSet(AgentTools(None).registered()).openai_schema()}
        view = tool_result_view(source)
    else:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        source = {**data["source_result"], "songs": data["source_result"]["songs"][:5]}
        view = tool_result_view(source)
    body = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    messages = [dict(message) for message in data["messages"]]
    messages[-1]["content"] = body
    stats = {"songs": len(view["songs"]), "result_body_tokens": count(body),
             "fixed_request_tokens": request_count(messages, data["tools"])}
    if args.stage == "after":
        prompts = load_prompts()
        persona = (ROOT / "configs/characters/elysia/system_prompt.txt").read_text(encoding="utf-8-sig")
        messages[0]["content"] = "\n\n".join([persona, prompts["agent_policy"], prompts["reply_output"]])
        stats["current_request_tokens"] = request_count(messages, ToolSet(AgentTools(None).registered()).openai_schema())
    data[args.stage] = stats
    data[args.stage + "_view"] = view
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"path": str(PATH), args.stage: stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
