# QQ companion scripts

NetEase account QR login and persistent encrypted sessions:
[login instructions](../../docs/qq_companion/netease_login.md).
Run `envs/astrbot/Scripts/python.exe scripts/qq_companion/netease_login.py`;
use `--status` for a read-only login check. This does not start any bot or model.

Run from the project root. See [operations](../../docs/qq_companion/operations.md) for Q0–Q4 setup, acceptance and shutdown. The Feiyu song installers maintain Q4's local model assets.

Qwen3 runtime settings are shared in [`configs/runtime/qwen3.yaml`](../../configs/runtime/qwen3.yaml):
32768 context tokens, Q8_0 K/V cache, 1536 output tokens, and a 1024-token thinking budget.
The launcher and AstrBot provider generator read the same file; restart the managed
stack after changes to apply both. Internal request budgeting uses the generated provider settings.

Complete model requests and raw API responses are recorded in
`storage/qq_companion/logs/model_debug/model-*.json`: indented JSON arrays with
line breaks, 50 request/response exchanges per file, at most five files, deleting
the oldest on rotation. Old JSONL files are automatically converted.
The archive covers the Agent, retries, post-reply decisions, song notifications
and summaries. `ASTRBOT_CHAT_MODEL_DEBUG_DIR` overrides the location. See
[the log field definitions](../../docs/qq_companion/agent.md#完整模型调试日志).

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/install_astrbot.ps1
runtime_env/python.exe scripts/qq_companion/configure.py --apply
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/doctor.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/start_all.ps1 -NoModels
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/start_all.ps1 -WithTTS
runtime_env/python.exe -m scripts.qq_companion.probe_media --count 10
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/stop_all.ps1
```

Q3 local acceptance uses the pinned AstrBot Python environment and fake
transport/inference; it never connects to QQ or starts a model:

```powershell
envs/astrbot/Scripts/python.exe scripts/qq_companion/probe_q3_local.py
runtime_env/python.exe -m pytest tests/test_qq_q3.py -q --basetemp storage/temporary/pytest_q3
```

`supervisor.py` owns each launched tree with a Windows Job Object. Ctrl+C
starts cleanup and further interrupts do not cut it short; supervisor death
also terminates its managed descendants. `test_qq_shutdown.py` checks startup
interrupts, repeated console interrupts, launcher exit and forced supervisor
termination using local stand-ins, without QQ or actual models.

During authorized live tests, `runtime_env/python.exe scripts/qq_companion/live_counts.py` shows aggregate inbound private, group, @, image, voice and reply counts from ignored local SQLite. `live_media_probe.py` can send a small image and one-second tone to an explicitly authorized friend or the sole test group; run `--help` for required target options. Confirm delivery on the other QQ client before recording an acceptance result.

# Q4 local acceptance

`probe_q4_local.py` runs with the AstrBot Python environment and fake transport/inference. `probe_q4_source.py`, `probe_q4_audio.py` and `supervisor.py --local-q4-acceptance` run with the model environment for real local backend checks. `probe_q4_model.py` now delegates to current native-Agent acceptance in the AstrBot environment; `--quick` verifies direct chat, post-Agent follow-up and native song tools. They never start QQ components. Run GPU probes sequentially. See [Q4 operations](../../docs/qq_companion/q4.md).

`search_music.py` exports twenty weapi candidates each for 雨爱、奇迹再现、年轮、叹云兮,
including aliases/subtitles, without downloading audio. Run `probe_q4_selection.py`
with the AstrBot Python environment against the existing ready local model to
check thinking, semantic version selection and no-match clarification. It uses
activity leases and has no QQ transport. Production jobs now wait for the
Agent's choice before starting the song worker.
