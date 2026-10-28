# Q0–Q1 acceptance record

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

Q0 has a recoverable baseline and isolated installation. Q1 cannot be declared accepted until the live counts and media checks above pass. The observed QQ account reported zero groups, so group testing needs an account with an authorized test group. Do not mark a simulated or unit test as a QQ conversation.
