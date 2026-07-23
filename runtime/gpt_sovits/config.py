from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.common.config import load_flat_yaml


@dataclass(frozen=True)
class GPTSoVITSConfig:
    service_name: str = "gpt-sovits-service"
    host: str = "127.0.0.1"
    port: int = 5002
    model_manifest: Path = Path("model_store/active/gpt_sovits_elysia.json")
    character_id: str = "elysia"
    emotion_map: Path = Path("configs/characters/elysia/emotion_map.yaml")
    reference_texts: Path = Path("configs/characters/elysia/reference_texts.yaml")
    reference_dir: Path = Path("model_store/registry/gpt_sovits/elysia/base-v1/references")
    generated_dir: Path = Path("storage/generated_audio")
    upstream_url: str = "http://127.0.0.1:9880/tts"
    text_lang: str = "zh"
    prompt_lang: str = "zh"
    max_sentence_chars: int = 80
    request_timeout_seconds: float = 120.0


def load_gpt_sovits_config(path: str | Path = "configs/runtime/gpt_sovits.yaml") -> GPTSoVITSConfig:
    raw = load_flat_yaml(Path(path))
    return GPTSoVITSConfig(
        service_name=str(raw.get("service_name", GPTSoVITSConfig.service_name)),
        host=str(raw.get("host", GPTSoVITSConfig.host)),
        port=int(raw.get("port", GPTSoVITSConfig.port)),
        model_manifest=Path(str(raw.get("model_manifest", GPTSoVITSConfig.model_manifest))),
        character_id=str(raw.get("character_id", GPTSoVITSConfig.character_id)),
        emotion_map=Path(str(raw.get("emotion_map", GPTSoVITSConfig.emotion_map))),
        reference_texts=Path(str(raw.get("reference_texts", GPTSoVITSConfig.reference_texts))),
        reference_dir=Path(str(raw.get("reference_dir", GPTSoVITSConfig.reference_dir))),
        generated_dir=Path(str(raw.get("generated_dir", GPTSoVITSConfig.generated_dir))),
        upstream_url=str(raw.get("upstream_url", GPTSoVITSConfig.upstream_url)),
        text_lang=str(raw.get("text_lang", GPTSoVITSConfig.text_lang)),
        prompt_lang=str(raw.get("prompt_lang", GPTSoVITSConfig.prompt_lang)),
        max_sentence_chars=int(raw.get("max_sentence_chars", GPTSoVITSConfig.max_sentence_chars)),
        request_timeout_seconds=float(
            raw.get("request_timeout_seconds", GPTSoVITSConfig.request_timeout_seconds)
        ),
    )
