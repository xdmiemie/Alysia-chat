# Architecture

The runtime is split into separate processes:

- `companion-app` for recording, playback, orchestration, and state control.
- `sensevoice-service` for ASR and user emotion/event recognition.
- `llama-server` for Qwen3 inference through an OpenAI-compatible API.
- `gpt-sovits-upstream` for the official GPT-SoVITS character TTS API on port
  `9880`.
- `gpt-sovits-wrapper` for the optional project wrapper API on port `5002`.
