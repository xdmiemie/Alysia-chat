# Companion Robot Prototype

This repository follows the staged plan in `项目阶段要求.md`.

Stage 0 establishes the project skeleton, configuration files, environment
definitions, model manifests, Git repository, and diagnostics. Stages 1-6 build
from local audio loopback to a realtime microphone dialogue app that starts and
stops the required model services.

## Stage Roadmap

Stages 0-17 (except the reserved Stage 14), including the half-duplex AI WeChat
phone pipeline, are complete. The remaining release/control-panel work is
Stage 18.

- Stage 11: Windows WeChat environment, text monitoring, and text sending.
- Stage 12: contact identity, whitelist, session, and memory isolation.
- Stage 13: WeChat voice bubbles, audio capture transaction, 16 kHz WAV
  normalization, and SenseVoice transcription.
- Stage 15: one global GPU inference queue, one shared WeChat UI queue,
  per-contact send ordering, queue metrics, bounded backpressure, and restart
  storm protection helpers.
- Stage 16: incoming WeChat call window detection, caller resolution,
  whitelist/call permission policy, highest-priority UI answer/reject/hangup,
  duplicate suppression, manual takeover, and call-state sync.
- Stage 17: WeChat process loopback, VAD turn segmentation, SenseVoice → Qwen3
  → GPT-SoVITS inference, default-device playback, echo protection, manifests,
  and deterministic audio cleanup.

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
powershell -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -WeChatDesktop -Character elysia
powershell -ExecutionPolicy Bypass -File scripts/stop_runtime.ps1
```

`-GPTSoVITS` starts the official GPT-SoVITS upstream API on port `9880`.
`-GPTSoVITSWrapper` starts this project's optional wrapper API on port `5002`.

## Stage 11 Windows WeChat Text Adapter

Stage 11 uses the normal Windows WeChat client plus a pinned local
`pywechat/pyweixin` checkout. It does not use Tencent iLink, server-side message
protocols, DLL injection, or WeChat process hooks.

Prepare the local dependency manually:

```text
third_party/pywechat/
```

The checkout must provide an importable `pyweixin` module. After validating the
real Windows WeChat version and selector profile, record those values in
`configs/runtime/wechat.yaml`.

Run the safe calibration checklist:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/calibrate_wechat_ui.ps1
```

Configure the allowed test contact in `configs/runtime/wechat.yaml`:

```yaml
contacts:
  default_policy: deny
  allow_contact_keys:
    - stable-contact-key-from-pyweixin
  allow_display_names:
    - 测试好友昵称
```

Start Qwen3, then start the desktop adapter:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -LlamaServer -Character elysia
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -WeChatDesktop -Character elysia
```

What you need to do during Stage 11 testing:

- Log in to Windows WeChat with the dedicated character account.
- Keep the Windows desktop unlocked.
- Keep the WeChat main window open, visible, and preferably in the foreground.
- Pin or keep the allowed test contact visible in the recent chat list.
- Do not manually switch chats while the adapter is sending.
- Send only private text messages for Stage 11; voice, images, files, groups,
  and calls are later stages.

The adapter refuses automatic sending when required UI controls cannot be read.
All desktop send operations run through one UI queue, and logs are written under
`storage/logs/wechat_desktop/` with contact/text redaction by default.

## Stage 12 Contact Isolation Test

Stage 12 assumes test contacts do not share the same WeChat display name by
default. In that lowered test route, the WeChat display name is allowed to act as
the configured contact key. The default local test allowlist contains:

```yaml
contacts:
  default_policy: deny
  allow_display_names:
    - n
    - L
    - 文件传输助手
```

Use two separate WeChat friends as test user A and test user B. Suggested setup:

- Test user A: display name `n`.
- Test user B: display name `L`.
- Optional local self-test contact: display name `文件传输助手`.
- Keep both private chats visible in the recent chat list.
- Alternate messages between A and B; do not use group chats for this stage.

Stage 12 test flow commands:

```powershell
# 1. Optional: stop stale runtime/model processes before a clean test run.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop_runtime.ps1

# 2. Start Qwen3 llama-server in the background for the selected character.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -LlamaServer -Character elysia

# 3. Confirm Qwen3 can answer before connecting WeChat.
powershell -NoProfile -ExecutionPolicy Bypass -Command '. .\scripts\runtime_python.ps1; $Python = Resolve-AlysiaRuntimePython; & $Python scripts/qwen3_chat.py "你好，请用一句话回复我。" --character elysia --json'

# 4. Dump the WeChat UI if the health check fails or after changing WeChat versions.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dump_wechat_ui.ps1

# 5. Start the Stage 12 WeChat desktop bridge.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_wechat_desktop.ps1 -Character elysia

# 6. Watch the WeChat desktop bridge log in another terminal.
Get-Content storage\logs\wechat_desktop\text.jsonl -Tail 120 -Wait

# 7. Stop Qwen3 after testing.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop_runtime.ps1
```

Suggested questions for test user A:

1. `请记住，我今天最喜欢的颜色是蓝色。`
2. `你还记得我刚才说我喜欢什么颜色吗？`
3. `我今天有点累，你能温柔地鼓励我一下吗？`

Suggested questions for test user B:

1. `请记住，我今天想吃草莓蛋糕。`
2. `你还记得我刚才说我想吃什么吗？`
3. `我准备睡觉了，和我说一句晚安吧。`

Expected result: A's reply should only remember A's color, B's reply should only
remember B's dessert, and replies must be sent back to the same contact that sent
the message.

## Stage 13 WeChat Voice Input

Stage 13 accepts bound incoming WeChat voice bubbles from allowlisted private
contacts. The adapter relocates the original bubble, starts audio capture before
clicking playback, validates the captured WAV, normalizes it to 16 kHz mono
PCM16, runs SenseVoice through `AlysiaCore`, and sends a text fallback reply.
Native WeChat voice-bubble replies are no longer part of the project plan.

Relevant config lives under `voice:` in `configs/runtime/wechat.yaml`:

```yaml
voice:
  enabled: true
  capture_backend: application_loopback
  captured_dir: storage/wechat/incoming/captured
  normalized_dir: storage/wechat/incoming/normalized
  failed_dir: storage/wechat/failed
  ffmpeg_path: tools/ffmpeg/bin/ffmpeg.exe
```

The code path is ready for process-level Application Loopback, but this local
runtime builds the native helper locally. Build it once before live voice
capture:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_application_loopback_capture.ps1
```

For manual fallback testing, set `capture_backend: wasapi_loopback` and route
only WeChat audio to a dedicated loopback recording device via
`loopback_input_device`; otherwise captured audio can include unrelated system
sound.

Start Qwen3 and the WeChat desktop bridge:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -LlamaServer -Character elysia
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_wechat_desktop.ps1 -Character elysia
```

Voice capture writes transient files under `storage/wechat/incoming/`; successful
runs keep normalized WAVs and delete raw captures by default. Logs redact contact
fields and do not persist screenshots or raw UI dumps.

For first-pass audio acceptance, run the manual capture script and play one
WeChat voice bubble yourself after the script says capture has started:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/wechat_voice_capture_acceptance.ps1 -CaptureSeconds 12
```

This validates the audio path without relying on automatic UI bubble detection.
Expected output includes:

- `storage/wechat/incoming/captured/manual-<request_id>.wav`
- `storage/wechat/incoming/normalized/manual-<request_id>.wav`
- `storage/wechat/incoming/normalized/manual-<request_id>.json`
- a SenseVoice text line, unless `-SkipTranscribe` is used

To check only normalization and reporting with an already captured file:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/wechat_voice_capture_acceptance.ps1 -CapturedWav path\to\captured.wav -SkipTranscribe
```

After the manual audio path works, run the normal WeChat bridge to validate
automatic bubble binding, click playback, de-duplication, and reply routing.

## Stage 15 Scheduler And Recovery

Stage 15 serializes the expensive and focus-sensitive parts of the runtime:

- `SingleGPUQueue` wraps `AlysiaCore.process(...)` so SenseVoice, Qwen3, and
  GPT-SoVITS work does not overlap by default.
- `SingleUIQueue` is shared by text sending and voice-bubble capture so only one
  WeChat focus-changing transaction runs at a time.
- The UI queue supports pause/resume for manual takeover, bounded pending work,
  per-operation timeout, and queue metrics.
- The GPU queue supports bounded pending work, deterministic priority/FIFO
  order, total message timeout, and cancellation of pending older requests from
  the same session.
- `runtime.scheduler.recovery` provides health-check/restart primitives with
  restart storm protection for model subprocess supervisors.

The relevant WeChat bridge config is under `scheduler:` and `ui:` in
`configs/runtime/wechat.yaml`:

```yaml
scheduler:
  gpu_max_pending: 16
  ui_max_pending: 32
  ui_pause_max_pending: 8
  total_message_seconds: 240
  call_turn_seconds: 180
  cancel_pending_same_session: true

ui:
  operation_timeout_seconds: 15
```

Queue events are logged as redacted JSONL entries in the configured
`storage/logs/wechat_desktop/` log path. Run the focused scheduler tests with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stage15_scheduler.py
```

## Stage 16 WeChat Calls

Stage 16 adds a call-only automation layer under
`runtime/adapters/wechat_desktop/`:

- `call_window_locator.py` detects WeChat voice/video call windows through UI
  Automation or a client-provided `list_call_windows()` hook.
- `call_detection_thread.py` runs a read-only detector thread for top-level
  WeChat call windows. It does not open chats, switch contacts, or click.
- `call_contact_resolver.py` resolves the caller to a stable contact key when
  the client can provide one, with display text as the Stage 12 test fallback.
- `call_policy.py` enforces calls enabled, auto-answer enabled, private
  allowlist, `call_enabled` permission, no active call, no manual takeover,
  health gates, and video-call policy.
- `call_controller.py` performs answer/reject/hangup through the shared
  `SingleUIQueue` at priority `0`, then confirms the call state before
  reporting success.
- `incoming_call_watcher.py` de-duplicates the same call, coordinates policy and
  controller actions, and exposes `ordinary_messages_paused` while a call is in
  the short ringing/answering/ending control phase.

Call automation is explicit at runtime:

```powershell
& $Python -m runtime.adapters.wechat_desktop.app --enable-calls
```

The relevant config lives in `configs/runtime/wechat.yaml`:

```yaml
calls:
  enabled: true
  auto_answer: true
  allow_video_calls: false
  default_policy: deny
  poll_interval_seconds: 0.2
  ring_wait_ms: 0
  max_call_seconds: 1800
  busy_behavior: ignore
  model_unhealthy_behavior: ignore

contacts:
  allow_contact_keys: []
  call_enabled_contact_keys: []
  call_disabled_contact_keys: []
```

Run the focused call tests with:

```powershell
runtime_env\python.exe -m pytest tests\test_stage16_wechat_calls.py
```

## Stage 17 Half-Duplex WeChat Calls

Stage 17 starts automatically after a Stage 16 allowlisted call is answered.
It captures only WeChat process output, stops capture during inference and TTS
playback, then waits 450 ms before listening again. Ordinary WeChat UI work is
paused for the full AI-call state sequence. Call manifests and summaries are
stored under `storage/wechat/calls/<call_id>/`; input/output WAV files are
deleted after each turn unless `calls.retain_audio` is enabled.

The send side is bound by stable endpoint name to `CABLE Input (VB-Audio
Virtual Cable)` through `calls.output_device_name`; PortAudio numeric indices
may change after reboot. Select the paired `CABLE Output` recording endpoint as
the WeChat microphone. Windows may keep the physical headset as its default
input/output because the Stage 17 route no longer depends on those defaults.

Build the streaming helper once after cloning or after changing its source:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_application_loopback_capture.ps1
```

Start the adapter with the managed Stage 17 launcher:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_wechat_desktop.ps1 -Character elysia -EnableCalls
```

This command starts Qwen3 and GPT-SoVITS when their ports are not already in
use. SenseVoice is loaded in-process by the adapter. On `Ctrl+C` or any normal
exit, the launcher stops only the Qwen3/GPT-SoVITS process trees that it started;
services that were already running before launch are reused and left running.
Pass `-NoServiceManagement` only when intentionally managing all model services
from another terminal.

The focused automated acceptance suite is:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_stage17_wechat_calls.py tests\test_stage16_wechat_calls.py tests\test_stage13_wechat_voice.py
```

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
