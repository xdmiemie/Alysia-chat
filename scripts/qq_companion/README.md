# QQ companion scripts

Run from the project root. See [operations](../../docs/qq_companion/operations.md) for Q0–Q2 setup, acceptance and shutdown. The existing Feiyu song installers remain Q4 reference utilities.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/install_astrbot.ps1
runtime_env/python.exe scripts/qq_companion/configure.py --apply
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/doctor.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/start_all.ps1 -NoModels
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/start_all.ps1 -WithTTS
runtime_env/python.exe -m scripts.qq_companion.probe_media --count 10
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/qq_companion/stop_all.ps1
```

During authorized live tests, `runtime_env/python.exe scripts/qq_companion/live_counts.py` shows aggregate inbound private, group, @, image, voice and reply counts from ignored local SQLite. `live_media_probe.py` can send a small image and one-second tone to an explicitly authorized friend or the sole test group; run `--help` for required target options. Confirm delivery on the other QQ client before recording an acceptance result.
