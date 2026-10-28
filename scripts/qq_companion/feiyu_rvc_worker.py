"""Run the verified FeiyuVoice release with bounded FAISS search threads."""

from __future__ import annotations

import argparse
import builtins
import runpy
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    runner = args.runner.resolve(strict=True)
    if runner.name != "run_rvc.py":
        raise ValueError("unexpected RVC runner")

    # Let the release import Torch/FAISS in its validated order. Importing
    # either here loads conflicting OpenMP DLLs on this Windows build.
    original_import = builtins.__import__
    pinned = False

    def set_faiss_threads(name, *args, **kwargs):
        nonlocal pinned
        module = original_import(name, *args, **kwargs)
        if not pinned and name == "faiss" and hasattr(module, "omp_set_num_threads"):
            # Four FAISS threads stalled a 3,000-frame query for minutes;
            # one thread completes it in milliseconds on this index.
            module.omp_set_num_threads(1)
            pinned = True
            print(f"FAISS OpenMP threads: {module.omp_get_max_threads()}", flush=True)
        return module

    builtins.__import__ = set_faiss_threads
    sys.argv = [str(runner), *remaining]
    runpy.run_path(str(runner), run_name="__main__")


if __name__ == "__main__":
    main()
