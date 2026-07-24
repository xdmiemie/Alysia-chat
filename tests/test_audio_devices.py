from __future__ import annotations

import pytest

from runtime.audio.devices import AudioDevice, select_input_device, select_output_device


DEVICES = [
    AudioDevice(0, "speaker", 0, 0, 2, 44100.0),
    AudioDevice(1, "mic", 0, 1, 0, 48000.0),
    AudioDevice(2, "headset", 0, 1, 2, 48000.0),
]


def test_select_input_device_uses_first_input_when_preferred_is_none():
    assert select_input_device(None, DEVICES).index == 1


def test_select_output_device_uses_first_output_when_preferred_is_none():
    assert select_output_device(None, DEVICES).index == 0


def test_select_input_device_rejects_non_input_preference():
    with pytest.raises(RuntimeError, match="input_device"):
        select_input_device(0, DEVICES)


def test_select_output_device_rejects_missing_preference():
    with pytest.raises(RuntimeError, match="output_device"):
        select_output_device(99, DEVICES)
