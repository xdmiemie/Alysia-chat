# Q0–Q2 acceptance record

Date: 2026-10-28 (Asia/Hong_Kong). This record distinguishes code checks from tests requiring a logged-in NTQQ client.

| Gate | Result | Evidence / next action |
|---|---|---|
| Stage 10 baseline | Pass | `git merge-base HEAD aca495e8…` = `aca495e801f061210ce4961da8df2f63ab3f3c44`; branch `astrbot-chat` |
| Stage 5/6 automated regression | Pass | `10 passed` with workspace pytest temp directory |
| Existing runtime Doctor | Pass | GPU/CUDA, FFmpeg, llama-server, models, audio devices and ports report OK |
| Python environment separation | Pass | Stage 10 Python 3.10 in `runtime_env`; AstrBot Python 3.12.10 in `envs/astrbot` |
| AstrBot v4.29.3 source and dependency install | Pass | Initialized `data/cmd_config.json`; `pip check` clean; 154-line venv freeze recorded |
| SnowLuma v2.1.1 release | Pass | Local release manifest, bundled Node 24.22.1; archive SHA256 in dependency lock |
| OneBot pair and inbound guard | Code tested | Unit tests check matching token, loopback, idempotent config and persistent deduplication |
| OneBot live login / AstrBot reverse WS | Pass | SnowLuma detected a logged-in QQ client, `get_login_info` succeeded, WS client connected to AstrBot and Q1 guard loaded. Startup log warns private-message reporting unavailable, although controlled private ingress and reply succeeded; retain this warning for later diagnosis. |
| Authorized test targets | Ready | Current account reports one group and 12 friends. User authorized all groups and three specified friends for Q1 test messages; IDs remain outside this tracked report. |
| Outbound private/group text API | Pass | One labeled Q1 prompt sent to an authorized friend and one to the visible group; both OneBot actions returned `retcode=0` and the phone operator confirmed receipt. |
| Controlled private/group text round trip | Pass | Phone operator confirmed one `q1pong` per `/q1ping` in both private and group chat; AstrBot log showed matched inbound commands and outbound replies. |
| Outbound image and voice playback | Pass | One generated PNG and one one-second WAV probe sent to authorized private chat and group; OneBot returned `retcode=0` for all four and phone operator confirmed display/playback. |
| Qwen3/QQ stack one-click startup | Pass | `start_all.ps1` applied OneBot pairing automatically, then started Qwen3 (`/health=ok`), AstrBot 6185/6199 and SnowLuma 5099/3000; no second local Agent started |
| Managed shutdown | Pass | `stop_all.ps1` and a separate Ctrl+C run stopped the registered trees; all five ports closed, NTQQ left untouched |
| Private/group/@/image/reconnect counts | Partial | Controlled message session: 122 private inbound, 24 group inbound, 4 true @bot, 10 group images. A separate failure-injection run completed 3/3 AstrBot disconnect/recovery cycles with SnowLuma running throughout. Text round-trip totals remain below the 50/50 gate because many messages were plain text or unaddressed group commands. |
| Inbound image handling | Pass | Phone operator sent ten separate group image messages; the AstrBot guard recorded ten distinct group events containing image segments. |
| Inbound voice handling | Pending | No phone-originated voice message observed yet. |
| Bounded service recovery | Pass | Killed only the registered AstrBot child three times; supervisor restarted with 2/4/8-second backoff, and SnowLuma reconnected on every attempt (approximately 11/16/21 seconds). NTQQ and SnowLuma were not restarted. |
| Post-recovery text round trip | Pass | After the third recovery, AstrBot logged one `/q1ping` and one `q1pong`; phone operator confirmed receipt. |
| Original file/microphone model end-to-end | Pending | Automated Stage 5/6 tests passed; manual audio/model session remains unobserved |

Q0 has a recoverable baseline and isolated installation. Q1 cannot be declared accepted until the live counts and media checks above pass. The current QQ account has an authorized test group; the remaining Q1 message counts and phone-originated voice checks still need live evidence. Do not mark a simulated or unit test as a QQ conversation.

## Q2 acceptance record

| Gate | Result | Evidence / next action |
|---|---|---|
| One AstrBot Agent and no second QQ core | Code verified | Q1 guard remains ingress only; Q2 selects one OpenAI Compatible provider on the existing AstrBot Runner. No QQ path invokes `AstrbotChatCore.process`. |
| Live Qwen3 model ID, chat and two-step tool call | Pass | On the local RTX 5080, `probe_live_model.py` discovered the actual llama-server model ID and passed chat, structured `q2_echo` arguments and tool-result follow-up. The server requires string `tool_choice` and a health wait; Qwen3 launched with `--reasoning off`. The first sandboxed attempt had an inaccessible loopback listener; the later host probe passed. |
| AstrBot provider, role prompt and tool registration | Pass | Managed startup applied one `astrbot_chat_qwen3` provider, selected it on the single Runner, synced one `elysia` persona row, and AstrBot logged `Added llm tool: q2_echo`. QQ message inference remains a separate live gate. |
| Media API auth, limits and ASR/TTS contract | Pass | Auth, invalid-audio and contract unit checks passed; local authenticated `probe_media.py --count 10` returned ASR 10/10 and TTS 10/10. |
| GPU Broker minimum ownership | Unit tested | Cross-process file lock reserves managed QQ GPU workload against operator-only training and activation; Q7 drain/unload and VRAM checks remain future work. |
| Stage 5/6 and training smoke regression | Pass | Targeted Stage 5/6, Qwen3/GPT-SoVITS training and Q0–Q2 tests passed. Both operator-only training presets completed `--dry-run` with status `succeeded`; Qwen dataset files generated by the dry run were restored to their tracked content. |
| Model activation and rollback | Code tested | The offline manager only accepts registered model IDs, reserves the Broker, snapshots manifests, runs fixed model smoke and restores the snapshot on failure. Unit checks cover failed smoke rollback and operator rollback; no replacement model was activated during Q2. |
| TTS failure keeps text service | Pass at service level | The registered TTS wrapper was terminated once; supervisor restarted it in 2 seconds and Qwen3 `/health` plus the AstrBot listener remained available. |
| QQ text single inference | Pass | During the controlled private-chat check, SnowLuma and AstrBot each logged one inbound private message and one outgoing reply; the Qwen3 server logged one corresponding generation task. The sender confirmed receiving exactly one reply. The same run also observed a group message and reply through the same AstrBot provider. |
| Pure-text continuity after TTS loss | Pass at service level | The registered TTS wrapper was stopped in a fault-injection run. Qwen3 and AstrBot's text listener stayed available, and the supervisor restarted TTS in 2 seconds. QQ text remains a text-only route until the later Q4 voice integration. |

## QQ text pacing follow-up

The plugin now requests a structured thinking tier in the same LLM reply and subtracts elapsed model/code time from the combined thinking and typing target. Pure calculation tests and a direct AstrBot hook probe passed. The local Qwen3 returned valid structured tiers for synthetic simple and involved chat prompts. Managed restart loaded plugin 0.3.0. In a controlled group @ test, AstrBot recorded inbound at 17:32:38.650 and pre-send at 17:32:46.057 (7.407 seconds). The visible reply contained 11 CJK ideographs and two other characters, so typing time was 4.64 seconds; tier 3 adds 2.75 seconds for a 7.39-second target. One Qwen generation task and one OneBot send were observed. An earlier unaddressed group message received no reply. All timestamps are local on 2026-10-28 (Asia/Hong_Kong); the 17 ms difference is within log timestamp and scheduling precision.

## Sentence pacing and optional continuation

Plugin 0.4.0 loaded at 17:48:41 and OneBot reconnected at 17:48:44. The AstrBot hook probe sent a two-sentence first reply as two ordered QQ message chains, then a one-sentence follow-up through the same event, and verified that the follow-up entered the saved run history. Seven pacing/splitting parser tests and all 17 targeted Q0–Q2 tests passed. A direct local Qwen3 probe returned valid JSON with an appropriate optional question for the singing request and reciprocal sleep greeting, and `null` after a completed thanks exchange. The full repository suite was interrupted during the unrelated, long-running GPT-SoVITS tests; its first attempt also needed a workspace pytest temp directory due sandbox permissions.

The phone operator sent one private message at 17:53:20. AstrBot recorded one inbound text event and a three-sentence primary reply. SnowLuma sent those sentences in three separate QQ messages at 17:53:25, 17:53:27 and 17:53:34. The first send at 17:53:25.391 was 5.279 seconds after AstrBot inbound at 17:53:20.112, matching tier 3 (2.75 seconds) plus six CJK ideographs (2.4 seconds) and one other character (0.12 seconds) in the first sentence. A second Qwen3 task then decided to ask a natural question, which SnowLuma sent as one further QQ message at 17:53:39. AstrBot's latest persisted conversation had trailing roles `user, assistant, assistant` and contained the follow-up text. The extra-call retry setting initially produced a harmless AstrBot fallback warning; version 0.4.1 sets the supported minimum directly. The final managed restart loaded 0.4.1 and OneBot reconnected at 17:56:25; services remain running.

## Q3 acceptance record — local gate passed

Date: 2026-10-28 (Asia/Hong_Kong). Plugin version 0.5.0. Per the user's instruction,
tests use local events instead of connecting to QQ or asking for phone messages.
No QQ connection, outgoing QQ message, model startup, or live stack restart was
performed for Q3. The new version installs at the next normal managed startup.

| Gate | Result | Evidence |
|---|---|---|
| Canonical sessions / old history isolation | Pass locally | Numeric identity validation; separate bot, DM and group keys before Runner history lookup; legacy group context is replaced |
| Two interleaved participants / three groups | Pass locally | Unit checks cover ten alternating messages per group; real AstrBot-class probe interleaves ten private messages per participant plus three ten-message groups, with same display names and unique numeric IDs |
| Structured context / TTL / size | Pass | Sender, name, mention and reply ID retained; 20-entry / 300-second defaults; JSON UTF-8 byte budget tested; bounded session cache and exact-owner memory filter |
| Fragment merging / priority | Pass | Same-sender adjacent fragments merge; latest fragment owns the turn; wake phrase remains effective across fragments; @bot and quote-to-bot bypass delay; plain group traffic stops before Runner |
| Deduplication / self / system events | Pass | Persistent guard and real event hooks reject duplicate message IDs, self messages and notice events |
| Group controls / quiet periods / size | Pass | OneBot owner/admin only, per-bot/group persisted controls, midnight-crossing quiet periods, pending-reply invalidation, maximum group size and cached size lookup |
| Automatic participation | Pass | Default off, probability test, per-group 60-second minimum cooldown and three/hour rolling cap; reservations survive restart; unknown size prevents automatic participation |
| Permissions / outbound quota | Pass | Prohibited/unknown capabilities denied; Qzone requires gate, author and public candidate; private-send reservation is atomic, persistent, idempotent and shared across source groups |
| Tool enforcement in actual AstrBot convention | Pass locally | Copied tool set exposes only the audited echo adapter; actual `call_local_llm_tool` passes event plus schema arguments successfully; execution after group stop is rejected |
| Reply style / spam prevention | Pass locally | Original pacing JSON cleaned from response and history; group primary reply capped at three sends, private at six; combined final segment preserves text; group optional follow-up disabled |
| Q0–Q3 and pacing automated checks | Pass | 44 passed; Q2 Broker tests use disposable state instead of the active stack lock |
| Stage 5/6/10 and training regression | Pass | 33 passed across Stage 5, Stage 6, Stage 10 core, Qwen3 training and GPT-SoVITS training tests |
| Pinned AstrBot integration probe | Pass locally | `envs/astrbot/Scripts/python.exe scripts/qq_companion/probe_q3_local.py` prints `Q3_LOCAL_PASS`; uses real AstrBot classes with fake transport/inference and disposable state |

Stage Q3's implementation and local acceptance are complete. This record does
not upgrade Q1's outstanding live-count gate or claim new live QQ evidence.

## Editable prompt follow-up (0.5.1)

On 2026-10-28, the current QQ main-reply and private-continuation instructions
were moved to `configs/qq_companion/prompts.yaml`. Every section documents its
purpose, trigger and editing constraints. The plugin reads this file per request;
persona content is retained. This also replaces currently active AstrBot safety,
full-schema tool and date reminder text, plus approved echo tool descriptions.
No upstream source or live QQ configuration was modified.

The prompt, Q0–Q3 and pacing suite passed 50 tests. The pinned AstrBot local probe
also passed: temporary file edits reached the actual main/follow-up request and
tool schema, the persona survived, and intentionally invalid YAML stopped the
affected inference. The probe's configuration-error log is expected for that
fault-injection check. No QQ connection, outgoing QQ message or model call was
used. Normal managed startup installs the new loader with plugin 0.5.1; later
prompt-only edits need no restart.
