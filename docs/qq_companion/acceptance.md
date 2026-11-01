# Q0–Q2 acceptance record

## Q6 acceptance — 2026-10-30 (Asia/Hong_Kong), plugin 0.11.0

Q6 development and the user-authorized self-post acceptance are complete.
The new user scope supersedes the old all-capabilities-at-once gate:
publication and friend interactions have separate evidence. Settings: [Q6](q6.md).

| Gate | Result | Evidence |
|---|---|---|
| Account / cookies / own list and detail | Pass live | Account matched; required fields present; credentials never printed |
| Own publish / visibility / delete | Pass live | Ten temporary posts created, queried and confirmed removed; self visibility and exact three-friend whitelist verified |
| Isolation | Pass | No chat/history/memory insertion path in self-test; separate Qzone outbox |
| Daily policy / important events | Pass locally | Two sentences, one regular post per day; independent unlimited events deduplicated by event/date; seven-day simulation |
| Real local Qwen formatting | Pass | `Q6_MODEL_PASS`: summary, quiet day, important event; GPU admission, no QQ/history/memory writes |
| Quotas, reload, recovery and boundaries | Pass locally | Atomic reservation, unknown-write reconciliation, uncertain-delete verification, exact allowlist and gates;47 Q6 tests |
| Regression | Pass |451 QQ tests passed; final Q6 additions separately verified; original Stage5/6/10 and training33 passed |
| Friend feed / comment / like | Not tested, disabled | User requested no friend-interaction tests; none issued |
| Seven days unattended / friend's client display | Not tested | Simulation and server access rights do not replace those observations |

Evidence stays under ignored `storage/temporary/q6/` and independent
`storage/qq_companion/qzone.sqlite3`. Existing QQ/model services were reused.
No ordinary QQ messages or Qzone friend comments/likes were sent. Plugin files
install on normal managed startup; policy and prompts then reload automatically.

## Q5 acceptance — 2026-10-30 (Asia/Hong_Kong), plugin 0.10.0

Q5 is complete for the requested local acceptance scope. User decisions are
automatic preferences/events/sensitive facts, exact-owner originals with
processed shared summaries, indefinite retention with bounded recall/compaction,
and no forgetting/deletion interface. Implementation and operations: [Q5](q5.md).

| Gate | Result | Evidence |
|---|---|---|
| Durable facts, FTS5, revision and source queue | Pass | Restart, deduplication, Chinese terms, correction revisions, lease/retry, no expiry of generated facts |
| Privacy boundaries | Pass | Different DMs/groups/bots/characters isolated; public projections cannot carry free-form private facts; mixed private diaries excluded from ordinary retrieval |
| Prompt injection and compaction | Pass | Pinned real Runner receives temporary current-user memory; saved history remains clean; changed source invalidates cache; original records preserved |
| Seven daily entries, timezone and retries | Pass locally | Seven-day simulation, date cutoff, missed runs, DST, duplicate requests and transaction uniqueness; empty-material day supported |
| 100 subtitle pseudo-instructions | Pass for fixtures | Rejected by data filtering; extraction/model-tool forgeries have no executor or QQ transport; all utility requests disable tools |
| Full QQ automated regression | Pass | 411 tests passed using `envs/astrbot/Scripts/python.exe`; includes 117 Q5 tests |
| Original Stage 5/6/10 and training | Pass | 33 tests passed using `runtime_env/python.exe` |
| Q3 local adapter probe | Pass | `Q3_LOCAL_PASS`; obsolete completion-approval/system-block fixtures updated to current lifecycle contract |
| Real local Qwen pipeline | Pass | `probe_q5_local.py`: extraction including health fixture, corrected singer preference, actual recall, 64-record compaction, private/public diary, idempotency and isolation; `Q5_MODEL_PASS` |
| Real token and diary sizes | Pass | Memory allowance8192; compressed full request1841 tokens; private/public drafts217/208 characters |
| External side effects | None | Zero QQ connections, zero QQ sends, no Qzone enablement; only a Broker-owned isolated model on18080 was started and stopped |

Synthetic data, temporary databases and the model report are under ignored
`storage/temporary/q5-acceptance/`. Seven simulated days are not seven days of
live unattended operation. Q6/Q7 gates and Q1's historical pending live counts
are unchanged. The new plugin files install on the normal managed startup;
no QQ stack was started for this acceptance.

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

### Q3 local revalidation (2026-10-28)

After rereading the complete phase plan and reviewing the Q3 routing, context,
permission enforcement and real AstrBot local probe, the current plugin 0.5.1
passed the following checks again:

- Q0–Q3, reply pacing and editable prompts: 50 tests passed.
- Stage 5/6/10 and both training pipelines: 33 regression tests passed.
- Pinned AstrBot integration probe: `Q3_LOCAL_PASS`; local fake transport only,
  zero QQ connections and zero model calls. The invalid-prompt error is an
  expected fault-injection check.

No Q3 code gap requiring a change was found. No live service was started or
restarted, and no QQ message was sent. The user's newly confirmed Q4 behavior
is recorded in the phase plan as implementation requirements, not completed
Q4 functionality.

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

## Q4 acceptance record — local gate passed

Date: 2026-10-28 (Asia/Hong_Kong). Plugin 0.6.0. The user confirmed Q4 scope,
persona-controlled voice, model-only intent judgment, original playback unless
AI singing is requested, whole-song covers, and low-frequency stickers. The
user confirmed normal weapi availability as the source boundary. No QQ test
message was sent and no QQ component was started for these checks.

| Gate | Result | Evidence |
|---|---|---|
| Regression | Pass | 107 targeted Q0–Q4, pacing/prompt, Stage 5/6/10, training and SenseVoice tests; one upstream ASGI test-client deprecation warning |
| Pinned AstrBot integration | Pass locally | `Q3_LOCAL_PASS` and `Q4_LOCAL_PASS`; real event/message classes, local transport/inference, 20 voice inputs, 20 voice outputs, 20 sticker sends; correct acknowledgement/job/result/AI-line order and recipient; dedup, long-reply/TTS fallback and image exclusion |
| Real weapi | Pass | `Q4_SOURCE_PASS`: ten successful searches, five nonexistent-song checks, three complete downloads; trials and incomplete files rejected |
| Real Feiyu whole-song conversion | Pass | `Q4_AUDIO_PASS`: 雨爱 260.480→260.44 s, 年轮 274.530→274.48 s, 小幸运 263.820→263.78 s; separation/RVC/mix/24 kHz transport WAV; exact selected model/index hashes verified |
| Real Qwen intent | Pass | `Q4_MODEL_PASS`: 14/14 cases, including original/cover/negative/ambiguous requests and persona voice rules; prompt clarification excludes requested excerpts from the extracted work title |
| Real ASR/TTS API | Pass | Production-local supervisor mode ran SenseVoice 20/20 and Bailu TTS 20/20, with token-authenticated facade and normalized WAV |
| Real GPU unload and restore | Pass | `Q4_STACK_PASS`: actual production callbacks drained and stopped Qwen/ASR/TTS, verified 14564 MiB free, converted a whole song, restored all four services and passed real Qwen and TTS requests; no AstrBot/SnowLuma started |
| Failure/cancel/recovery | Pass locally | Queued jobs survive restart; stopped groups cannot receive stale results; duplicate requests cannot start duplicate tasks; uncertain sends do not auto-repeat; drain timeout does not kill inference; worker/VRAM/stop failures restore; restoration failure keeps admission closed |

Reports and generated audio are ignored local artifacts under
`storage/temporary/q4-{source,audio,model,stack}-acceptance/`. Source/audio/model
probes used a dedicated Broker owner. The final stack probe used disposable
state and the actual production stop/worker/restore callbacks. Every test-owned
process tree was stopped after verification.

Before GPU checks, host-level inspection found seven coordinated companion
service trees left from the 17:55–17:56 run after their supervisor exited.
Verified orphan cleanup restored free VRAM from approximately 2.8 GiB to
14.5 GiB; NTQQ was retained. The cleanup command now requires an orphan cohort
including AstrBot and Media API and excludes standalone local chat and training.

Q4 implementation and the user-requested local acceptance are complete.
Actual QQ client reception/playback, Q1's outstanding live counts, and Q7 visual
recognition are not claimed by this record. Operational details and rerun
commands are in [q4.md](q4.md). Normal next startup installs plugin 0.6.0.

### Bounded search retry follow-up (2026-10-28)

Backend song selection now makes at most three attempts per persisted job,
waiting one and two seconds after transient failures. Timeouts, interrupted
connections/responses and HTTP/business codes 429/500/502/503/504 are retryable.
No-match, unavailable/trial audio, authentication/permission, redirects, TLS
and malformed-response failures are final. These attempts do not retry GPU
synthesis or send a result twice.

Attempt reservations, safe failure codes and per-attempt history survive a
restart. Old song databases migrate without deleting jobs. Recovered synthesis
reuses its selected song, while the download stage still rechecks availability.
Terminal failures remain terminal when the same request ID is resubmitted.

The local Q0–Q4/pacing/prompt/search-retry suite passed 94 tests. Checks include
eventual success, exact retry bounds/backoff, HTTP and business error classes,
cancel during backoff, restart budgets, schema migration and no GPU work on
search failure. The pinned AstrBot Q4 integration probe again returned
`Q4_LOCAL_PASS`. No QQ connection, outgoing QQ message, live weapi request,
GPU model or service restart was used for this follow-up.

### Song reply timing follow-up (2026-10-28)

The pre-song prompt now guides intent and tone without offering a waiting-line
example. Completed jobs wait for model readiness, including while the gate is
`recovering`. The same model prepares the result reply before either message is
sent; one GPU activity lease spans generation and the consecutive audio/text
sends. At this point model-generation failures retained the fallback after
admission; the 2026-10-29 result-reply repair below supersedes that behavior.

The local regression suite passed 94 tests. The pinned AstrBot integration probe
returned `Q4_LOCAL_PASS`, additionally checking restoration/recovery waiting,
no media delivery during generation, a pending delivery surviving interruption,
and retention of the activity lease through both sends when another cover starts
draining. Transport and inference were local fakes; no QQ or model/service
restart was used. These checks verify ordering, not new real-model wording.

### Ctrl+C shutdown repair (2026-10-28)

Host inspection after the reported Ctrl+C found the supervisor already gone,
with its registry and stop flag still present. Qwen, SenseVoice, Media API,
AstrBot and SnowLuma trees remained running; GPU memory used was 11237 MiB.
The original terminal traceback was unavailable, so the precise interruption
that ended this cleanup cannot be uniquely reconstructed. Code inspection found
that repeated interrupts or an individual process error could abort cleanup,
and a dead supervisor had no OS-enforced child lifetime guarantee.

Cleanup now ignores repeated interrupts, attempts every service despite a
disappearing/inaccessible child, retains unresolved registry entries and writes
registry updates atomically. New services enter kill-on-close Windows Job
Objects before their launchers resume. Descendants remain owned after launcher
exit and terminate automatically if the supervisor dies. GPU unload and service
restart use these same owners.

Seven shutdown tests passed, including real local processes with PowerShell
launchers, startup interruption, repeated console control events, launcher exit,
forced supervisor termination, transient process errors and registry retention.
An independent sentinel process survived every shutdown test. Console delivery
uses CTRL_BREAK_EVENT; the SIGINT handler is also checked directly. The broader
local Q0–Q4/search/pacing/prompt/shutdown regression passed 101 tests. No test
started QQ services, made a QQ connection or loaded an actual model.

The old registry's PID/creation-time-verified trees were stopped on the host.
Final host verification: no matching project model/service process, zero
listeners across the nine runtime ports, no supervisor registry, GPU memory
used 1655 MiB/free 14323 MiB. The existing main NTQQ process remained running.

### Song result reply format and provenance (2026-10-29)

The user reported a fixed-looking message after a completed cover. The installed
plugin matched the source. SnowLuma recorded the exact old success fallback at
00:49:41, after the model restore checks completed. A local replay of that stored
persona and job reproduced a natural plain-text answer with a sticker tag;
`parse_followup` rejected it. The old code silently replaced invalid JSON with
a canned success message. No original model response was recorded for that
historical call, so the replay is evidence of the failure path rather than a
recovery of the original output.

The result task now explicitly distinguishes persona text from JSON transport,
requires a nonempty result notification and repeats the task in the final user
turn before the job data. Success templates were removed. Generation/validation
gets at most three calls per delivery run, waiting 1 and 2 seconds between calls;
each provider call has one request attempt and a 45-second caller deadline.
Exhaustion leaves audio and delivery pending for startup recovery, without
sending music alone, synthesizing it again or pretending a fixed line came from
the model. Other terminal API failure notices remain deterministic error text.

The local `song_reply_attempts` table stores each generated/rejected attempt,
bounded raw response, parsed text and safe error type. This allows source and
delivery state to be checked independently. A legacy-schema migration check
confirms pending deliveries survive creation of the audit table.

`Q4_RESULT_PASS`: five real local Qwen result replies through the pinned
AstrBot `Context.llm_generate` and actual OpenAI-compatible Provider, covering
two cover successes, original audio, link-only and failure notifications. All
five produced valid nonempty JSON on their first call. Report and raw replies:
`storage/temporary/q4-result-acceptance/report.json` (ignored local data).
`Q4_LOCAL_PASS` additionally checks rejected plain text/sticker output, null,
eventual valid generation, exact three-call exhaustion, audit contents, no canned
success send and later pending recovery without duplicate media. Its expected
warning/error logs are injected test failures.

The regression suite passed 102 tests. No acceptance test sent QQ messages,
connected a test QQ transport, restarted services or repeated cover synthesis.
Source changes are installed on the next normal stack startup; the already
running plugin was not reloaded during this investigation.

### Thinking and semantic song selection (2026-10-29)

Qwen launch defaults now enable thinking, separate reasoning from visible
content and cap reasoning at 1024 tokens. The AstrBot provider configuration
also sets `chat_template_kwargs.enable_thinking=true` and a positive reasoning
budget. The current local JSON configuration was backed up and updated without
restarting the running services. A normal next stop/start installs the new
plugin/API and applies the launch defaults. Development requests explicitly
enabled thinking on the existing local model rather than starting another one.

Production search now saves up to twenty candidates before selecting anything.
Each job retains the user request and bounded same-session context. The same
AstrBot Provider receives title, artists, album and aliases/subtitles, then
returns a candidate ID or null. The backend only checks candidate membership;
it no longer automatically selects by exact title, duration or first playable
result. Selection happens while the conversation model is loaded, before the
GPU drain/unload/cover transition. Candidate and chosen-song state survive
restart. Empty search or null selection becomes terminal `needs_clarification`,
freeing the queue slot and requesting an AI-written open question for the
accurate name through `music_clarification`. No example or fixed question is
provided. Generation errors retain bounded retries and pending delivery.

`Q4_SELECTION_PASS`: ten real thinking calls/cases through the pinned AstrBot
Context/Provider path: seven version selections (synthetic title variants,
actual artist/album/alias matches, unavailable requested original and a missing
version), two cover intent/title extractions and one clarification. All final
cases returned valid output and nonempty reasoning; only reasoning lengths,
not reasoning text, are stored in selection audit records. The originally
suspected missing 司凤 match was a test-expectation error: three real candidates
carry 司凤版 in their aliases. Final fixtures account for that metadata.
Report: `storage/temporary/q4-selection-acceptance/report.json`.

`THINKING_STARTUP_PASS`: real normal chat, structured tool call and tool-result
follow-up passed with thinking enabled and larger output budgets. The startup
and restoration probes now require final content, avoiding acceptance of only
a reasoning message. The local regression suite passed 117 tests. Updated
`Q3_LOCAL_PASS`/`Q4_LOCAL_PASS` cover routing/privacy plus full local
search → AI selection → worker → restored model reply ordering, no-match/null
clarification, candidate scope checks, late cancellation, legacy migration and
no replacement of unavailable requested versions with playable remixes. Source
and worker in these integration probes are local stand-ins; no QQ message was
sent, no QQ test transport was connected, and no real cover was repeated.

The old source acceptance report saved search outcomes and chosen download
records, not complete candidate lists. As requested, weapi was queried again
for 雨爱、奇迹再现、年轮、叹云兮, twenty results each, without downloading audio.
All eighty candidates, including aliases/subtitles, artist, album, duration,
availability and links, are saved in ignored local files:
`storage/temporary/song-search-results/{results.json,results.md}`.
The search time recorded in the export is 2026-10-29 01:05 (Asia/Hong_Kong).

## Q7：主模型视觉输入（2026-11-01）

图片、静态表情、GIF、视频已分别验收并启用。沿用常驻
`Qwen3.5-9B-Q8_0`，新增校验过的 F16 投影组件；各入口共用
`llama-server:8080`、统一前台优先队列和既有 GPU 资源准入。
没有新增常驻视觉模型。安装、预算、接口和停用方法见 [Q7 操作说明](q7.md)。
用户确定只对好友 `1301049831` 收发真实测试素材，视频按每 2 秒一帧、
每 24 秒一段、每段最多 12 帧处理；60 秒为 12/12/6 帧。
单次推理最多四帧，继续拆批覆盖后续帧，不截掉片段尾部。

### 真实模型与资源记录

完整报告：`storage/temporary/q7-model-acceptance-002/report.json`
（本机忽略文件，保留逐项原始结果与原始配置指纹）。

| 能力 | 样本数 | 目标特征检查通过 |
|---|---:|---:|
| 图片 | 20 | 20 |
| 静态表情 | 20 | 20 |
| GIF | 10 | 10 |
| 视频 | 20 | 20 |

素材为本机原创的几何图形、OCR、表情和动作测试集。这些结果只表示该测试集
目标特征通过，不代表自然照片、复杂视频或人物推理的总体准确率。
人物语气及开放问题的回答质量仍受主模型和提示词影响。
图片另外完成换图同问、同图换问、多图比较四项检查及三十轮图文交替。
共 167 次真实模型调用，前后主模型 PID 一致；前台请求在当前后台批次结束后
优先执行，随后恢复后台队列。视频验证包含全部片段时间覆盖、无声标记和
片段级真实 ASR；分析失败不能当作已理解音轨。

验收时 SenseVoice 与预热后的 GPT-SoVITS 同时常驻。GPU 总使用基线
13114MiB、峰值 13322MiB，最低空闲 2656MiB；模型专用显存峰值
10375.703MiB、模型共享 GPU 内存峰值 128MiB，所有自有进程共享峰值
210MiB。系统 RAM 使用峰值 29277.664MiB，模型 RSS 峰值
9575.855MiB，结束时系统可用约 4.37GiB。GPU/RAM 共记录 959 个采样点。
记录到首内容 token 的 61 次流式调用为 0.063–0.532 秒；该统计主要对应预热后
图文交替请求，不是全部视频任务的首 token 延迟。最大排队约 1.703 秒，
单次请求最长约 17.297 秒。正常验收请求未出现 OOM。

### 真实 QQ 入站与发送修复

`storage/temporary/q7-live-acceptance/qq_report.json` 保存四类素材外发结果，
以及 SnowLuma 日志中好友真正回传的消息：图片 `-397354164`、静态表情
`-2051413373`、GIF `-1456104394`、视频 `901979264`。
外发成功和读取自身发送记录没有计为入站成功。
真实 Provider 请求传入 JPEG 像素；GIF 传入四个不同时刻的帧。
视频真实任务覆盖 0–12 秒，识别 RED/BLUE 及文字移动，无音轨明确记录为
`no_audio`。用户完成回传并确认识别基本正常。

首轮联调发现并修复了 AstrBot 预处理改写原 URL、素材上传晚于文字合并窗口、
以及后台视频结果绕过普通聊天分句与语音发送的问题。当前会保留事件及已携带
引用链的原始 URL，并在同一会话、同一发送者最近 60 秒内关联问题。
视频最终回复现在使用 `reply/thinking_level/voice` 契约与已有 TTS：
文字按普通聊天分句，语音成功只发真实语音，合成失败才回退文字。
动作旁白、错误类型的语音字段及转义后的动作旁白均不能直接外发。
分句中途发送结果不明时保留 `uncertain`，避免自动重发。

修复后的真实模型及 QQ 发送记录位于
`storage/temporary/q7-live-acceptance/native_delivery_report.json`。
图片、静态表情和 GIF 经当前任务提示生成有效回答；同一真实视频缓存用于
独立的文字与语音测试任务，内容相近是测试安排。
语音任务只发送一条 `record`（消息 `-79464908`），没有再发送同内容文字。
明确要求两句话的文字任务发送两条独立 `text`（`-1076840266`、`455602131`）。
初次文字测试实际只有一句话，却要求两条消息，导致测试断言失败；该记录保留，
随后使用明确的两句话问题复测通过。

### 边界验证与证据版本

完整 QQ 回归为 **524 passed / 5 deprecation warnings / 124.45 秒**。
最后补充解码后拒绝语音动作旁白的校验后，Q7、分句节奏与 Q3 边界测试为
**83 passed / 1 deprecation warning / 10.86 秒**。
已有 Stage 5/6/10 与训练入口的相关回归此前通过；未重复执行训练或真实翻唱。
测试覆盖引用链 URL 保留、跨会话隔离、过期问题、权限关闭、取消、恢复、
重试上限、故障后文字继续可用、原始像素序列化和发送结果不明等边界。

可选 API 的实际上传及分析通过，未认证返回 401，四类能力健康状态分别为 true。
损坏图片被拒绝后，同一后端的下一条文字请求成功。
证据分别为 `api_report.json`、`fault_recovery_report.json`，位于上述联调目录。
原始 70 项模型测试指纹保持不变；最终集成指纹、真实模型与 QQ 补充验证、
以及最后的边界测试写入 `post_validation`，据此独立登记四类启用证据。
抖音公开页解析仅完成本地边界验证，未把受限页面或登录内容宣称为真实可下载
测试成功。Q8 浏览器及 Q9 自主分享保持未启用。

最终部署的 36 个插件文件与源码哈希一致，插件 `0.12.0` 成功加载，
真实启动的聊天/工具探针及 OneBot 连接通过；Doctor 的组件校验、四类能力
开关和各本机服务检查通过，可选 API 的四项健康状态均为 true。
验收结束关闭本次测试拥有的服务，保留用户已打开的 NTQQ。

### 补发末尾反复出现语气词（2026-11-01，0.12.1）

用户随后启动正常服务，在 15:23:49、15:25:03、15:26:00、15:26:46
四次收到独立的“嗯…”补发。`storage/qq_companion/agent.sqlite3` 的
`followup_generated` 与原始 Provider 调试记录显示，模型每次实际生成了以
省略号开头的完整补发句子。旧 `parse_followup` 使用普通聊天的分段函数，
将省略号当作句末，再仅保留第一段，因此正文被截掉；不是固定兜底回复。
角色提示词列出口语填充的写法，使模型更容易采用此类开头。

旧发送钩子同时将未裁剪的完整生成 JSON 写成已发送历史，产生了“实际只发
语气词、历史却认为整句话已发送”的不一致。修复后补发按完整句末限制一句，
保留省略号、波浪号、分号后的正文；80 字、重复检查、单条配额及插话取消
仍有效。历史只保存实际发送正文和协议字段，原始生成继续单独审计。
新增的阶段提示约束没有具体新内容时返回 null，并明确历史 follow_up
不应成为本轮主回复的输出格式；没有修改角色人格或清空旧聊天。

163 项定向回归通过，包含四种停顿开头、完整长度、重复检查，以及真实
AstrBot Runner 的补发发送/历史/原始审计一致性。四条故障原始结果可离线
重放，完整正文均不再被截为单独语气词；另外在同一常驻 Qwen 上回放原
请求，报告位于 `storage/temporary/followup-pause-fix/report.json`。
这些回放不发送 QQ 消息、不改写已有聊天记录。
修复已备份并部署，只重启现有监督器拥有的 AstrBot 子进程，插件
`0.12.1` 成功加载且 OneBot 重新连接；监督器、Qwen、ASR、TTS、媒体
服务和 SnowLuma 的 PID 保持不变。部署记录见同目录 `deployment.json`。
最终完整 QQ 回归 **530 passed / 5 deprecation warnings / 131.74 秒**，
部署后的 Doctor 全部通过，四项视觉能力继续有效。

### 记忆证据与重复摘要修复（2026-11-01，0.12.2）

真实记录暴露了三类问题：固定共享话题按日期重复保存；提取器把
`known_facts` 当作当前消息重新输出，覆盖原始来源；临时的“不用唱啦”
被误写为用户音乐偏好，覆盖了已确认的林俊杰偏好。

候选现须携带当前消息的连续原文 `evidence`，逐字校验后使用独立只读
复核任务判断含义、长期价值及已有属性对应关系；复核失败不提交事实。
共享话题保留一个活动摘要，日期观察单独保存以支持日记。
同主体同文事实复用记录，召回再去重，历史重复项归档并保留修订。

定向回归 135 项、完整 QQ 回归 **541 passed / 5 warnings / 148.85 秒**。
同一常驻 Qwen 的九项隔离数据库实测通过，包含临时请求拒绝、旧事实
不刷新、歌手纠正、称呼、风格、照片地点及错误因果候选的独立复核。
没有发送 QQ 消息；复核仍受同一本地模型的语义判断能力限制。

正式库先通过 SQLite backup 完整备份，再合并两组重复摘要、恢复六条
事实或来源（含林俊杰偏好）、归档两条错误记录。活动记录 18→14，
用户事实七条，活动摘要同文重复为零，完整性校验通过，无记忆硬删除。
报告、修复计划和备份位于忽略目录
`storage/temporary/memory-quality-fix/`。

四个变更插件文件已备份并部署，0.12.2 成功加载且 OneBot 重新连接。
只重启监督器拥有的 AstrBot 子进程；其他服务 PID 不变。
部署后的 Doctor 全部通过，四项视觉能力继续有效。
