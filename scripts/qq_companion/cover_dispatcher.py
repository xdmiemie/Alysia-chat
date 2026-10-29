"""Cover transitions under the supervisor's existing cross-process GPU owner."""

import time

from integrations.qq_astrbot.plugin.media_state import MediaState
from services.media_api.songs import SongStore, search_candidates_with_retry
from services.media_api.song_source import NetEaseSource, SongSourceError


class CoverDispatcher:
    def __init__(self, directory, *, broker, stop_models, restore_models, verify_gpu, run_worker,
                 stopping=lambda: False, source=None, drain_timeout=180):
        self.state, self.jobs = MediaState(directory), SongStore(directory)
        self.broker = broker
        self.stop_models, self.restore_models = stop_models, restore_models
        self.verify_gpu, self.run_worker, self.stopping = verify_gpu, run_worker, stopping
        self.source = source or NetEaseSource.from_environment()
        self.drain_timeout = drain_timeout

    def step(self):
        job = self.jobs.next_job()
        if not job:
            return
        key = job["id"]
        stopped = None
        restored = True
        try:
            if self.broker.file is None:
                raise RuntimeError("cover_without_gpu_owner")
            if not job["song"]:
                self.jobs.update(key, "searching")
                candidates = search_candidates_with_retry(self.source, job["payload"],
                    attempts_used=job["search_attempts"], last_error=job["search_error"],
                    on_attempt=lambda attempt, error: self.jobs.record_search_attempt(key, attempt, error),
                    should_stop=lambda: self.stopping() or self.jobs.get(key)["status"] == "canceled")
                self.jobs.record_candidates(key, candidates)
                return  # The same AstrBot Agent selects before any GPU work.
            song = job["song"]
            self.jobs.update(key, "waiting_gpu", song=song)
            if not song["available"]:
                self.jobs.update(key, "completed", error="source_unavailable_link_only")
                return
            if not 5 <= song["duration_seconds"] <= 600:
                raise ValueError("song_duration_out_of_range")
            if job["payload"]["mode"] == "original":
                self.jobs.update(key, "running")
                self.run_worker(key)  # CPU download/transcode only; never loads RVC.
                if not self.jobs.output(key).is_file():
                    raise RuntimeError("missing_original_output")
                self.jobs.update(key, "completed")
                return
            self.state.set_mode("draining")
            deadline = time.monotonic() + self.drain_timeout
            while self.state.active():
                if self.stopping() or time.monotonic() > deadline:
                    raise TimeoutError("chat_drain_timeout")
                time.sleep(0.1)
            if self.jobs.get(key)["status"] == "canceled":
                return
            # This callback tracks a partial stop as well, allowing restoration
            # even when one model fails to stop cleanly.
            stopped = []
            self.stop_models(stopped)
            self.verify_gpu()
            self.state.set_mode("cover")
            self.jobs.update(key, "running")
            self.run_worker(key)
            if self.jobs.get(key)["status"] != "canceled":
                if not self.jobs.output(key).is_file():
                    raise RuntimeError("missing_cover_output")
                self.jobs.update(key, "completed")
        except Exception as exc:
            self.jobs.update(key, "failed", error=exc.code if isinstance(exc, SongSourceError) else str(exc) if str(exc) in {
                "song_not_found", "song_duration_out_of_range", "chat_drain_timeout"} else type(exc).__name__)
        finally:
            if stopped is not None:
                self.state.set_mode("restoring")
                try:
                    self.restore_models(stopped)
                except Exception:
                    restored = False
                    self.state.set_mode("recovering")
                    self.jobs.update(key, "failed", error="model_restore_failed")
            if restored:
                self.state.set_mode("ready")

