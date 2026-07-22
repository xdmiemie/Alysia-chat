from __future__ import annotations

from array import array

from runtime.audio.audio_format import inspect_wav, write_pcm16_wav


def test_write_and_inspect_stage1_wav(tmp_path):
    samples = array("h", [0, 1200, -1200, 32767, -32768])
    wav_path = write_pcm16_wav(tmp_path / "sample.wav", samples.tobytes(), 16000, 1)

    metrics = inspect_wav(wav_path)

    assert metrics.sample_rate == 16000
    assert metrics.channels == 1
    assert metrics.sample_width_bits == 16
    assert metrics.frame_count == 5
    assert metrics.peak_pcm == 32768
    assert metrics.clipped is True
    assert metrics.clipped_samples == 2
