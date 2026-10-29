# Q0–Q4 operations (Windows 11)

Q4 voice, music, whole-song cover, GPU switching and local acceptance are documented in [q4.md](q4.md). Normal startup now includes TTS; `-WithoutTTS` explicitly disables it. All Q4 development tests use local transport instead of QQ.

## Install and configure

1. Keep the existing `runtime_env` Python 3.10 environment for Stage 0–10. Install official Python 3.12.10 into `external/Python312`, then run `scripts/qq_companion/install_astrbot.ps1`. The script verifies AstrBot v4.29.3 at `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`, creates `envs/astrbot`, installs its dependencies and initializes `external/AstrBot/data`.
2. Place the official SnowLuma v2.1.1 Windows x64 release at `external/SnowLuma/v2.1.1`. Keep its local EULA/privacy choice under its ignored config. Log in to NTQQ yourself. SnowLuma is a nonofficial QQ integration with account restriction risk; use is subject to its [noncommercial license](https://github.com/SnowLuma/SnowLuma) and bundled EULA.
3. Stop both services, then run `runtime_env/python.exe scripts/qq_companion/configure.py --apply` once to inspect the selected account and pairing. The start script also applies this pairing automatically before launching services, and backs up local JSON only when it changes. If multiple SnowLuma accounts are present, select the desired `onebot_*.json` file with `--snowluma-config`; set `QQ_COMPANION_SNOWLUMA_CONFIG` to that file's path before using the start script. You may set `SNOWLUMA_ONEBOT_TOKEN` in the current process to override the existing local token. Do not paste the token into chat or Git.
4. Run `scripts/qq_companion/doctor.ps1`. It reports configuration and environment readiness without printing account IDs or tokens. Open AstrBot WebUI at `http://127.0.0.1:6185` after startup and change the initial administrator password. SnowLuma WebUI uses `http://127.0.0.1:5099`.

## Start and stop

Qwen now starts with thinking enabled and a 1024-token reasoning budget. The
provider also requests `enable_thinking=true`; reasoning is separated from
visible replies. Existing running processes need a normal stop/start to load
the new launch arguments, plugin and Media API. Configuration writes are backed
up; development probes use request-level thinking and never send QQ messages.

`scripts/qq_companion/start_all.ps1 -NoModels` pairs the local OneBot token, installs the guard, then starts the Q1 OneBot protocol and `/q1ping` test. Normal startup reserves the GPU with the Broker, starts Qwen3, SenseVoice, GPT-SoVITS and its wrapper, probes Qwen3 chat/tool calling, discovers its model ID, syncs the role prompt, and binds AstrBot's sole Runner to that provider. `-WithoutTTS` explicitly disables TTS; `-WithTTS` remains compatible. The authenticated facade at `127.0.0.1:8765` proxies ASR at 5001 and TTS at 5002 without loading another model. Q4 routes voice messages through this facade; TTS failure falls back to text. The script does not run the old local Agent or force NTQQ login. SnowLuma attaches to the already logged-in client.

The ignored `storage/qq_companion/media_token` is generated once. Use its value only in a local `Authorization: Bearer …` header; keep it out of chat, logs and tracked configuration. `GET /health`, `GET /v1/characters`, `POST /v1/audio/transcriptions`, `POST /v1/audio/speech`, and `GET /v1/jobs/{id}` require that token. Transcription accepts at most 12 MiB and 60 seconds of WAV or Ogg Opus; speech accepts the active `character_id`, a configured emotion, and WAV output. Model activation and training are operator-only CLI actions; the QQ Agent cannot invoke them through this API.

QQ text replies use the guard plugin's pacing hooks. The first LLM response contains `reply` and an integer `thinking_level` from 1 to 5; the JSON wrapper is removed before AstrBot saves history or sends text. Thinking times are 0.5, 1.5, 2.75, 4.25 and 6 seconds. Typing time adds 0.4 seconds per CJK ideograph and 0.12 seconds per other Unicode character. The plugin splits the visible reply at sentence endings and newlines, then sends one sentence per QQ message. For the first sentence, it waits until `thinking + typing(first sentence)` from inbound receipt. Later sentences use the same inbound timer and cumulative typing time, so model and code time are deducted before each send; an elapsed target sends immediately.

After the last sentence in private chat, one extra call to the same local Qwen3 provider decides whether a natural follow-up is useful. It can return one sentence or `null`; the follow-up has its own thinking tier and typing delay measured from the start of that extra call. There is no chain of further follow-ups. A newer inbound message cancels a pending follow-up; a sent follow-up enters AstrBot's history. Follow-ups have a 60-second cooldown. Q3 disables optional follow-ups in groups and caps primary replies at three outgoing group messages or six private messages, combining remaining sentences into the final message. Invalid JSON or a failed extra call produces no follow-up. This path applies only to non-streaming QQ LLM text replies, not `/q1ping` or other deterministic messages. Prompt text lives in `configs/qq_companion/prompts.yaml`; splitting and arithmetic remain in `integrations/qq_astrbot/plugin/reply_pacing.py`. Startup installs all plugin modules listed in `configure.py`.

## Editable QQ prompts

Plugin 0.6.0 reads the single UTF-8 file
[`configs/qq_companion/prompts.yaml`](../../configs/qq_companion/prompts.yaml)
on each main-reply or private-follow-up request. Each section has Chinese
comments explaining purpose, trigger and editing constraints. YAML comments are
not sent to the model. Persona text remains in the character's `system_prompt.txt`.

| Key | Model-facing content |
|---|---|
| `qq_context` | Structured QQ input, numeric speaker identity, privacy and reply style |
| `reply_output` | Primary reply JSON and thinking tier |
| `followup_system` | Optional private continuation decision |
| `followup_user` | Final task message for the continuation decision |
| `astrbot_safety` | Replacement for the safety block when AstrBot enables it |
| `astrbot_tool_call` | Replacement for the current full-schema tool-call instructions |
| `datetime_reminder` | Current date/time reminder; `$current_time` and `$weekday` are runtime values |
| `echo_description` | Description in the approved `q2_echo` tool schema |
| `echo_value_description` | Description of that tool's `value` parameter |

Keep YAML indentation and key names. Edit the text under `|`; preserve the JSON
fields described in the comments because the reply parser uses them. Main replies
and private follow-ups load a fresh bundle, so saved edits take effect on the next
request after the new plugin has first been loaded. An already in-flight request
keeps the text it read. A broken or incomplete file stops the affected main
inference request or skips the optional follow-up and logs the file/key problem;
there is no hidden fallback copy of the prompt text.

The loader finds the same project file both from the source tree and from the
installed plugin under `external/AstrBot/data/plugins`. Installation copies the
loader, not another editable prompt bundle. A separate deployment can explicitly
set `ASTRBOT_CHAT_PROMPTS_FILE` to its absolute bundle path. Framework safety/tool
blocks are replaced only if they were present; this preserves their existing
enable settings. Dates are retained as dynamic data. Command confirmations,
actual user messages and tool result data are not fixed AI instructions.

After a stopped-stack change to the active Qwen3 manifest or role prompt, the next start automatically re-probes the live model and updates AstrBot's provider and persona. To inspect without writing, run `runtime_env/python.exe scripts/qq_companion/configure_model.py` while llama-server is up and AstrBot is stopped. A structured `q2_echo` tool call and tool-result follow-up are required; a failed probe aborts startup before QQ connects. The Q2 GPU lock stays held for the managed model lifetime. To run a preset training job, stop the QQ stack first, then run `runtime_env/python.exe scripts/qq_companion/training_jobs.py qwen3_elysia --dry-run` or the `gpt_sovits_elysia` preset. Omit `--dry-run` only for an operator-approved real run. The original training scripts retain their checkpoint behavior; Q2 never forcibly interrupts an active training process.

For a registered model change, keep the QQ stack stopped and run `runtime_env/python.exe -m scripts.qq_companion.model_activation activate qwen3 elysia <registered-model-id>` (or `gpt_sovits` for speech). The manager snapshots the active and registry manifests in ignored `storage/qq_companion/model_backups`, activates the registered model, runs a fixed Qwen chat/tool or GPT-SoVITS WAV smoke check, and restores the old manifests on failure. On success it prints a job ID; `runtime_env/python.exe -m scripts.qq_companion.model_activation rollback <job-id>` restores that snapshot only while the activated model is still current. Restart the QQ stack afterward to resync AstrBot's model ID and persona.

Keep the starting terminal open. Ctrl+C requests shutdown; repeated interrupts are ignored during cleanup. Each managed service starts suspended, enters a Windows Job Object with `KILL_ON_JOB_CLOSE`, then resumes. Descendants remain owned even if a PowerShell launcher exits; Windows also closes these service trees if the supervisor is forcibly terminated. This uses the [Windows job ownership mechanism](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).

From a second terminal, `scripts/qq_companion/stop_all.ps1` stops registered child trees. A disappearing or inaccessible child does not skip the remaining services. Any service that still cannot exit remains in the registry for a later stop attempt, and registry updates use atomic replacement. Both stop paths leave the independently running NTQQ client alone. Check ignored logs in `storage/qq_companion/logs` if a process exits or a port is occupied. A stale registry does not authorize stopping a reused PID.

## Q1 live acceptance

First run `scripts/qq_companion/test_onebot.ps1`. A success only proves SnowLuma's local OneBot endpoint and login, not AstrBot connectivity. Confirm the AstrBot log reports `aiocqhttp(OneBot v11) ... connected`. Send `/q1ping` in a test private chat and test group; expect one `q1pong` each. The guard drops repeat `(account_id, message_id)` events and bot self messages.

Record private chat 50 turns, group 50 turns, @ 20 turns, image ingress 10 messages, and three connection interruptions/recoveries in [acceptance.md](acceptance.md). Include reply/quote, outbound text and image, and a voice upload/playback check. Record counts and timestamps only in the tracked report; keep message IDs, account IDs and content in ignored local notes if needed. Confirm no self reply loop, cross-user context leak or duplicate reply. If an item fails, keep Q1 marked incomplete and diagnose the interface. Do not replace SnowLuma with another protocol implementation.

`runtime_env/python.exe scripts/qq_companion/live_counts.py` prints aggregate inbound message, @bot, image, voice and quote counts without account IDs. `live_media_probe.py --user-id <authorized-friend-id> --target private|group|both --kind image|voice|both` sends small generated media probes only after that friend is verified in the active QQ friend list. `reconnect_probe.py` is a disruptive test: it terminates only the registered AstrBot child three times, waits for the supervisor's bounded restart and SnowLuma's reverse WS reconnect, and keeps NTQQ running. Run it only when chat traffic is idle, then confirm a fresh `/q1ping` from another QQ client.

## Q3 group routing, controls and permissions

The next normal managed startup installs plugin 0.5.1, including its schema and
three new routing/permission/context modules. Q3 development and local acceptance
do not restart or connect the QQ stack. For manual deployment while the stack is
stopped, use the existing `configure.py --apply`; it copies the same complete
file list. The AstrBot plugin settings UI exposes the following defaults:

| Setting | Default / bound |
|---|---|
| Group context | Newest 20 entries within 300 seconds; configurable up to 100 / 3600 |
| Context token budget | 2048, conservatively counted by JSON UTF-8 bytes; 512–16384 |
| Fragment merge | 1.5 seconds for adjacent messages from the same sender; 0–5 |
| Wake words | `爱莉希雅`, `elysia`; ASCII words use word boundaries |
| Automatic participation | Probability 0 (off); when enabled at most 3 turns/hour/group, cooldown at least 60 seconds |
| Maximum group size | 2000 members; known larger groups do not receive chat replies |
| Proactive DM allowlist / quota | Empty list / 1 per recipient per local calendar day; no Q3 send tool is installed |

Direct @bot and quotes of bot messages bypass the fragment delay. Other admitted
messages wait for the merge interval; superseded fragments do not start another
Runner turn. Ordinary group traffic is cached without calling the model. Group
size is read through `get_group_info`, cached for five minutes, and never requires
fetching the whole member list. If size lookup fails, automatic participation is
disabled; explicit triggers remain available. Explicit triggers bypass only the
automatic-participation cooldown, and still respect group disable/quiet policy.

Only the current group's OneBot `owner`/`admin` may change its settings:

```text
/q3 status
/q3 off
/q3 on
/q3 quiet 23:00 07:00
/q3 quiet off
```

Quiet periods use the machine's local timezone, include the start and exclude the
end, and may cross midnight. Equal start/end means all day. Control commands
remain available during quiet/off periods. Changes invalidate pending group
replies and tools, even if the group is immediately re-enabled. Settings survive
restart in `routing.sqlite3` under the existing ignored plugin state directory
(`ASTRBOT_CHAT_STATE_DIR`, otherwise AstrBot's `data/astrbot_chat_q1`).

Conversation keys include both bot UIN and numeric DM sender/group ID. Legacy
conversation keys are not migrated into the new scopes. Group prompts rebuild
their recent context from the current group's TTL window, with sender IDs,
display names, @ flags and quote IDs. A restart clears that ephemeral window.
The `context_token_budget` applies to the recent window; the current message and
persona remain separate. Future persistent memories must use the same exact
owner key; Q5's repository is not implemented by Q3.

The Agent receives only audited tool adapters. Q3 keeps `q2_echo` as a public
read-only test tool; unknown tools, arbitrary shell/Python, profile changes,
deletion, arbitrary uploads, activation and training are unavailable from QQ.
Tools are copied per request, filtered after other request hooks, and checked
again at execution. An administrator's chat text does not grant these actions.
The private-send permission layer atomically reserves a bot-wide per-recipient
quota and request ID in `permissions.sqlite3`; failed sends keep their quota.
It does not itself send a message. Qzone remains disabled until its Q6 gate.

Local verification, requiring no QQ messages or running models:

```powershell
runtime_env/python.exe -m pytest tests/test_qq_q3.py tests/test_qq_q0_q1.py tests/test_qq_q2.py tests/test_qq_pacing.py -q --basetemp storage/temporary/pytest_q3
envs/astrbot/Scripts/python.exe scripts/qq_companion/probe_q3_local.py
```

The probe uses the pinned AstrBot classes with local fake transport/inference,
disposable state and a timer-only scheduler. It checks interleaved private chats,
three groups, actual adapter dispatch, deduplication, controls, sentence pacing,
schema loading and actual AstrBot tool calling convention. `Q3_LOCAL_PASS` is a
local integration result, not evidence of a live QQ exchange. Q2 unit tests now
isolate their Broker state, so they cannot contend with a running model stack.

## Rollback

Run `stop_all.ps1`, restore the chosen ignored JSON backups from `storage/qq_companion/config_backups`, then restart only the Stage 10 local entry point with `quickstart/run.ps1`. The Stage 10 baseline is `aca495e801f061210ce4961da8df2f63ab3f3c44`; branch `astrbot-chat` includes it. AstrBot/SnowLuma live under ignored `external/`, and the original runtime and training scripts were not replaced.
