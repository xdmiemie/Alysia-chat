# Architecture

The runtime is split into separate processes:

- `companion-app` for recording, playback, orchestration, and state control.
- `sensevoice-service` for ASR and user emotion/event recognition.
- `llama-server` for Qwen3 inference through an OpenAI-compatible API.
- `gpt-sovits-service` for character TTS.
