# Companion Robot Prototype

This repository follows the staged plan in `项目阶段要求.md`.

Stage 0 establishes the project skeleton, configuration files, environment
definitions, model manifests, Git repository, and diagnostics. Stages 1-6 build
from local audio loopback to a realtime microphone dialogue app that starts and
stops the required model services.

## Stage Roadmap

Stages 0-10 are complete. The remaining plan starts with the WeChat adapter
stages:

- Stage 11: WeChat text message receive/send.
- Stage 12: WeChat voice receive and SenseVoice transcription.
- Stage 13: WeChat audio file/native voice replies.

## Clone-To-Code Quickstart

For a new machine, use the root-level `quickstart/` scripts to make the code
runtime self-initializing. Runtime now uses a single Python environment centered
on GPT-SoVITS compatibility. The default Python path is the project-local
`runtime_env` interpreter: `runtime_env/python.exe` for a packed conda-style
environment, or `runtime_env/Scripts/python.exe` for a venv built by
`quickstart/bootstrap.ps1`. Explicit overrides can still be provided through
`-Python`, `ALYSIA_PYTHON`, or `GPT_SOVITS_PYTHON`. This does not bundle GPU
drivers, but it pulls Git LFS files, clones the GPT-SoVITS upstream source,
downloads project-local FFmpeg and llama.cpp tools, creates/reuses the
project-local unified runtime environment, installs the Python dependencies,
downloads the Qwen3 and SenseVoice model files, runs the doctor, and then starts
the app through the normal Stage 6 entry.

Initialize code dependencies only:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1
```

Initialize if needed and run the realtime app:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/run.ps1 -Character elysia
```

Choose a Qwen3 model during quickstart startup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/run.ps1 -Character elysia -Qwen3Interactive
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/run.ps1 -Character elysia -Qwen3Model qwen3-8b-q4-k-m-base-v1
```

Qwen3 training dependencies are intentionally separate from normal runtime
bootstrap. The HF-format training base model is separate too. Run bootstrap
first, then install only the training extras and download the training base:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/install_qwen3_training_deps.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/download_qwen3_training_model.ps1
```

The training dependency script is idempotent: if bootstrap or a previous run
already installed CUDA torch and the Qwen3 training packages, it exits without
reinstalling them. It also keeps downloaded wheels under
`model_store/cache/qwen3_training_wheels/` and pip cache under
`model_store/cache/pip/`, so rerunning after an interrupted download reuses
completed files. Use `-CheckOnly` to inspect the training environment, or
`-DownloadOnly` to prefetch wheels without installing.

The HF training model download also uses a project-local cache:

```text
model_store/cache/huggingface/
model_store/cache/qwen3/hf/Qwen3-8B/
```

Rerun `quickstart/download_qwen3_training_model.ps1` after an interrupted
download; completed files and Hugging Face cache entries are reused. Use
`-VerifyOnly` to check missing shards without downloading, and use
`-ForceDownload` only when you intentionally want to refresh the model files.

For a lighter setup that skips the heavy SenseVoice model dependency install:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1 -Lightweight
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/run.ps1 -Character elysia -LightweightBootstrap
```

For an offline or code-only setup that skips model downloads:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1 -SkipLfsPull -SkipThirdParty -SkipModelDownloads
```

Use the skip switches only when the corresponding files are already present or
you only want to prepare code dependencies; the realtime app still requires Git
LFS TTS files, GPT-SoVITS source, Qwen3, and SenseVoice before it can run.

External runtime items are still checked or reported by the scripts:

- `tools/ffmpeg/bin/ffmpeg.exe`
- `tools/llama.cpp/**/llama-server.exe`
- `third_party/GPT-SoVITS`
- unified Python from `runtime_env/python.exe` or `runtime_env/Scripts/python.exe`
- model files referenced by `model_store/active/*.json`; Qwen3 and SenseVoice
  are downloaded by bootstrap, and the current TTS base is tracked in Git/LFS

For manual Python commands in the sections below, resolve the unified runtime
Python once:

```powershell
$Python = (& powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\scripts\runtime_python.ps1; Resolve-AlysiaRuntimePython").Trim()
```

`runtime_python/` and `runtime_env/` are local runtime artifacts and are ignored
by Git. To recreate them on another machine without conda, run
`quickstart/bootstrap.ps1`. The script first looks for system Python 3.10; if it
is missing, it downloads official CPython 3.10.11 for Windows into
`runtime_python/`. It then creates `runtime_env` with Python 3.10 and installs
the required packages there. You can also copy/pack a compatible environment into
`runtime_env/`; run `runtime_env/Scripts/conda-unpack.exe` only when the copied
environment came from `conda-pack`.

## Stage 0 Doctor

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -Doctor
```

The doctor checks:

- NVIDIA GPU and CUDA availability
- project-local FFmpeg under `tools/ffmpeg`
- project-local llama-server under `tools/llama.cpp`
- microphone and speaker devices
- runtime ports `8000`, `5001`, `8080`, `9880`, and `5002`
- SenseVoice, Qwen3, and GPT-SoVITS model files from `model_store/active/*.json`

Use strict mode when you want a failing exit code for missing hardware, occupied
ports, or missing model files:

```powershell
& $Python scripts/doctor.py --strict
```

Third-party source checkouts and project-local binary tool locations are tracked
in `configs/third_party.yaml`.

## Runtime Scripts

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_app.ps1 -Character elysia
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

## Stage 6 Realtime App

The main app entry starts Qwen3 `llama-server` on port `8080` and the official
GPT-SoVITS upstream API on port `9880` when they are not already running. It
waits for health checks, starts the Python microphone dialogue loop, and stops
only the model processes it started when you press `Ctrl+C` or the app exits.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_app.ps1 -Character elysia
```

For a bounded acceptance run, stop after 20 complete microphone-to-speaker turns:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_app.ps1 -Character elysia -MaxTurns 20
```

The Python app can also be run directly when Qwen3 and GPT-SoVITS are already
up:

```powershell
& $Python -m runtime.app.main --character-id elysia --max-turns 3
```

Microphone capture uses 16 kHz mono PCM, 30 ms VAD frames, 450 ms pre-roll, and
WebRTC VAD when available. If `webrtcvad` is not installed, it falls back to RMS
energy with an adaptive noise floor. Generated replies are played immediately
while microphone listening is paused, and per-turn audio plus `manifest.jsonl`
are written under `storage/generated_audio/stage6_dialogue/<timestamp>/`.
Captured utterances, metrics, and VAD event logs are written under
`storage/recordings/stage6/`.

## Install stage 1 dependencies

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1
```

## List audio devices

```powershell
& $Python scripts/audio_loopback.py --list-devices
```

Put the desired device indexes into `configs/runtime/audio.yaml`, or override
them on the command line:

```powershell
& $Python scripts/audio_loopback.py --input-device 1 --output-device 3
```

## Run the stage 1 acceptance loop

```powershell
& $Python scripts/audio_loopback.py
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
& $Python -m pip install -r runtime/sensevoice/requirements.txt
```

Install the model backend dependencies in the unified runtime environment when
you are ready to run the real model:

```powershell
& $Python -m pip install -r runtime/sensevoice/requirements-model.txt
```

Put the SenseVoiceSmall model files under the path referenced by
`model_store/active/sensevoice.json`, or let `quickstart/bootstrap.ps1` download
them automatically. Manual download is still available:

```powershell
& $Python scripts/download_sensevoice_model.py
& $Python scripts/sensevoice_transcribe.py test_audio.WAV --json
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -SenseVoice
```

The service exposes:

```http
POST /v1/audio/transcriptions
```

## Stage 3 Qwen3 Character Chat

Prepare a Qwen3 GGUF model at the path referenced by
`model_store/active/qwen3.json`, or let `quickstart/bootstrap.ps1` download it
automatically.

```powershell
& $Python -m pip install -r runtime/qwen3/requirements.txt
& $Python scripts/download_qwen3_model.py
```

Start the local `llama-server` before using the chat client:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -LlamaServer
```

In another terminal, send a single message, run the acceptance check, or start
an interactive chat:

```powershell
& $Python scripts/qwen3_chat.py "今天有点累" --json
& $Python scripts/qwen3_acceptance.py
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

## Stage 7 Qwen3 Role Training

Elysia training data lives under `training/qwen3/datasets/elysia/raw/Elysia.jsonl`.
Normal quickstart runtime does not install every training package. Install the
training extras after bootstrap before a real QLoRA run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/install_qwen3_training_deps.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/download_qwen3_training_model.ps1
```

Qwen3 HF training requires `transformers>=4.51.0`; older versions fail with
`KeyError: 'qwen3'`.

Create a new role scaffold. This creates the Qwen3 text-training files and the
matching GPT-SoVITS voice-training config/directories:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/add_qwen3_character.ps1 -Character fish -DisplayName Fish
```

Then put role data at the path printed by the script, train with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_qwen3.ps1 -Character fish
```

Put voice WAV clips under the printed GPT-SoVITS `raw/` directory, then prepare
or train with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_gpt_sovits.ps1 -Character fish -ValidateOnly
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_gpt_sovits.ps1 -Character fish
```

After training/exporting, run and select the character/model:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/run.ps1 -Character fish -Qwen3Interactive
```

The run wrapper selects the Qwen3 role/model first, then selects the
GPT-SoVITS voice model for the same character. Pass `-GPTSoVITSModel <model-id>`
to skip the voice-model prompt.

`hf_path` points at the Hugging Face safetensors base model used for training:

```text
model_store/cache/qwen3/hf/Qwen3-8B
```

The runtime GGUF stays under `model_store/registry/qwen3/...`; GGUF is for
llama.cpp inference and cannot be fine-tuned directly.

Prepare the dataset and run training through the wrapper script:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_qwen3.ps1 -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_qwen3.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_qwen3.ps1 -FromScratch
```

Only prepare the dataset:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_qwen3.ps1 -PrepareOnly
```

The wrapper skips normalize/split when the raw dataset and training config have
not changed. Use `-ForcePrepare` to regenerate dataset outputs.

The role model list is `model_store/registry/qwen3/elysia/models.json`.
Training exports register into that list and keep only the latest five trained
models for the role; the base model is retained. Start Qwen3 with interactive
role/model selection:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_qwen3_server.ps1
```

Or choose non-interactively:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_qwen3_server.ps1 -Character elysia -ModelVersion qwen3-8b-q4-k-m-base-v1
```

## Stage 8 GPT-SoVITS Voice Training

Stage 8 adds the GPT-SoVITS training pipeline scaffold: raw WAV discovery,
SenseVoice transcript generation, dataset validation, normalized manifest
generation, S1/S2 resume metadata, checkpoint retention, reference selection,
model registration, and activation.

Voice data is private local input and is ignored by Git. Put Elysia data here:

```text
training/gpt_sovits/datasets/elysia/raw/
```

File names are not fixed. The pipeline recursively reads every `.wav` under
that directory, uses the project SenseVoice model to generate transcripts, and
writes the generated training manifests under `training/gpt_sovits/datasets/elysia/work/`.

Install Stage 8 training-only dependencies when bootstrap was run in a light
mode or when the training checks report missing modules:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/install_gpt_sovits_training_deps.ps1
```

Prepare and validate the dataset:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_gpt_sovits.ps1 -ValidateOnly
```

Run the combined prepare-and-train pipeline in dry-run mode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_gpt_sovits.ps1 -DryRun
```

For real training, run the same wrapper without `-DryRun`. The checked-in
`s1.command` and `s2.command` entries call Alysia's recipe runner, which
generates the upstream GPT-SoVITS filelist/configs from the processed raw WAV
dataset at runtime:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/train_gpt_sovits.ps1
```

The fixed recipe commands receive these environment variables:

```text
ALYSIA_PROJECT_ROOT
ALYSIA_GPT_SOVITS_CONFIG
ALYSIA_GPT_SOVITS_RUN_DIR
ALYSIA_GPT_SOVITS_DATASET
ALYSIA_GPT_SOVITS_RESUME
```

After real training, the wrapper automatically selects reference clips and
registers the latest S1 `.ckpt` plus S2 `.pth` from the run directory into
`model_store/registry/gpt_sovits/<character>/`. Use `-Activate` to immediately
switch the character to the newly registered model, or `-SkipRegister` to leave
the produced weights only under the training run directory.

Manual collection is still available for a custom weight pair:

```powershell
python training/gpt_sovits/export/collect_weights.py --config training/gpt_sovits/configs/characters/elysia.yaml --gpt-weight path/to/model.ckpt --sovits-weight path/to/model.pth --references-dir training/gpt_sovits/outputs/elysia/elysia-gpt-sovits/references
```

Activate a registered candidate only after smoke validation:

```powershell
python training/gpt_sovits/export/activate_model.py --character elysia --model <model-id>
```

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
& $Python scripts/check_gpt_sovits_assets.py
```

Start the official upstream GPT-SoVITS API. This is the service used by the
stage 4 CLI scripts, and it listens on port `9880`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITS
```

The optional project wrapper listens on port `5002`:

```powershell
& $Python -m pip install -r runtime/gpt_sovits/requirements.txt
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -GPTSoVITSWrapper
```

Generate a single WAV or run the fixed 20-sentence acceptance set:

```powershell
& $Python scripts/gpt_sovits_speech.py "辛苦啦，今天先休息一下。" --emotion gentle
& $Python scripts/gpt_sovits_acceptance.py
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
& $Python scripts/gpt_sovits_speech.py "text to synthesize" --emotion gentle
& $Python scripts/gpt_sovits_acceptance.py
```

Optional one-shot smoke check:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/run_gpt_sovits_stage4_smoke.ps1
```

Windows compatibility packages used by this setup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/patch_gpt_sovits_windows.ps1
& $Python -m pip install torchcodec opencc-python-reimplemented
& $Python -c "import nltk; nltk.download('averaged_perceptron_tagger_eng'); nltk.download('averaged_perceptron_tagger'); nltk.download('cmudict')"
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
& $Python scripts/dialogue_stage5.py
```

Useful options:

```powershell
& $Python scripts/dialogue_stage5.py --limit 3 --json
& $Python scripts/dialogue_stage5.py --output-dir storage/generated_audio/stage5_dialogue/manual_run
```

Each run writes a `manifest.jsonl` with request IDs, state transitions,
transcribed text, reply metadata, output WAV paths, errors, and per-stage
timings. The stage 5 loop does not play audio directly.
