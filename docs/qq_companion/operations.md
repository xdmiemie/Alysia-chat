# Q0–Q1 operations (Windows 11)

## Install and configure

1. Keep the existing `runtime_env` Python 3.10 environment for Stage 0–10. Install official Python 3.12.10 into `external/Python312`, then run `scripts/qq_companion/install_astrbot.ps1`. The script verifies AstrBot v4.29.3 at `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`, creates `envs/astrbot`, installs its dependencies and initializes `external/AstrBot/data`.
2. Place the official SnowLuma v2.1.1 Windows x64 release at `external/SnowLuma/v2.1.1`. Keep its local EULA/privacy choice under its ignored config. Log in to NTQQ yourself. SnowLuma is a nonofficial QQ integration with account restriction risk; use is subject to its [noncommercial license](https://github.com/SnowLuma/SnowLuma) and bundled EULA.
3. Stop both services, then run `runtime_env/python.exe scripts/qq_companion/configure.py --apply` once to inspect the selected account and pairing. The start script also applies this pairing automatically before launching services, and backs up local JSON only when it changes. If multiple SnowLuma accounts are present, select the desired `onebot_*.json` file with `--snowluma-config`; set `QQ_COMPANION_SNOWLUMA_CONFIG` to that file's path before using the start script. You may set `SNOWLUMA_ONEBOT_TOKEN` in the current process to override the existing local token. Do not paste the token into chat or Git.
4. Run `scripts/qq_companion/doctor.ps1`. It reports configuration and environment readiness without printing account IDs or tokens. Open AstrBot WebUI at `http://127.0.0.1:6185` after startup and change the initial administrator password. SnowLuma WebUI uses `http://127.0.0.1:5099`.

## Start and stop

`scripts/qq_companion/start_all.ps1 -NoModels` pairs the local OneBot token, installs the guard, then starts the Q1 OneBot protocol and `/q1ping` test. Omit `-NoModels` to start the existing Qwen3 llama-server before AstrBot. Add `-WithTTS` to also start the existing GPT-SoVITS upstream and wrapper. TTS is not yet wired into QQ until Q2/Q4. The script intentionally does not run the old local Agent or force NTQQ login. SnowLuma attaches to the already logged-in client.

Keep the starting terminal open. Ctrl+C calls the managed shutdown. From a second terminal, `scripts/qq_companion/stop_all.ps1` also stops its registered child trees. Both leave NTQQ running. Check ignored logs in `storage/qq_companion/logs` if a process exits or a port is occupied. A stale registry does not authorize stopping a reused PID.

## Q1 live acceptance

First run `scripts/qq_companion/test_onebot.ps1`. A success only proves SnowLuma's local OneBot endpoint and login, not AstrBot connectivity. Confirm the AstrBot log reports `aiocqhttp(OneBot v11) ... connected`. Send `/q1ping` in a test private chat and test group; expect one `q1pong` each. The guard drops repeat `(account_id, message_id)` events and bot self messages.

Record private chat 50 turns, group 50 turns, @ 20 turns, image ingress 10 messages, and three connection interruptions/recoveries in [acceptance.md](acceptance.md). Include reply/quote, outbound text and image, and a voice upload/playback check. Record counts and timestamps only in the tracked report; keep message IDs, account IDs and content in ignored local notes if needed. Confirm no self reply loop, cross-user context leak or duplicate reply. If an item fails, keep Q1 marked incomplete and diagnose the interface. Do not replace SnowLuma with another protocol implementation.

`runtime_env/python.exe scripts/qq_companion/live_counts.py` prints aggregate inbound message, @bot, image, voice and quote counts without account IDs. `live_media_probe.py --user-id <authorized-friend-id> --target private|group|both --kind image|voice|both` sends small generated media probes only after that friend is verified in the active QQ friend list. `reconnect_probe.py` is a disruptive test: it terminates only the registered AstrBot child three times, waits for the supervisor's bounded restart and SnowLuma's reverse WS reconnect, and keeps NTQQ running. Run it only when chat traffic is idle, then confirm a fresh `/q1ping` from another QQ client.

## Rollback

Run `stop_all.ps1`, restore the chosen ignored JSON backups from `storage/qq_companion/config_backups`, then restart only the Stage 10 local entry point with `quickstart/run.ps1`. The Stage 10 baseline is `aca495e801f061210ce4961da8df2f63ab3f3c44`; branch `astrbot-chat` includes it. AstrBot/SnowLuma live under ignored `external/`, and the original runtime and training scripts were not replaced.
