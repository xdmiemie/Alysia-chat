# QQ Q0–Q3 architecture

```
NTQQ (user managed)
  ↕ local attachment
SnowLuma v2.1.1 (external/SnowLuma, Node 24.22.1)
  → OneBot v11 reverse WS, ws://127.0.0.1:6199/ws, shared token
AstrBot v4.29.3 (external/AstrBot, isolated Python 3.12)
  → astrbot_chat_q1 guard → built-in AstrBot event pipeline
  → optional local llama-server on 127.0.0.1:8080
```

AstrBot owns QQ messages, the Agent and conversation history. The old `AstrbotChatCore.process()` remains for the Stage 10 local entry point and is never called in the QQ pipeline.

`scripts/qq_companion/configure.py` pairs one local SnowLuma account with one AstrBot OneBot adapter. The start script applies the pairing before launching services. It reuses the local OneBot HTTP token or generates one, writes matching reverse WS tokens, binds the AstrBot listener to loopback and installs the small Q1 guard. Configuration backups, tokens, deduplication state, profile and logs remain under ignored paths. The guard stores a unique `(account_id, message_id)` key in SQLite and drops messages from the bot itself. SnowLuma also disables self reports and retries reverse WS every five seconds. The deterministic `/q1ping` → `q1pong` command tests outbound text without a model.

`supervisor.py` owns AstrBot, SnowLuma, Qwen3 and optional TTS process trees. It checks fixed ports before starting and stops only children whose PID and creation time match its registry. AstrBot and SnowLuma get at most three restarts in five minutes with increasing delay; SnowLuma's reverse WS client reconnects when AstrBot returns. NTQQ is never in that registry. Ctrl+C and `stop_all.ps1` both stop registered services. Qwen3 runs as a model service only; no second QQ Agent starts.

Q2 connects the single AstrBot Runner to `127.0.0.1:8080/v1`, exposes the authenticated Media API, and reserves managed GPU work through the Broker. Q7's exclusive visual switching remains a separate future gate.

Q3 changes the event's session ID before the Runner's session lock and conversation lookup: `qq:{bot_uin}:dm:{sender_uin}` or `qq:{bot_uin}:group:{group_id}`. AstrBot retains its platform/message-type prefix in the UMO; the canonical QQ ID is its session component. Existing conversations under legacy keys are not imported. Direct replies use the original numeric event sender/group via `AiocqhttpMessageEvent.send`, independent of the history key. Proactive sends must use an audited numeric-recipient adapter, never pass a canonical session key directly to OneBot.

`message_router.py` admits @bot, quotes of bot messages, wake words, then optional automatic participation. It stores only per-group settings and automatic-participation timestamps. `memory_context.py` holds an ephemeral, scoped, TTL-limited window, not a second durable chat database. Each row includes speaker ID, display name, message IDs, @ flag, reply ID and receipt time. Group requests replace the Runner's historical contexts with this window; private requests retain AstrBot's newly scoped history. The future memory retrieval helper first filters exact `owner_key` and deletion state.

`permissions.py` rejects unknown and prohibited capabilities. The request hook runs last, copies the tool set and exposes only audited adapters. In Q3 this is `q2_echo`; its execution wrapper rechecks current group controls. AstrBot's tool-start hook cannot veto execution, so security does not depend on it. The persistent private-send reservation enforces a bot-wide, per-recipient daily quota and request idempotency across source groups. Q3 installs no proactive-send or Qzone tool. Qzone's gate remains closed.

Q3 local validation instantiates the pinned AstrBot event, message, request and tool execution classes with local transport and inference fakes. It opens no QQ connection, and the test event skips unrelated base-event telemetry. The temporary timer-only loop has no socket transports. Existing live process trees and model ownership are untouched.
