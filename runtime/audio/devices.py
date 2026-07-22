from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    hostapi: int
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.max_output_channels > 0


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'sounddevice'. Install stage 1 requirements with "
            "'python -m pip install -r runtime/audio/requirements.txt'."
        ) from exc
    return sd


def list_devices() -> list[AudioDevice]:
    sd = _sounddevice()
    devices = []
    for index, device in enumerate(sd.query_devices()):
        devices.append(
            AudioDevice(
                index=index,
                name=_single_line(str(device["name"])),
                hostapi=int(device["hostapi"]),
                max_input_channels=int(device["max_input_channels"]),
                max_output_channels=int(device["max_output_channels"]),
                default_sample_rate=float(device["default_samplerate"]),
            )
        )
    return devices


def _single_line(value: str) -> str:
    return " ".join(value.split())


def print_devices(devices: Iterable[AudioDevice] | None = None) -> None:
    rows = list(devices if devices is not None else list_devices())
    if not rows:
        print("No audio devices detected.")
        return

    print("Audio devices:")
    print("  ID  IN  OUT  DEFAULT_SR  NAME")
    for device in rows:
        print(
            f"  {device.index:>2}  "
            f"{device.max_input_channels:>2}  "
            f"{device.max_output_channels:>3}  "
            f"{device.default_sample_rate:>10.0f}  "
            f"{device.name}"
        )
