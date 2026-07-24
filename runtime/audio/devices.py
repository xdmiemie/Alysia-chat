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
            "'powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1'."
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


def select_input_device(preferred: int | None = None, devices: Iterable[AudioDevice] | None = None) -> AudioDevice:
    rows = list(devices if devices is not None else list_devices())
    if preferred is not None:
        for device in rows:
            if device.index == preferred and device.is_input:
                return device
        raise RuntimeError(f"configured input_device is not available as a microphone: {preferred}")

    inputs = [device for device in rows if device.is_input]
    if not inputs:
        raise RuntimeError("no microphone input device found")
    return inputs[0]


def select_output_device(preferred: int | None = None, devices: Iterable[AudioDevice] | None = None) -> AudioDevice:
    rows = list(devices if devices is not None else list_devices())
    if preferred is not None:
        for device in rows:
            if device.index == preferred and device.is_output:
                return device
        raise RuntimeError(f"configured output_device is not available as a speaker: {preferred}")

    outputs = [device for device in rows if device.is_output]
    if not outputs:
        raise RuntimeError("no speaker output device found")
    return outputs[0]


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
