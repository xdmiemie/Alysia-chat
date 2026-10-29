"""Three real whole-song Feiyu conversions using locally verified weapi files.

Never starts QQ or touches another application's processes. Re-run after the
12 GiB free-VRAM gate is met. Uses the existing global GPU ownership lock.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.qq_companion.gpu_broker import GPUResourceBroker
from scripts.qq_companion.supervisor import stop_tree
from services.media_api.songs import SongStore


def run(source_dir: Path):
    report = json.loads((source_dir / "report.json").read_text(encoding="utf-8"))
    sources = report.get("downloads", [])
    if len(sources) < 3:
        raise RuntimeError("Three complete, verified sources are required; run probe_q4_source.py first")
    broker = GPUResourceBroker()
    broker.acquire("q4_local_audio_acceptance")
    directory = ROOT / "storage/temporary/q4-audio-acceptance"
    jobs = SongStore(directory)
    receipts = []
    try:
        available = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits", "--id=0"],
                                    capture_output=True, text=True, timeout=15, check=True)
        if int(available.stdout.strip()) < 12288:
            raise RuntimeError("GPU gate not met: free VRAM must be at least 12288 MiB")
        for source in sources[:3]:
            key = uuid.uuid4().hex
            song = source["song"]
            jobs.submit(key, "qq:999:dm:101", {"mode": "cover", "query": song["title"], "artist": song["artists"]})
            jobs.update(key, "running", song=song)
            log = directory / (key + ".log")
            with log.open("wb") as stream:
                worker = subprocess.Popen([str(ROOT / "runtime_env/python.exe"), "-B",
                    str(ROOT / "scripts/qq_companion/run_cover_job.py"), "--job", key,
                    "--state-dir", str(directory), "--local-source", str(source_dir / source["file"])],
                    cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                created = psutil.Process(worker.pid).create_time()
                try:
                    worker.wait(timeout=1800)
                finally:
                    stop_tree(worker.pid, created)
            if worker.returncode:
                jobs.update(key, "failed", error="audio_acceptance_failed")
                raise RuntimeError(f"Feiyu conversion failed; inspect {log}")
            jobs.update(key, "completed")
            receipt = json.loads((jobs.output(key).parent / "receipt.json").read_text(encoding="utf-8"))
            receipts.append({"song": song["title"], "receipt": receipt, "output": str(jobs.output(key))})
            print(f"whole-song conversion {len(receipts)}/3 passed", flush=True)
        (directory / "report.json").write_text(json.dumps({"receipts": receipts, "qq_connections": 0},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("Q4_AUDIO_PASS: three whole songs, separation/RVC/mix/transport WAV; QQ connections: 0.")
    finally:
        broker.release()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, default=ROOT / "storage/temporary/q4-source-acceptance")
    raise SystemExit(run(parser.parse_args().source_dir))
