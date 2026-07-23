from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from runtime.common.config import load_flat_yaml


@dataclass(frozen=True)
class SenseVoiceConfig:
    service_name: str = "sensevoice-service"
    host: str = "127.0.0.1"
    port: int = 5001
    model_manifest: Path = Path("model_store/active/sensevoice.json")
    device: str = "cpu"
    min_duration_ms: int = 300
    silence_peak_threshold: float = 0.002


def load_sensevoice_config(path: str | Path = "configs/runtime/sensevoice.yaml") -> SenseVoiceConfig:
    raw = load_flat_yaml(Path(path))
    return SenseVoiceConfig(
        service_name=str(raw.get("service_name", SenseVoiceConfig.service_name)),
        host=str(raw.get("host", SenseVoiceConfig.host)),
        port=int(raw.get("port", SenseVoiceConfig.port)),
        model_manifest=Path(str(raw.get("model_manifest", SenseVoiceConfig.model_manifest))),
        device=str(raw.get("device", SenseVoiceConfig.device)),
        min_duration_ms=int(raw.get("min_duration_ms", SenseVoiceConfig.min_duration_ms)),
        silence_peak_threshold=float(raw.get("silence_peak_threshold", SenseVoiceConfig.silence_peak_threshold)),
    )
