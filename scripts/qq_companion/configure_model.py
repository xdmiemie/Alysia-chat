"""Probe llama.cpp and bind the one AstrBot Runner to the active local model.

Run only while AstrBot is stopped. Persona data lives in AstrBot's SQLite DB;
the upstream repo is ignored and never receives project-specific source edits.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

try:
    from scripts.qq_companion.configure import ASTRBOT, ROOT, read_json, save_json
except ModuleNotFoundError:  # direct script execution
    from configure import ASTRBOT, ROOT, read_json, save_json

sys.path.insert(0, str(ROOT))
from runtime.qwen3.config import Qwen3Config, load_qwen3_config

BASE = load_qwen3_config().base_url + "/v1"
PROVIDER_ID = "astrbot_chat_qwen3"
LOCAL_OPENER = build_opener(ProxyHandler({}))


def call(path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(BASE + path, data=body,
                      headers={"Content-Type": "application/json"} if body else {},
                      method="POST" if body else "GET")
    with LOCAL_OPENER.open(request, timeout=150) as response:
        return json.load(response)


def probe() -> str:
    output_tokens = load_qwen3_config().max_tokens
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            with LOCAL_OPENER.open(BASE.removesuffix("/v1") + "/health", timeout=3) as response:
                if response.status == 200:
                    break
        except (HTTPError, URLError, TimeoutError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError("llama-server did not become healthy within 180 seconds")
    models = call("/models").get("data", [])
    ids = [item.get("id") for item in models if isinstance(item.get("id"), str)]
    if len(ids) != 1:
        raise RuntimeError(f"Expected one llama-server model, found {len(ids)}")
    model_id = ids[0]
    chat = call("/chat/completions", {"model": model_id, "messages": [{"role": "user", "content": "Reply with OK."}],
                                      "temperature": 0, "max_tokens": output_tokens})
    if not chat.get("choices") or not chat["choices"][0].get("message", {}).get("content"):
        raise RuntimeError("llama-server chat completion failed")
    tool = {"type": "function", "function": {"name": "q2_echo", "description": "Echo a short value", "parameters": {
        "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}
    result = call("/chat/completions", {"model": model_id, "messages": [{"role": "user", "content": "Call q2_echo with value test."}],
                                         "tools": [tool], "tool_choice": "required", "temperature": 0, "max_tokens": output_tokens})
    calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
    if not calls or calls[0].get("function", {}).get("name") != "q2_echo":
        detail = json.dumps(result.get("choices", [{}])[0].get("message", {}), ensure_ascii=False)[:1200]
        raise RuntimeError(f"llama-server did not return a structured tool call: {detail}")
    try:
        arguments = json.loads(calls[0]["function"]["arguments"])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"llama-server tool arguments are not JSON: {calls[0]['function']['arguments'][:300]}") from exc
    if not isinstance(arguments.get("value"), str):
        raise RuntimeError("llama-server tool arguments are invalid")
    followup = call("/chat/completions", {"model": model_id, "messages": [
        {"role": "user", "content": "Call q2_echo with value test."},
        result["choices"][0]["message"],
        {"role": "tool", "tool_call_id": calls[0]["id"], "content": "test"},
    ], "temperature": 0, "max_tokens": output_tokens})
    if not followup.get("choices") or not followup["choices"][0].get("message", {}).get("content"):
        raise RuntimeError("llama-server tool follow-up failed")
    return model_id


def configure(astrbot: dict, model_id: str, character_id: str, *, model_config: Qwen3Config | None = None,
              prompt_profile: str = "qwen3") -> dict:
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    load_prompts(profile=prompt_profile)
    model_config = model_config or load_qwen3_config()
    source_id = PROVIDER_ID + "_source"
    sources = astrbot.get("provider_sources", [])
    existing = next((s for s in sources if s.get("id") == source_id), {})
    source = {**existing, "id": source_id, "provider": "openai", "type": "openai_chat_completion",
              "provider_type": "chat_completion", "key": ["local"], "api_base": model_config.base_url + "/v1",
              "timeout": 150, "proxy": "", "custom_headers": {}}
    astrbot["provider_sources"] = [s for s in sources if s.get("id") != source_id] + [source]
    providers = [p for p in astrbot.get("provider", []) if p.get("id") != PROVIDER_ID]
    providers.append({"id": PROVIDER_ID, "provider_source_id": source_id, "enable": True,
                      "modalities": ["text", "tool_use"], "model": model_id,
                      "prompt_profile": prompt_profile,
                      "max_context_tokens": model_config.context_size,
                      "context_margin_tokens": model_config.context_margin_tokens,
                      "thinking_phases": model_config.thinking_phases,
                      "phase_temperatures": model_config.phase_temperatures,
                      "custom_extra_body": {"chat_template_kwargs": {"enable_thinking": model_config.enable_thinking},
                                            "reasoning_budget_tokens": model_config.reasoning_budget_tokens,
                                            "temperature": model_config.temperature,
                                            "top_p": model_config.top_p, "top_k": model_config.top_k,
                                            "max_tokens": model_config.max_tokens}})
    astrbot["provider"] = providers
    runner = astrbot.setdefault("agent_runner", {}).setdefault("config", {})
    runner.setdefault("model", {})["provider_id"] = PROVIDER_ID
    runner["model"]["fallback_provider_ids"] = []
    runner["model"]["request_max_retries"] = 1
    runner.setdefault("persona", {})["persona_id"] = character_id
    settings = astrbot.setdefault("provider_settings", {})
    settings["provider_id"] = PROVIDER_ID
    settings["streaming_response"] = False
    settings["show_tool_use_status"] = False
    settings["show_tool_call_result"] = False
    return astrbot


def persona_dialogs(model_config: Qwen3Config) -> list[str]:
    dialogs = []
    if model_config.few_shot_examples <= 0:
        return dialogs
    for line in (ROOT / model_config.examples_path).read_text(encoding="utf-8-sig").splitlines():
        if len(dialogs) >= max(0, model_config.few_shot_examples) * 2:
            break
        if not line.strip():
            continue
        example = json.loads(line)
        # Existing character examples use the legacy speech metadata. Only
        # carry their dialogue into the current native three-field contract.
        answer = example["assistant"]
        dialogs.extend([example["user"], json.dumps({"reply": answer["reply"],
            "thinking_level": 2, "voice": False}, ensure_ascii=False, separators=(",", ":"))])
    return dialogs


def sync_persona(db_path: Path, character_id: str, prompt: str, *, model_config: Qwen3Config | None = None) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    dialogs = json.dumps(persona_dialogs(model_config or load_qwen3_config()), ensure_ascii=False)
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("INSERT INTO personas (persona_id, system_prompt, begin_dialogs, tools, skills, sort_order, created_at, updated_at) "
                   "VALUES (?, ?, ?, 'null', 'null', 0, ?, ?) "
                   "ON CONFLICT(persona_id) DO UPDATE SET system_prompt=excluded.system_prompt, "
                   "begin_dialogs=excluded.begin_dialogs, updated_at=excluded.updated_at",
                   (character_id, prompt, dialogs, timestamp, timestamp))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    active = read_json(ROOT / "model_store/active/qwen3.json")
    character_id = active["character_id"]
    prompt = (ROOT / "configs/characters" / character_id / "system_prompt.txt").read_text(encoding="utf-8-sig").strip()
    model_id = probe()
    astr_path = ASTRBOT / "data/cmd_config.json"
    config = configure(read_json(astr_path), model_id, character_id,
        prompt_profile=active.get("runtime", {}).get("prompt_profile", "qwen3"))
    if args.apply:
        sync_persona(ASTRBOT / "data/data_v4.db", character_id, prompt)
        save_json(astr_path, config)
    print(f"Qwen3 chat and tool probe passed; model ID: {model_id}; character: {character_id}; applied: {args.apply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
