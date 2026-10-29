# QQ companion scripts

Run from the project root. See [operations](../../docs/qq_companion/operations.md) for Q0–Q4 setup, acceptance and shutdown. The Feiyu song installers maintain Q4's local model assets.

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

`probe_q4_local.py` runs with the AstrBot Python environment and fake transport/inference. `probe_q4_source.py`, `probe_q4_audio.py`, `probe_q4_model.py` and `supervisor.py --local-q4-acceptance` run with the model environment for real local backend checks. They never start QQ components. Run GPU probes sequentially. See [Q4 operations](../../docs/qq_companion/q4.md).

`search_music.py` exports twenty weapi candidates each for 雨爱、奇迹再现、年轮、叹云兮,
including aliases/subtitles, without downloading audio. Run `probe_q4_selection.py`
with the AstrBot Python environment against the existing ready local model to
check thinking, semantic version selection and no-match clarification. It uses
activity leases and has no QQ transport. Production jobs now wait for the
Agent's choice before starting the song worker.
