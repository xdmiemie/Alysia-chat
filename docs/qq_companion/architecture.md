# QQ Q0–Q1 architecture

```
NTQQ (user managed)
  ↕ local attachment
SnowLuma v2.1.1 (external/SnowLuma, Node 24.22.1)
  → OneBot v11 reverse WS, ws://127.0.0.1:6199/ws, shared token
AstrBot v4.29.3 (external/AstrBot, isolated Python 3.12)
  → astrbot_chat_q1 guard → built-in AstrBot event pipeline
  → optional local llama-server on 127.0.0.1:8080
```

AstrBot owns QQ messages, the Agent and conversation history. The old `AstrbotChatCore.process()` remains for the Stage 10 local entry point and is never called in the QQ pipeline. No Q2 model provider, ASR, TTS or vision integration is implied by Q1.

`scripts/qq_companion/configure.py` pairs one local SnowLuma account with one AstrBot OneBot adapter. The start script applies the pairing before launching services. It reuses the local OneBot HTTP token or generates one, writes matching reverse WS tokens, binds the AstrBot listener to loopback and installs the small Q1 guard. Configuration backups, tokens, deduplication state, profile and logs remain under ignored paths. The guard stores a unique `(account_id, message_id)` key in SQLite and drops messages from the bot itself. SnowLuma also disables self reports and retries reverse WS every five seconds. The deterministic `/q1ping` → `q1pong` command tests outbound text without a model.

`supervisor.py` owns AstrBot, SnowLuma, Qwen3 and optional TTS process trees. It checks fixed ports before starting and stops only children whose PID and creation time match its registry. AstrBot and SnowLuma get at most three restarts in five minutes with increasing delay; SnowLuma's reverse WS client reconnects when AstrBot returns. NTQQ is never in that registry. Ctrl+C and `stop_all.ps1` both stop registered services. Qwen3 runs as a model service only; no second QQ Agent starts.

Future Q2 work will connect the AstrBot OpenAI provider to `127.0.0.1:8080/v1`, expose media APIs and introduce the GPU broker. Q1 does not activate those features.
