# QQ companion operations (Windows 11)

The authenticated [大肥鱼控制台](webui.md) adds Douyin monitoring, memory and context CRUD,
model settings with supervised restart, shared persona editing, complete request usage,
application schedules and Chinese prompt controls to AstrBot's native WebUI.
Existing AstrBot personas are now preserved across startup; the character text file
initializes a missing persona only. QQ, diaries and Douyin use the AstrBot persona store.

Q6 publishing, local controls and verification boundaries are documented in
[q6.md](q6.md). Daily publishing defaults to23:40, two short sentences, once per
natural day; important calendar/admin events have no daily count quota and are
deduplicated. Edit `configs/qq_companion/qzone.local.yaml` for time/audience/events
and `configs/qq_companion/prompts.yaml` keys `qzone_daily`, `qzone_event`,
`qzone_interaction` for wording. Local policy reloads every minute. Friend
interactions remain disabled until real visible verification; no such tests
were run in Q6. `qzone.py stop` immediately prevents new reservations.

Q4 voice, music, whole-song cover, GPU switching and local acceptance are documented in [q4.md](q4.md). Normal startup now includes TTS; `-WithoutTTS` explicitly disables it. All Q4 development tests use local transport instead of QQ.

Cover switching unloads and restores only Qwen. SenseVoice, GPT-SoVITS and the
TTS wrapper keep their existing processes and loaded models. New GPU requests
still wait until cover synthesis and Qwen restoration finish. Restart the
supervisor once after updating its switching code; restarting AstrBot alone
does not reload the supervisor.

Plugin 0.9.8's current Agent, native tools, interruption guards and acceptance
are documented in [agent.md](agent.md). The old optional private follow-up prompts
are restored after primary delivery, with at most one tool-free decision.

## Install and configure

1. Keep the existing `runtime_env` Python 3.10 environment for Stage 0–10. Install official Python 3.12.10 into `external/Python312`, then run `scripts/qq_companion/install_astrbot.ps1`. The script verifies AstrBot v4.29.3 at `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`, creates `envs/astrbot`, installs its dependencies and initializes `external/AstrBot/data`.
2. Place the official SnowLuma v2.1.1 Windows x64 release at `external/SnowLuma/v2.1.1`. Keep its local EULA/privacy choice under its ignored config. Log in to NTQQ yourself. SnowLuma is a nonofficial QQ integration with account restriction risk; use is subject to its [noncommercial license](https://github.com/SnowLuma/SnowLuma) and bundled EULA.
3. Stop both services, then run `runtime_env/python.exe scripts/qq_companion/configure.py --apply` once to inspect the selected account and pairing. The start script also applies this pairing automatically before launching services, and backs up local JSON only when it changes. If multiple SnowLuma accounts are present, select the desired `onebot_*.json` file with `--snowluma-config`; set `QQ_COMPANION_SNOWLUMA_CONFIG` to that file's path before using the start script. You may set `SNOWLUMA_ONEBOT_TOKEN` in the current process to override the existing local token. Do not paste the token into chat or Git.
4. Run `scripts/qq_companion/doctor.ps1`. It reports configuration and environment readiness without printing account IDs or tokens. Open AstrBot WebUI at `http://127.0.0.1:6185` after startup and change the initial administrator password. SnowLuma WebUI uses `http://127.0.0.1:5099`.

## Start and stop

Qwen starts with thinking enabled and a 1024-token reasoning budget. Per-request
phase switches in `configs/runtime/qwen3.yaml` keep main planning and song selection
enabled; post-Agent follow-up, result notifications and summary disable
thinking with a zero reasoning budget. Sampling settings reach both llama.cpp and
the AstrBot request adapter. Legacy Elysia examples are explicitly disabled for
the current QQ persona. Existing running processes need a normal stop/start to load
the new launch arguments, plugin and Media API. Configuration writes are backed
up; development probes use request-level thinking and never send QQ messages.

`scripts/qq_companion/start_all.ps1 -NoModels` pairs the local OneBot token, installs the guard, then starts the Q1 OneBot protocol and `/q1ping` test. Normal startup reserves the GPU with the Broker, starts Qwen3, SenseVoice, GPT-SoVITS and its wrapper, probes Qwen3 chat/tool calling, discovers its model ID, syncs the role prompt, and binds AstrBot's sole Runner to that provider. `-WithoutTTS` explicitly disables TTS; `-WithTTS` remains compatible. The authenticated facade at `127.0.0.1:8765` proxies ASR at 5001 and TTS at 5002 without loading another model. Q4 routes voice messages through this facade; TTS failure falls back to text. The script does not run the old local Agent or force NTQQ login. SnowLuma attaches to the already logged-in client.

Managed configuration enables SnowLuma's `hookAutoLoad`, so a newly started QQ
client is attached without a separate WebUI action. Startup waits up to 90 seconds
for authenticated OneBot `get_login_info` and `get_status` to confirm the selected
account is online. WebUI port 5099 alone is insufficient. If attachment fails,
startup reports the connection failure and cleans up its services; inspect
`storage/qq_companion/logs/snowluma.log`. NTQQ stays open.

The ignored `storage/qq_companion/media_token` is generated once. Use its value only in a local `Authorization: Bearer …` header; keep it out of chat, logs and tracked configuration. `GET /health`, `GET /v1/characters`, `POST /v1/audio/transcriptions`, `POST /v1/audio/speech`, and `GET /v1/jobs/{id}` require that token. Transcription accepts at most 12 MiB and 60 seconds of WAV or Ogg Opus; speech accepts the active `character_id`, a configured emotion, and WAV output. Model activation and training are operator-only CLI actions; the QQ Agent cannot invoke them through this API.

QQ replies use one native AstrBot tool loop. Native tool calls execute and continue;
a response without tool calls ends the Agent directly. Main responses carry
`reply`, integer `thinking_level` (1-5), and boolean `voice` delivery metadata.
Internally the Agent keeps persona/framework, reply rules and dynamic execution
state separate. Before inference and token counting, the same adapter merges all
system text into one leading system message, preserving dialogue/tool ordering.
This satisfies Qwen3.5's template and remains compatible with Qwen3. Temporary
rules participate in tool rounds and follow-ups but are removed before saving
dialogue to avoid accumulation.
Content accompanying a native tool call is ignored regardless of its shape; its
reply/voice fields never trigger parsing, regeneration or delivery. Tool-enabled
requests discard response_format from both per-request and provider settings.
Ordinary text is accepted locally. Unusable output gets one regeneration with
the same context and native tools; another invalid result uses the local fallback.
The durable reply_retries counter is capped at one across tool rounds and provider
fallback. There is no semantic judge, draft approval, or model-generated terminal close.
Default limits are 16 model requests, 16 tool calls and 180 seconds per run.

After all primary private-message segments are sent, the original optional
follow-up prompts independently decide whether to add one sentence. Each input
allows at most one decision; null, duplicate/invalid output, unexpected tool
calls, errors, or new user input stop the follow-up. The decision disables tools and
thinking. A confirmed addition enters chat history and ends the follow-up.
Follow-ups inherit all main-Agent system messages and a freshly rebuilt execution
state. The followup_phase prompt scopes the original follow-up format and tool
ban without dropping persona, QQ privacy or execution-truth rules.
Groups do not start private follow-ups. Model requests share the run budget,
and the durable followups counter prevents resetting the one-call cap.
History preserves original assistant content JSON, native tool_calls and matching
tool_call_id results. A fully delivered primary reply is saved once with its
complete original content; partial delivery retains metadata and only delivered
reply text. Follow-ups and background song notifications retain their own JSON.
Post-Agent requests keep the latest eight complete user turns and tool pairs,
without truncating individual JSON bodies. Group protocol history remains scoped
to the group's bounded TTL window. Historical calls are data, never re-executed.
Actual delivery receipts remain in audit; temporary state/instructions are omitted.
The first text segment waits for its thinking tier (0.5, 1.5, 2.75, 4.25 or 6 seconds)
plus its own typing time (0.4 seconds per CJK character, 0.12 per other character), less
elapsed time since input. Each later text segment waits for its own typing time
after the previous send completes, with no repeated thinking delay or elapsed-time credit.
Merged final segments use their actual combined length. Ready-message pacing continues
after the generation budget expires and remains interruptible by new input;
prepared voice is sent without a simulated typing delay. Text sentence splitting
caps delivery at three group or six private messages. voice=true sends one recording
for the complete spoken reply; bounded internal synthesis chunks are joined as WAV
audio, with decorative faces/emoji sent as text. Synthesis failure falls back to the
complete reply's text sentence splitting. Prompt text lives in `configs/qq_companion/prompts.yaml`.
Startup installs every plugin module listed in `configure.py`.

## Editable QQ prompts

### Local model selection

Both local GGUFs are retained in the character registry:

| Registered ID | Weights | Prompt profile |
|---|---|---|
| `qwen3-14b-q4-k-m-base-v1` | Qwen3-14B Q4_K_M | `qwen3` |
| `qwen3.5-9b-q8-0-base-v1` | Qwen3.5-9B Q8_0 | `qwen35` |

Stop the managed stack before switching. The verified activation command holds
the GPU broker, probes chat/native tool calling, and rolls back on failure:

```powershell
runtime_env/python.exe -m scripts.qq_companion.model_activation activate qwen3 elysia qwen3.5-9b-q8-0-base-v1
```

Replace the final ID with `qwen3-14b-q4-k-m-base-v1` to switch back. Then start
`scripts/qq_companion/start_all.ps1` normally. Startup selects the active weights
and their `runtime.prompt_profile`, and regenerates the local AstrBot provider.
Only one model is loaded at a time. `scripts/qwen3_models.py list --character
elysia --json` lists both choices; the existing interactive llama launcher also
shows them. Activation does not hot-swap a running model.

Both models use the same 32768-token context, Q8_0 KV cache, GPU layer setting,
phase switches and Agent code. Automatic fit is disabled and flash attention
enabled, so llama.cpp cannot silently reduce the configured context or GPU
layers. The GGUF's own chat template serializes native tools. Shared business
prompts follow Qwen3.5's requirements; `model_profiles` in `prompts.yaml` changes
only `model_protocol`, selected by registry metadata rather than model-name
branches in the request code.
Current-run tool reasoning is replayed with its matching calls and counted in
the context budget, following [Qwen's function-calling requirements](https://docs.qwencloud.com/developer-guides/tool-calling/function-calling#thinking-mode).
It remains transient: saved history, optional follow-ups and delivered messages
exclude it. The adapter never promotes reasoning text into visible content.

The Qwen3.5 weight source is [Unsloth's GGUF repository](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF),
file `Qwen3.5-9B-Q8_0.gguf`, 9,527,502,048 bytes, SHA256
`809626574d0cb43d4becfa56169980da2bb448f2299270f7be443cb89d0a6ae4`.
For another checkout, download and verify that file, then register it with
`scripts/qwen3_models.py register --character elysia --model qwen3.5-9b-q8-0-base-v1
--file <GGUF-path> --name "Qwen3.5-9B Q8_0 base" --prompt-profile qwen35`.

Reproduce the isolated real-GPU acceptance with:

```powershell
envs/astrbot/Scripts/python.exe -m scripts.qq_companion.probe_model_switch
```

It runs 14B → 9B → 14B under the GPU broker, warms real ASR/TTS, checks native
chat/tools/follow-up, memory/diaries and near-32K input, samples WDDM per-process
dedicated/shared memory plus nvidia-smi, and restores the original manifests.
Reports and raw samples go under `storage/temporary/model-switch-*`. QQ is never
started; Agent song actions are mocked. Normal CPU tokenizer, file mapping and
host staging allocations are distinct from shared GPU memory counters.

#### 2026-11-01 measured acceptance

On this RTX 5080 (16,303 MiB reported VRAM), the final 14B → 9B → 14B
run used llama.cpp build 10099 / `1a064ab09`, the shared 32K configuration,
warmed GPT-SoVITS, and the existing SenseVoice service (configured for CPU).
No QQ connection was opened; song actions were mocked, speech/ASR were real.

| Measurement | Qwen3-14B Q4_K_M | Qwen3.5-9B Q8_0 |
|---|---:|---:|
| Layers offloaded to GPU | 41/41 | 33/33 |
| Model process dedicated GPU peak (MiB) | 11,363.719 | 9,289.695 |
| Whole GPU used peak (MiB) | 14,164 | 12,143 |
| Minimum free VRAM (MiB) | 1,814 | 3,835 |
| Model process shared GPU peak (MiB) | 134 | 130 |
| Long-context input tokens | 30,712 | 30,699 |
| Long-context request seconds | 13.922 | 6.281 |

**GPU acceptance passed.** Both models loaded all transformer layers and KV
cache on GPU; 9B's recurrent state was also on CUDA0. No OOM occurred. The user's
criterion allows the pre-existing nonzero shared GPU buffers; these are measured
for visibility, not treated as a failure. The logs also show CPU-mapped embedding
weights and CUDA host buffers, as in the existing llama.cpp offload scheme; this
is not evidence of layers falling back to CPU due to VRAM exhaustion. Measurements
apply to the tested load, including the coexisting speech services.

308 related automated tests passed. Both models passed chat, one optional
follow-up, search-to-cover tool roundtrip, cancellation, memory extraction,
correction, recall, compaction, diaries, long-context inference and coexisting
speech. Switching back to 14B passed the chat/tool smoke tests. The final run
was **not an all-green behavioral acceptance**: both models used cancel/start
instead of replace for changing a song; 14B additionally changed a task in
response to unrelated VIP-account chatter. All failures remain in the report.

During compatibility work, 9B sometimes put a final JSON in reasoning_content
with empty content after tools. The shared transient tool-reasoning replay and
explicit protocol-boundary prompts fixed that in the final run: 9B's tested
Agent/control cases required no reply regeneration. This is sampled acceptance,
not a claim that every future model response will be correct.

Raw results: `storage/temporary/model-switch-final-20261101/report.json`,
per-process samples: `gpu_samples.jsonl` in the same directory, and regression
results: `storage/temporary/model-switch-unit-final.xml`. Earlier failed probe
directories are retained for comparison. The original active 14B was restored;
9B remains registered and its verified GGUF is kept alongside 14B.
The operator activation entry point also passed its real 9B chat/tool smoke
check; rollback then restored the original 14B selection.

After acceptance, the operator selected Qwen3.5-9B Q8_0 as the default on
2026-11-01. The active manifest, registry and AstrBot provider now select 9B
with the `qwen35` prompt profile. Its activation chat/tool smoke check passed.
The context audit found no test markers in production databases; all ten
database files remained unchanged during activation. Test model processes
exited, releasing their in-memory context. Isolated test artifacts under
`storage/temporary` are not loaded as production conversation or memory.

### Prompt editing

Plugin 0.9.8 reads the single UTF-8 file
[`configs/qq_companion/prompts.yaml`](../../configs/qq_companion/prompts.yaml)
on each main-reply or post-Agent follow-up request. Each section has Chinese
comments explaining purpose, trigger and editing constraints. YAML comments are
not sent to the model. Persona text remains in the character's `system_prompt.txt`.

| Key | Model-facing content |
|---|---|
| `qq_context` | Structured QQ input, numeric speaker identity, privacy and reply style |
| `reply_output` | Final reply and delivery metadata |
| `agent_policy` | Native tool use, receipts and direct completion |
| `agent_reply_regenerate` | One regeneration for an unusable final response |
| `followup_system`, `followup_user` | Original optional private post-reply decision |
| `followup_phase` | Follow-up format and tool ban after inheriting the complete main system |
| `agent_budget_reply` | Local terminal fallback; no extra inference |
| `tool_*` | Native tool descriptions and argument descriptions |
| `music_result`, `music_clarification` | Background song result notification |
| `astrbot_safety` | Replacement for the safety block when AstrBot enables it |
| `astrbot_tool_call` | Replacement for the current full-schema tool-call instructions |
| `datetime_reminder` | Current date/time reminder; `$current_time` and `$weekday` are runtime values |
| `echo_description` | Description in the approved `q2_echo` tool schema |
| `echo_value_description` | Description of that tool's `value` parameter |

Keep YAML indentation and key names. Edit the text under `|`; preserve the JSON
fields described in the comments because the reply parser uses them. Main replies
and post-Agent decisions load a fresh bundle, so saved edits take effect on the next
request after the new plugin has first been loaded. An already in-flight request
keeps the text it read. A broken or incomplete file stops the affected main
inference request and logs the file/key problem;
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

Complete model input/output bodies are in `storage/qq_companion/logs/model_debug/`.
Each indented JSON file contains an array of request/response exchanges, with
line breaks for reading. Each file holds up to 50 exchanges and only the newest
five files are retained; displayed line counts do not affect rotation. Existing
JSONL archives are migrated without changing their records. `request` contains the
actual model input; `response` retains the API's complete JSON, including `choices`.
See [the field definitions](agent.md#完整模型调试日志). Reload AstrBot after installing
updated plugin code to enable logging; earlier raw responses cannot be recovered.

## Q1 live acceptance

First run `scripts/qq_companion/test_onebot.ps1`. A success only proves SnowLuma's local OneBot endpoint and login, not AstrBot connectivity. Confirm the AstrBot log reports `aiocqhttp(OneBot v11) ... connected`. Send `/q1ping` in a test private chat and test group; expect one `q1pong` each. The guard drops repeat `(account_id, message_id)` events and bot self messages.

Record private chat 50 turns, group 50 turns, @ 20 turns, image ingress 10 messages, and three connection interruptions/recoveries in [acceptance.md](acceptance.md). Include reply/quote, outbound text and image, and a voice upload/playback check. Record counts and timestamps only in the tracked report; keep message IDs, account IDs and content in ignored local notes if needed. Confirm no self reply loop, cross-user context leak or duplicate reply. If an item fails, keep Q1 marked incomplete and diagnose the interface. Do not replace SnowLuma with another protocol implementation.

`runtime_env/python.exe scripts/qq_companion/live_counts.py` prints aggregate inbound message, @bot, image, voice and quote counts without account IDs. `live_media_probe.py --user-id <authorized-friend-id> --target private|group|both --kind image|voice|both` sends small generated media probes only after that friend is verified in the active QQ friend list. `reconnect_probe.py` is a disruptive test: it terminates only the registered AstrBot child three times, waits for the supervisor's bounded restart and SnowLuma's reverse WS reconnect, and keeps NTQQ running. Run it only when chat traffic is idle, then confirm a fresh `/q1ping` from another QQ client.

## Q3 group routing, controls and permissions

The next normal managed startup installs plugin 0.7.0, including its schema and
Agent, routing, permission and context modules. Q3 development and local acceptance
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
persona remain separate. Q5 now adds exact-owner durable facts plus separately
processed shared projections; see [Q5 memory and diary operations](q5.md).

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

## Q7 native image, animation and video input

Q7 uses the active Qwen3.5-9B-Q8_0 server and a verified F16 projection
component. Image/sticker/GIF/video have independent evidence gates. Model,
component, visual prompt, budget or pipeline changes invalidate old evidence.
All model callers share a foreground-priority queue. See [Q7 operations](q7.md)
and [acceptance evidence](acceptance.md).

Deployment checks must include `VisionGates.enabled()` for all previously enabled
capabilities. The fingerprint includes `request_provider.py`: even adding a
text-only request phase invalidates the saved visual evidence. Unit tests and a
healthy model endpoint do not refresh it. Run the full `probe_q7_model.py
--use-running --enable-passed` acceptance after such a change, then verify the
source and deployed plugin fingerprints agree. Do not replace the saved
fingerprint without successful visual evidence. If Douyin already stopped with
`q7_image_gate_required` or `q7_video_gate_required`, refresh the evidence first,
then use its authenticated `/v1/douyin/resume` endpoint and verify the checkpoint
finishes; merely waiting for QQ idle time cannot clear this persistent halt.

Video sampling is one frame every two seconds, in segments of at most 24 seconds
/ 12 frames. Each model request currently takes at most four frames; further
requests cover the remaining frames. Normal startup deploys the plugin and
loads the projection component once. Set `configs/qq_companion/vision.yaml`
`enabled: false` to disable visual input. The optional local analysis API starts
only with `QQ_COMPANION_VISION_API=1`.

## Q8/Q9 Douyin browsing and native sharing

See [Q8/Q9 operations and API](q8_q9.md). Start with `start_all.ps1 -WithDouyin`
or `QQ_COMPANION_DOUYIN_WORKER=1`. A separate browser environment owns port 8767,
while all inference uses the existing resident Qwen. The default is 20 videos/hour
from 10:00 to 24:00, pausing throughout QQ activity. Frequency changes are hot and
persistent. Sharing uses the configured Douyin account and mutual Douyin friends,
with 5 successful shares/day/recipient and a 1-hour cooldown; no QQ forwarding.
The development build has no first-week manual review. Unknown send outcomes halt
sharing and require evidence-based reconciliation; they are never auto-retried.

## Rollback

Run `stop_all.ps1`, restore the chosen ignored JSON backups from `storage/qq_companion/config_backups`, then restart only the Stage 10 local entry point with `quickstart/run.ps1`. The Stage 10 baseline is `aca495e801f061210ce4961da8df2f63ab3f3c44`; branch `astrbot-chat` includes it. AstrBot/SnowLuma live under ignored `external/`, and the original runtime and training scripts were not replaced.
