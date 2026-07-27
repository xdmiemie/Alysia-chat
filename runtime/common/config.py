from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    sample_width_bits: int = 16
    input_device: int | None = None
    output_device: int | None = None
    recordings_dir: Path = Path("storage/recordings")
    play_after_recording: bool = True


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"null", "None", "~", ""}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def load_flat_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if not path.exists():
        return data

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = _parse_scalar(value)
    return data


def load_audio_config(path: str | Path = "configs/runtime/audio.yaml") -> AudioConfig:
    raw = load_flat_yaml(Path(path))
    config = AudioConfig(
        sample_rate=int(raw.get("sample_rate", AudioConfig.sample_rate)),
        channels=int(raw.get("channels", AudioConfig.channels)),
        sample_width_bits=int(raw.get("sample_width_bits", AudioConfig.sample_width_bits)),
        input_device=raw.get("input_device"),
        output_device=raw.get("output_device"),
        recordings_dir=Path(str(raw.get("recordings_dir", AudioConfig.recordings_dir))),
        play_after_recording=bool(raw.get("play_after_recording", True)),
    )

    if config.sample_rate != 16000:
        raise ValueError("Stage 1 requires 16 kHz recording.")
    if config.channels != 1:
        raise ValueError("Stage 1 requires mono recording.")
    if config.sample_width_bits != 16:
        raise ValueError("Stage 1 requires 16-bit PCM recording.")
    return config
