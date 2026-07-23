# Companion Robot Prototype

This repository follows the staged plan in `项目阶段要求.md`.

Stage 0 establishes the project skeleton, configuration files, environment
definitions, model manifests, Git repository, and diagnostics. Stage 1 implements
a local microphone-to-speaker loopback. It does not call ASR, LLM, or TTS
services yet.

## Stage Roadmap

Stages 0-5 are complete. The remaining plan is:

- Stage 6: microphone streaming input, speaker streaming output, and one app
  entry that starts and stops required model services.
- Stage 7: Qwen3 role-training pipeline with dataset-swappable configs,
  resume support, and checkpoint retention.
- Stage 8: TTS training pipeline with the same dataset, resume, and checkpoint
  controls.
- Stage 9: selectable character/model loading from the Stage 6 app entry.

## Stage 0 Doctor

```powershell
conda create -p .venv python=3.10 -y
.venv\Scripts\python.exe -m pip install -r runtime/audio/requirements.txt pytest
.venv\Scripts\python.exe scripts/doctor.py
```

The doctor checks:

- NVIDIA GPU and CUDA availability
- FFmpeg on `PATH`
- microphone and speaker devices
- runtime ports `8000`, `5001`, `8080`, `9880`, and `5002`
- SenseVoice, Qwen3, and GPT-SoVITS model files from `model_store/active/*.json`

Use strict mode when you want a failing exit code for missing hardware, occupied
ports, or missing model files:

```powershell
.venv\Scripts\python.exe scripts/doctor.py --strict
```

The third-party source locations are reserved under `third_party/`, and their
upstream repositories are tracked in `configs/third_party.yaml`.

## Runtime Scripts

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -AudioLoopback
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -LlamaServer
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -Qwen3
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITS
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITSWrapper
powershell -ExecutionPolicy Bypass -File scripts/stop_runtime.ps1
```

`-GPTSoVITS` starts the official GPT-SoVITS upstream API on port `9880`.
`-GPTSoVITSWrapper` starts this project's optional wrapper API on port `5002`.

## Install stage 1 dependencies

```powershell
conda create -p .venv python=3.10 -y
.venv\Scripts\python.exe -m pip install -r runtime/audio/requirements.txt
```

## List audio devices

```powershell
.venv\Scripts\python.exe scripts/audio_loopback.py --list-devices
```

Put the desired device indexes into `configs/runtime/audio.yaml`, or override
them on the command line:

```powershell
.venv\Scripts\python.exe scripts/audio_loopback.py --input-device 1 --output-device 3
```

## Run the stage 1 acceptance loop

```powershell
.venv\Scripts\python.exe scripts/audio_loopback.py
```

Flow:

```text
Press Enter to start recording
Speak into the microphone
Press Enter again to stop
The WAV file is saved and played back automatically
```

Recordings are 16 kHz, 16-bit, mono PCM/WAV files. Each recording gets a sibling
JSON metrics file containing duration, sample rate, peak volume, clipping status,
and saved path.

## Stage 2 SenseVoice Transcription

Install the lightweight service dependencies:

```powershell
.venv\Scripts\python.exe -m pip install -r runtime/sensevoice/requirements.txt
```

Install the model backend dependencies in the dedicated SenseVoice environment
when you are ready to run the real model:

```powershell
conda env create -f envs/sensevoice.yml
```

Put the SenseVoiceSmall model files under the path referenced by
`model_store/active/sensevoice.json`, then run:

```powershell
.venv\Scripts\python.exe scripts/download_sensevoice_model.py
.venv\Scripts\python.exe scripts/sensevoice_transcribe.py test_audio.WAV --json
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -SenseVoice
```

The service exposes:

```http
POST /v1/audio/transcriptions
```

## Stage 3 Qwen3 Character Chat

Prepare a Qwen3 GGUF model at the path referenced by
`model_store/active/qwen3.json`.

```powershell
winget install llama.cpp
.venv\Scripts\python.exe -m pip install -r runtime/qwen3/requirements.txt
.venv\Scripts\python.exe scripts/download_qwen3_model.py
```

Start the local `llama-server` before using the chat client:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -LlamaServer
```

In another terminal, send a single message, run the acceptance check, or start
an interactive chat:

```powershell
.venv\Scripts\python.exe scripts/qwen3_chat.py "今天有点累" --json
.venv\Scripts\python.exe scripts/qwen3_acceptance.py
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -Qwen3
```

Stop the local Qwen3 service when you are done testing:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop_runtime.ps1
```

Qwen3 service logs are written to:

```text
storage/logs/qwen3-llama-server.err.log
storage/logs/qwen3-llama-server.out.log
```

The chat client sends `/no_think`, keeps the last 8 turns, requires structured
JSON, and retries once when the model returns invalid JSON or `<think>`.

## Stage 4 GPT-SoVITS Speech

Prepare GPT-SoVITS weights and reference audio under the paths listed in
`model_store/active/gpt_sovits_elysia.json`:

```text
model_store/registry/gpt_sovits/elysia/base-v1/gpt.ckpt
model_store/registry/gpt_sovits/elysia/base-v1/sovits.pth
model_store/registry/gpt_sovits/elysia/base-v1/references/neutral.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/happy.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/gentle.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/sad.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/surprised.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/serious.wav
```

Put the exact transcript for each reference clip in
`configs/characters/elysia/reference_texts.yaml`, then start the upstream
GPT-SoVITS API at `http://127.0.0.1:9880/tts`.

Check that all required weights and reference clips are present:

```powershell
.venv\Scripts\python.exe scripts/check_gpt_sovits_assets.py
```

Start the official upstream GPT-SoVITS API. This is the service used by the
stage 4 CLI scripts, and it listens on port `9880`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITS
```

The optional project wrapper listens on port `5002`:

```powershell
.venv\Scripts\python.exe -m pip install -r runtime/gpt_sovits/requirements.txt
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITSWrapper
```

Generate a single WAV or run the fixed 20-sentence acceptance set:

```powershell
.venv\Scripts\python.exe scripts/gpt_sovits_speech.py "辛苦啦，今天先休息一下。" --emotion gentle
.venv\Scripts\python.exe scripts/gpt_sovits_acceptance.py
```

The wrapper exposes:

```http
POST /v1/audio/speech
```

The request body is:

```json
{
  "text": "辛苦啦，今天是不是发生了什么让你很累的事情？",
  "character_id": "elysia",
  "emotion": "gentle",
  "speed": 0.95,
  "format": "wav"
}
```

## Stage 4 Current Runbook

The current Elysia GPT-SoVITS assets are:

```text
model_store/registry/gpt_sovits/elysia/base-v1/Elysia_v2-e15.ckpt
model_store/registry/gpt_sovits/elysia/base-v1/Elysia_v2_e24_s13080.pth
model_store/registry/gpt_sovits/elysia/base-v1/references/neutral_trimmed.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/happy.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/gentle.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/sad.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/surprised.wav
model_store/registry/gpt_sovits/elysia/base-v1/references/serious.wav
```

Each reference clip needs a same-name `.txt` transcript beside the `.wav`.
GPT-SoVITS requires reference audio to be between 3 and 10 seconds; the neutral
emotion uses `neutral_trimmed.wav` because the original `neutral.wav` is longer
than 10 seconds.

Start the official upstream API on port `9880`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITS
```

This is equivalent to running:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_gpt_sovits_upstream.ps1
```

Stop it with `Ctrl+C`. In another terminal, generate one WAV or run acceptance:

```powershell
.venv\Scripts\python.exe scripts/gpt_sovits_speech.py "text to synthesize" --emotion gentle
.venv\Scripts\python.exe scripts/gpt_sovits_acceptance.py
```

Optional one-shot smoke check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_gpt_sovits_stage4_smoke.ps1
```

Windows compatibility packages used by this setup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/patch_gpt_sovits_windows.ps1
& "C:\Users\MLTZ\miniconda3\envs\GPTSoVits\Scripts\pip.exe" install torchcodec opencc-python-reimplemented
& "C:\Users\MLTZ\miniconda3\envs\GPTSoVits\python.exe" -c "import nltk; nltk.download('averaged_perceptron_tagger_eng'); nltk.download('averaged_perceptron_tagger'); nltk.download('cmudict')"
```

## Stage 5 File Dialogue Loop

Stage 5 temporarily skips microphone input and speaker playback. It reads WAV
questions from `storage/generated_audio/dialogue_questions_20`, runs them through
SenseVoice, Qwen3, and GPT-SoVITS in order, then writes generated WAV replies to
`storage/generated_audio/stage5_dialogue/<timestamp>/`.

Start the required model services first:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -LlamaServer
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITS
```

Stage 5 talks directly to:

```text
Qwen3 llama-server: http://127.0.0.1:8080
GPT-SoVITS upstream: http://127.0.0.1:9880/tts
```

It does not require the project GPT-SoVITS wrapper on port `5002`.
SenseVoice is loaded in-process from the local model manifest.

Then run:

```powershell
.venv\Scripts\python.exe scripts/dialogue_stage5.py
```

Useful options:

```powershell
.venv\Scripts\python.exe scripts/dialogue_stage5.py --limit 3 --json
.venv\Scripts\python.exe scripts/dialogue_stage5.py --output-dir storage/generated_audio/stage5_dialogue/manual_run
```

Each run writes a `manifest.jsonl` with request IDs, state transitions,
transcribed text, reply metadata, output WAV paths, errors, and per-stage
timings. The stage 5 loop does not play audio directly.
