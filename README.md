# Companion Robot Prototype

This repository follows the staged plan in `项目阶段要求.md`.

Stage 0 establishes the project skeleton, configuration files, environment
definitions, model manifests, Git repository, and diagnostics. Stage 1 implements
a local microphone-to-speaker loopback. It does not call ASR, LLM, or TTS
services yet.

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
- runtime ports `8000`, `5001`, `8080`, and `5002`
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
powershell -ExecutionPolicy Bypass -File scripts/stop_runtime.ps1
```

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
