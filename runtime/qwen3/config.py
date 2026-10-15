from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path

from runtime.common.config import load_flat_yaml


@dataclass(frozen=True)
class Qwen3Config:
    service_name: str = "llama-server"
    host: str = "127.0.0.1"
    port: int = 8080
    model_manifest: Path = Path("model_store/active/qwen3.json")
    context_size: int = 8192
    temperature: float = 0.7
    top_p: float = 0.9
    top_k: int = 20
    max_tokens: int = 256
    max_history_turns: int = 8
    few_shot_examples: int = 6
    request_timeout_seconds: float = 60.0
    character_config: Path = Path("configs/characters/elysia/character.yaml")
    system_prompt: Path = Path("configs/characters/elysia/system_prompt.txt")
    examples_path: Path = Path("configs/characters/elysia/examples.jsonl")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_qwen3_config(path: str | Path = "configs/runtime/qwen3.yaml") -> Qwen3Config:
    raw = load_flat_yaml(Path(path))
    return Qwen3Config(
        service_name=str(raw.get("service_name", Qwen3Config.service_name)),
        host=str(raw.get("host", Qwen3Config.host)),
        port=int(raw.get("port", Qwen3Config.port)),
        model_manifest=Path(str(raw.get("model_manifest", Qwen3Config.model_manifest))),
        context_size=int(raw.get("context_size", Qwen3Config.context_size)),
        temperature=float(raw.get("temperature", Qwen3Config.temperature)),
        top_p=float(raw.get("top_p", Qwen3Config.top_p)),
        top_k=int(raw.get("top_k", Qwen3Config.top_k)),
        max_tokens=int(raw.get("max_tokens", Qwen3Config.max_tokens)),
        max_history_turns=int(raw.get("max_history_turns", Qwen3Config.max_history_turns)),
        few_shot_examples=int(raw.get("few_shot_examples", Qwen3Config.few_shot_examples)),
        request_timeout_seconds=float(
            raw.get("request_timeout_seconds", Qwen3Config.request_timeout_seconds)
        ),
        character_config=Path(str(raw.get("character_config", Qwen3Config.character_config))),
        system_prompt=Path(str(raw.get("system_prompt", Qwen3Config.system_prompt))),
        examples_path=Path(str(raw.get("examples_path", Qwen3Config.examples_path))),
    )


def with_character_generation_settings(config: Qwen3Config) -> Qwen3Config:
    """Use a role's sampling settings while retaining global values for old profiles."""
    raw = load_flat_yaml(config.character_config)
    temperature = float(raw["temperature"]) if raw.get("temperature") is not None else config.temperature
    top_p = float(raw["top_p"]) if raw.get("top_p") is not None else config.top_p
    max_tokens = int(raw["max_tokens"]) if raw.get("max_tokens") is not None else config.max_tokens
    if not isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("角色 Temperature 必须在 0 到 2 之间。")
    if not isfinite(top_p) or not 0 < top_p <= 1:
        raise ValueError("角色 Top P 必须大于 0 且不超过 1。")
    if max_tokens < 1:
        raise ValueError("角色 Max tokens 必须大于 0。")
    return replace(config, temperature=temperature, top_p=top_p, max_tokens=max_tokens)
