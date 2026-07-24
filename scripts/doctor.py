from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.common.config import load_audio_config


PORTS = {
    "companion-app": 8000,
    "sensevoice-service": 5001,
    "llama-server": 8080,
    "gpt-sovits-upstream": 9880,
    "gpt-sovits-wrapper": 5002,
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"OK", "WARN"}


def run_command(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def check_gpu() -> CheckResult:
    code, output = run_command(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=4)
    if code == 0 and output:
        names = ", ".join(line.strip() for line in output.splitlines() if line.strip())
        return CheckResult("NVIDIA GPU detected", "OK", names)
    return CheckResult("NVIDIA GPU detected", "FAIL", "nvidia-smi did not report a GPU")


def check_cuda() -> CheckResult:
    try:
        import torch
    except ImportError:
        torch = None

    if torch is not None and torch.cuda.is_available():
        return CheckResult("CUDA available", "OK", f"torch {torch.__version__}")

    env_python = _runtime_env_path("python.exe")
    if env_python.exists():
        code, output = run_command(
            [
                str(env_python),
                "-c",
                "import torch; print(torch.__version__ if torch.cuda.is_available() else '')",
            ],
            timeout=8,
        )
        if code == 0 and output.strip():
            return CheckResult("CUDA available", "OK", f"runtime torch {output.strip().splitlines()[0]}")

    code, output = run_command(["nvcc", "--version"], timeout=4)
    if code == 0 and "release" in output:
        version_line = next((line.strip() for line in output.splitlines() if "release" in line), "")
        return CheckResult("CUDA available", "OK", version_line)

    return CheckResult("CUDA available", "FAIL", "torch CUDA or nvcc was not available")


def check_ffmpeg() -> CheckResult:
    exe = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not exe.exists():
        return CheckResult("Project FFmpeg available", "FAIL", "tools/ffmpeg/bin/ffmpeg.exe")

    code, output = run_command([str(exe), "-version"], timeout=4)
    if code == 0:
        first_line = output.splitlines()[0] if output else str(exe)
        return CheckResult("Project FFmpeg available", "OK", first_line)
    return CheckResult("Project FFmpeg available", "FAIL", output or str(exe))


def check_llama_server() -> CheckResult:
    tool_root = PROJECT_ROOT / "tools" / "llama.cpp"
    candidates = list(tool_root.rglob("llama-server.exe")) if tool_root.exists() else []
    if not candidates:
        return CheckResult("Project llama-server available", "FAIL", "tools/llama.cpp/**/llama-server.exe")
    return CheckResult("Project llama-server available", "OK", str(candidates[0].relative_to(PROJECT_ROOT)))


def _runtime_env_path(*parts: str) -> Path:
    project_runtime = PROJECT_ROOT / "runtime_env" / Path(*parts)
    project_candidates = [project_runtime]
    if parts == ("python.exe",):
        project_candidates.append(PROJECT_ROOT / "runtime_env" / "Scripts" / "python.exe")
    for candidate in project_candidates:
        if candidate.exists():
            return candidate

    for env_var in ("ALYSIA_PYTHON", "GPT_SOVITS_PYTHON"):
        value = os.environ.get(env_var)
        if value:
            return Path(value).parent / Path(*parts)
    return project_runtime

def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'sounddevice'. Install with "
            "'powershell -NoProfile -ExecutionPolicy Bypass -File quickstart/bootstrap.ps1'."
        ) from exc
    return sd


def check_audio_devices() -> list[CheckResult]:
    try:
        config = load_audio_config()
        sd = _sounddevice()
        devices = list(sd.query_devices())
    except Exception as exc:
        return [
            CheckResult("Microphone detected", "FAIL", str(exc)),
            CheckResult("Speaker detected", "FAIL", str(exc)),
        ]

    input_devices = [
        (index, device)
        for index, device in enumerate(devices)
        if int(device["max_input_channels"]) > 0
    ]
    output_devices = [
        (index, device)
        for index, device in enumerate(devices)
        if int(device["max_output_channels"]) > 0
    ]

    results = []
    if config.input_device is not None:
        valid = 0 <= int(config.input_device) < len(devices) and int(devices[int(config.input_device)]["max_input_channels"]) > 0
        results.append(
            CheckResult(
                "Microphone detected",
                "OK" if valid else "FAIL",
                f"configured input_device={config.input_device}" if valid else "configured input_device is not an input device",
            )
        )
    else:
        results.append(
            CheckResult(
                "Microphone detected",
                "OK" if input_devices else "FAIL",
                f"{len(input_devices)} input device(s)",
            )
        )

    if config.output_device is not None:
        valid = 0 <= int(config.output_device) < len(devices) and int(devices[int(config.output_device)]["max_output_channels"]) > 0
        results.append(
            CheckResult(
                "Speaker detected",
                "OK" if valid else "FAIL",
                f"configured output_device={config.output_device}" if valid else "configured output_device is not an output device",
            )
        )
    else:
        results.append(
            CheckResult(
                "Speaker detected",
                "OK" if output_devices else "FAIL",
                f"{len(output_devices)} output device(s)",
            )
        )
    return results


def check_port_free(name: str, port: int) -> CheckResult:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        occupied = sock.connect_ex(("127.0.0.1", port)) == 0
    return CheckResult(
        f"Port {port} free for {name}",
        "FAIL" if occupied else "OK",
        "port is already occupied" if occupied else "available",
    )


def load_required_files(manifest_path: Path) -> list[Path]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = data.get("required_files", [])
    if not isinstance(required, list):
        raise ValueError(f"{manifest_path} field 'required_files' must be a list")
    return [(PROJECT_ROOT / str(path)).resolve() for path in required]


def check_model(name: str, manifest: str) -> CheckResult:
    manifest_path = PROJECT_ROOT / manifest
    if not manifest_path.exists():
        return CheckResult(f"{name} model found", "MISSING", f"manifest missing: {manifest}")

    try:
        required_files = load_required_files(manifest_path)
    except Exception as exc:
        return CheckResult(f"{name} model found", "FAIL", str(exc))

    missing = [path for path in required_files if not path.exists()]
    if missing:
        detail = "; ".join(str(path.relative_to(PROJECT_ROOT)) for path in missing)
        return CheckResult(f"{name} model found", "MISSING", detail)
    return CheckResult(f"{name} model found", "OK", str(manifest_path.relative_to(PROJECT_ROOT)))


def check_third_party_manifest() -> CheckResult:
    path = PROJECT_ROOT / "configs/third_party.yaml"
    if path.exists():
        return CheckResult("Third-party dependency manifest found", "OK", str(path.relative_to(PROJECT_ROOT)))
    return CheckResult("Third-party dependency manifest found", "MISSING", "configs/third_party.yaml")


def collect_checks() -> list[CheckResult]:
    results = [
        check_gpu(),
        check_cuda(),
        check_ffmpeg(),
        check_llama_server(),
        check_third_party_manifest(),
    ]
    results.extend(check_audio_devices())
    results.extend(check_port_free(name, port) for name, port in PORTS.items())
    results.extend(
        [
            check_model("SenseVoice", "model_store/active/sensevoice.json"),
            check_model("Qwen3", "model_store/active/qwen3.json"),
            check_model("GPT-SoVITS base", "model_store/active/gpt_sovits_elysia.json"),
        ]
    )
    return results


def print_results(results: Iterable[CheckResult]) -> None:
    for result in results:
        suffix = f" - {result.detail}" if result.detail else ""
        print(f"[{result.status}] {result.name}{suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 0 environment doctor.")
    parser.add_argument("--strict", action="store_true", help="Return a non-zero exit code when checks fail.")
    args = parser.parse_args(argv)

    results = collect_checks()
    print_results(results)
    if args.strict and any(not result.ok for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
