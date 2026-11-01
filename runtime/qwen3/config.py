from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.common.config import load_flat_yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs/runtime/qwen3.yaml"


@dataclass(frozen=True)
class Qwen3Config:
    service_name: str = "llama-server"
    host: str = "127.0.0.1"
    port: int = 8080
    model_manifest: Path = Path("model_store/active/qwen3.json")
    context_size: int = 32768
    gpu_layers: int = 99
    cache_type_k: str = "q8_0"
    cache_type_v: str = "q8_0"
    mmproj_offload: bool = True
    image_min_tokens: int = 1024
    image_max_tokens: int = 4096
    prompt_cache_mib: int = 512
    enable_thinking: bool = True
    thinking_agent: bool = True
    thinking_selection: bool = True
    thinking_followup: bool = False
    thinking_music_result: bool = False
    thinking_compression: bool = False
    reasoning_format: str = "deepseek"
    reasoning_budget_tokens: int = 1024
    context_margin_tokens: int = 256
    temperature: float = 0.7
    utility_temperature: float = 0.0
    result_temperature: float = 0.5
    top_p: float = 0.9
    top_k: int = 20
    max_tokens: int = 1536
    max_history_turns: int = 8
    few_shot_examples: int = 0
    request_timeout_seconds: float = 60.0
    character_config: Path = Path("configs/characters/elysia/character.yaml")
    system_prompt: Path = Path("configs/characters/elysia/system_prompt.txt")
    examples_path: Path = Path("configs/characters/elysia/examples.jsonl")

    def __post_init__(self):
        if not 1 <= self.image_min_tokens <= self.image_max_tokens <= 4096:
            raise ValueError("invalid visual token bounds")
        if not 0 <= self.prompt_cache_mib <= 2048:
            raise ValueError("invalid host prompt cache bound")
        if self.max_tokens <= 0 or self.context_margin_tokens < 0 or self.context_size <= self.max_tokens + self.context_margin_tokens:
            raise ValueError("context_size must leave space for input, output and context margin")
        if not 0 <= self.reasoning_budget_tokens <= self.max_tokens:
            raise ValueError("reasoning_budget_tokens must fit max_tokens")
        cache_types = {"f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"}
        if self.cache_type_k not in cache_types or self.cache_type_v not in cache_types:
            raise ValueError("unsupported KV cache type")

    def server_args(self, *, port: int | None = None) -> list[str]:
        return ["--host", self.host, "--port", str(self.port if port is None else port),
                "--ctx-size", str(self.context_size), "--n-gpu-layers", str(self.gpu_layers),
                "--cache-type-k", self.cache_type_k, "--cache-type-v", self.cache_type_v,
                "--fit", "off", "--flash-attn", "on", "--parallel", "1",
                "--image-min-tokens", str(self.image_min_tokens), "--image-max-tokens", str(self.image_max_tokens),
                "--cache-ram", str(self.prompt_cache_mib),
                "--reasoning", "on" if self.enable_thinking else "off",
                "--reasoning-format", self.reasoning_format,
                "--reasoning-budget", str(self.reasoning_budget_tokens),
                "--temp", str(self.temperature), "--top-p", str(self.top_p), "--top-k", str(self.top_k)]

    @property
    def thinking_phases(self) -> dict[str, bool]:
        return {phase: self.enable_thinking and getattr(self, "thinking_" + phase)
                for phase in ("agent", "selection", "followup", "music_result", "compression")}

    @property
    def phase_temperatures(self) -> dict[str, float]:
        return {phase: self.temperature if phase in {"agent", "selection"} else (
            self.result_temperature if phase in {"followup", "music_result"} else self.utility_temperature)
            for phase in self.thinking_phases}

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def load_qwen3_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Qwen3Config:
    raw = load_flat_yaml(Path(path))
    return Qwen3Config(
        service_name=str(raw.get("service_name", Qwen3Config.service_name)),
        host=str(raw.get("host", Qwen3Config.host)),
        port=int(raw.get("port", Qwen3Config.port)),
        model_manifest=Path(str(raw.get("model_manifest", Qwen3Config.model_manifest))),
        context_size=int(raw.get("context_size", Qwen3Config.context_size)),
        gpu_layers=int(raw.get("gpu_layers", Qwen3Config.gpu_layers)),
        cache_type_k=str(raw.get("cache_type_k", Qwen3Config.cache_type_k)),
        cache_type_v=str(raw.get("cache_type_v", Qwen3Config.cache_type_v)),
        mmproj_offload=bool(raw.get("mmproj_offload", Qwen3Config.mmproj_offload)),
        image_min_tokens=int(raw.get("image_min_tokens", Qwen3Config.image_min_tokens)),
        image_max_tokens=int(raw.get("image_max_tokens", Qwen3Config.image_max_tokens)),
        prompt_cache_mib=int(raw.get("prompt_cache_mib", Qwen3Config.prompt_cache_mib)),
        enable_thinking=bool(raw.get("enable_thinking", Qwen3Config.enable_thinking)),
        **{"thinking_" + phase: bool(raw.get("thinking_" + phase, getattr(Qwen3Config, "thinking_" + phase)))
           for phase in ("agent", "selection", "followup", "music_result", "compression")},
        reasoning_format=str(raw.get("reasoning_format", Qwen3Config.reasoning_format)),
        reasoning_budget_tokens=int(raw.get("reasoning_budget_tokens", Qwen3Config.reasoning_budget_tokens)),
        context_margin_tokens=int(raw.get("context_margin_tokens", Qwen3Config.context_margin_tokens)),
        temperature=float(raw.get("temperature", Qwen3Config.temperature)),
        utility_temperature=float(raw.get("utility_temperature", Qwen3Config.utility_temperature)),
        result_temperature=float(raw.get("result_temperature", Qwen3Config.result_temperature)),
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
