from __future__ import annotations

import argparse
import audioop
import json
import subprocess
import sys
import wave
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.sensevoice.service import SenseVoiceRecognizer
from training.gpt_sovits.config import TrainingConfig, file_sha256, load_training_config, stable_hash


REQUIRED_FIELDS = {"audio_path", "text", "speaker", "emotion", "language", "source"}


@dataclass(frozen=True)
class DatasetItem:
    audio_path: str
    resolved_audio_path: Path
    text: str
    speaker: str
    emotion: str
    language: str
    source: str
    duration: float
    sample_rate: int | None
    channels: int | None
    audio_hash: str

    def to_json_record(self) -> dict[str, object]:
        return {
            "audio_path": self.audio_path,
            "text": self.text,
            "speaker": self.speaker,
            "emotion": self.emotion,
            "language": self.language,
            "source": self.source,
            "duration": round(self.duration, 3),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "audio_hash": self.audio_hash,
        }


@dataclass(frozen=True)
class DatasetValidation:
    ready: bool
    manifest_path: Path
    normalized_manifest: Path
    report_path: Path
    items: tuple[DatasetItem, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    dataset_hash: str
    total_duration_seconds: float
    stats: dict[str, object] = field(default_factory=dict)

    def to_report(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "manifest_path": _relative(self.manifest_path),
            "normalized_manifest": _relative(self.normalized_manifest),
            "record_count": len(self.items),
            "total_duration_seconds": round(self.total_duration_seconds, 3),
            "dataset_hash": self.dataset_hash,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "stats": self.stats,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and validate a GPT-SoVITS voice dataset.")
    parser.add_argument("--config", default="training/gpt_sovits/configs/characters/elysia.yaml")
    parser.add_argument("--prepare", action="store_true", help="Scan raw WAV files and transcribe new/changed clips.")
    parser.add_argument("--force", action="store_true", help="Regenerate transcripts even when the manifest is current.")
    parser.add_argument("--sensevoice-config", default="configs/runtime/sensevoice.yaml")
    parser.add_argument("--write-normalized", action="store_true", default=True)
    parser.add_argument("--no-write-normalized", dest="write_normalized", action="store_false")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_training_config(args.config)
    if args.prepare:
        prepare_dataset(
            config,
            sensevoice_config=args.sensevoice_config,
            force=args.force,
        )
    validation = validate_dataset(config, write_normalized=args.write_normalized)
    if args.json:
        print(json.dumps(_cli_report(validation), ensure_ascii=False, indent=2))
    else:
        _print_human_report(validation)
    return 0 if validation.ready else 1


def prepare_dataset(
    config: TrainingConfig,
    *,
    sensevoice_config: str | Path = "configs/runtime/sensevoice.yaml",
    force: bool = False,
) -> bool:
    raw_audio_dir = config.dataset.raw_audio_dir
    if not raw_audio_dir.exists():
        raise RuntimeError(
            "missing raw audio directory: "
            f"{_relative(raw_audio_dir)}. Put Stage 8 WAV files under this directory."
        )

    audio_files = sorted(path for path in raw_audio_dir.rglob("*.wav") if path.is_file())
    if not audio_files:
        raise RuntimeError(f"no WAV files found under {_relative(raw_audio_dir)}")

    audio_index = _audio_index(config, audio_files)
    existing = _load_manifest_records(config.dataset.raw_manifest)
    existing_by_hash = {
        str(record.get("audio_hash")): record
        for record in existing
        if isinstance(record, dict) and record.get("audio_hash")
    }
    if not force and _manifest_current(existing, audio_index):
        language, changed = _apply_majority_language(existing, config.language)
        if changed:
            _write_manifest_records(config.dataset.raw_manifest, existing)
            print(f"Normalized GPT-SoVITS dataset manifest language to '{language}': {_relative(config.dataset.raw_manifest)}")
            return True
        print(f"GPT-SoVITS dataset already processed: {_relative(config.dataset.raw_manifest)}")
        return False

    recognizer: SenseVoiceRecognizer | None = None
    records: list[dict[str, object]] = []
    for item in audio_index:
        cached = existing_by_hash.get(str(item["audio_hash"]))
        if cached and not force:
            record = dict(cached)
            record["audio_path"] = item["audio_path"]
            record["duration"] = item["duration"]
            record["sample_rate"] = item["sample_rate"]
            record["channels"] = item["channels"]
            records.append(record)
            continue

        if recognizer is None:
            recognizer = SenseVoiceRecognizer.from_config(sensevoice_config)
        result = recognizer.transcribe(Path(item["resolved_audio_path"]))
        if result.skipped or not result.text.strip():
            raise RuntimeError(
                f"SenseVoice could not transcribe {_relative(Path(item['resolved_audio_path']))}: "
                f"{result.reason or result.event}"
            )
        records.append(
            {
                "audio_path": item["audio_path"],
                "text": result.text.strip(),
                "speaker": config.speaker,
                "emotion": _map_sensevoice_emotion(result.emotion),
                "language": result.language if result.language != "unknown" else config.language,
                "source": "sensevoice",
                "duration": item["duration"],
                "sample_rate": item["sample_rate"],
                "channels": item["channels"],
                "audio_hash": item["audio_hash"],
                "transcription_inference_ms": result.inference_ms,
            }
        )

    language, _changed = _apply_majority_language(records, config.language)
    _write_manifest_records(config.dataset.raw_manifest, records)
    print(f"Prepared GPT-SoVITS dataset manifest: {_relative(config.dataset.raw_manifest)}")
    print(f"Unified GPT-SoVITS dataset manifest language to '{language}'")
    return True


def validate_dataset(config: TrainingConfig, *, write_normalized: bool = True) -> DatasetValidation:
    manifest = config.dataset.raw_manifest
    errors: list[str] = []
    warnings: list[str] = []
    items: list[DatasetItem] = []
    stats: dict[str, object] = {"total_samples": 0, "too_short": 0, "too_long": 0,
                                "empty_text": 0, "damaged": 0, "duplicates": 0}

    if not manifest.exists():
        errors.append(
            "missing processed dataset manifest: "
            f"{_relative(manifest)}. Put WAV files under {_relative(config.dataset.raw_audio_dir)} "
            "and run this command with --prepare, or use quickstart/train_gpt_sovits.ps1."
        )
        validation = _validation(config, items, errors, warnings, stats)
        config.dataset.validation_report.parent.mkdir(parents=True, exist_ok=True)
        config.dataset.validation_report.write_text(json.dumps(validation.to_report(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return validation

    seen_audio_hashes: dict[str, str] = {}
    for line_number, raw_line in enumerate(manifest.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_number}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(record, dict):
            errors.append(f"line {line_number}: record must be a JSON object")
            continue
        stats["total_samples"] = int(stats["total_samples"]) + 1
        missing = sorted(REQUIRED_FIELDS - set(record))
        if missing:
            errors.append(f"line {line_number}: missing required field(s): {', '.join(missing)}")
            continue

        text = str(record.get("text", "")).strip()
        if not text:
            stats["empty_text"] = int(stats["empty_text"]) + 1
            errors.append(f"line {line_number} ({record.get('audio_path', '')}): text must not be empty")
        audio_rel = str(record.get("audio_path", "")).strip()
        audio_path = (config.dataset.raw_audio_dir / audio_rel).resolve()
        if not audio_rel:
            errors.append(f"line {line_number}: audio_path must not be empty")
            continue
        if config.dataset.raw_audio_dir.resolve() not in audio_path.parents:
            errors.append(f"line {line_number}: audio_path escapes the raw audio directory: {audio_rel}")
            continue
        if not audio_path.exists():
            errors.append(f"line {line_number}: audio file missing: {audio_rel}")
            continue
        if audio_path.suffix.lower() not in config.dataset.allowed_extensions:
            errors.append(
                f"line {line_number}: unsupported audio extension {audio_path.suffix}; "
                f"allowed: {', '.join(config.dataset.allowed_extensions)}"
            )
            continue

        audio_hash = file_sha256(audio_path)
        duplicate = seen_audio_hashes.get(audio_hash)
        if duplicate:
            stats["duplicates"] = int(stats["duplicates"]) + 1
            errors.append(f"line {line_number} ({audio_rel}): duplicate audio content also used by {duplicate}")
        else:
            seen_audio_hashes[audio_hash] = audio_rel

        duration, sample_rate, channels, audio_warnings = inspect_audio(audio_path)
        warnings.extend(f"line {line_number} ({audio_rel}): {warning}" for warning in audio_warnings)
        if duration <= 0 or any("could not" in warning for warning in audio_warnings):
            stats["damaged"] = int(stats["damaged"]) + 1
            errors.append(f"line {line_number} ({audio_rel}): audio cannot be decoded")
            continue
        for key in ("speaker", "emotion", "language", "source"):
            if not str(record.get(key, "")).strip():
                errors.append(f"line {line_number} ({audio_rel}): {key} must not be empty")
        if str(record.get("speaker", "")).strip() != config.speaker:
            errors.append(f"line {line_number} ({audio_rel}): speaker must match training character '{config.speaker}'")
        declared_duration = _optional_float(record.get("duration"))
        if declared_duration is not None and abs(declared_duration - duration) > 0.5:
            warnings.append(
                f"line {line_number} ({audio_rel}): declared duration {declared_duration:.3f}s differs from measured {duration:.3f}s"
            )
        if duration < config.dataset.min_duration_seconds:
            stats["too_short"] = int(stats["too_short"]) + 1
            warnings.append(
                f"line {line_number} ({audio_rel}): skipped audio shorter than {config.dataset.min_duration_seconds:.3f}s: "
                f"{duration:.3f}s"
            )
            continue
        if duration > config.dataset.max_duration_seconds:
            stats["too_long"] = int(stats["too_long"]) + 1
            warnings.append(
                f"line {line_number} ({audio_rel}): skipped audio longer than {config.dataset.max_duration_seconds:.3f}s: "
                f"{duration:.3f}s"
            )
            continue
        if sample_rate is not None and sample_rate != config.dataset.target_sample_rate:
            warnings.append(
                f"line {line_number} ({audio_rel}): sample rate is {sample_rate}, target is {config.dataset.target_sample_rate}; "
                "convert before real training"
            )
        if channels is not None and channels != 1:
            warnings.append(f"line {line_number} ({audio_rel}): audio has {channels} channels; mono is recommended")

        items.append(
            DatasetItem(
                audio_path=audio_rel,
                resolved_audio_path=audio_path,
                text=text,
                speaker=str(record.get("speaker", "")).strip(),
                emotion=str(record.get("emotion", "")).strip().lower() or "neutral",
                language=str(record.get("language", "")).strip().lower() or config.language,
                source=str(record.get("source", "")).strip(),
                duration=duration,
                sample_rate=sample_rate,
                channels=channels,
                audio_hash=audio_hash,
            )
        )

    stats["emotion_counts"] = dict(Counter(item.emotion for item in items))
    stats["average_duration_seconds"] = round(sum(item.duration for item in items) / len(items), 3) if items else 0.0
    validation = _validation(config, items, errors, warnings, stats)
    config.dataset.validation_report.parent.mkdir(parents=True, exist_ok=True)
    config.dataset.validation_report.write_text(
        json.dumps(validation.to_report(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if write_normalized and validation.ready:
        config.dataset.normalized_manifest.parent.mkdir(parents=True, exist_ok=True)
        config.dataset.normalized_manifest.write_text(
            "\n".join(json.dumps(item.to_json_record(), ensure_ascii=False) for item in validation.items) + "\n",
            encoding="utf-8",
        )
        train, holdout = split_items(validation.items, config.dataset.validation_ratio)
        for label, subset in (("train", train), ("validation", holdout)):
            target = config.dataset.normalized_manifest.with_name(f"manifest.{label}.jsonl")
            target.write_text("\n".join(json.dumps(item.to_json_record(), ensure_ascii=False) for item in subset)
                              + ("\n" if subset else ""), encoding="utf-8")
    return validation


def split_items(items: tuple[DatasetItem, ...], ratio: float) -> tuple[tuple[DatasetItem, ...], tuple[DatasetItem, ...]]:
    """Stable holdout split: path/hash ordering does not depend on manifest row order."""
    ordered = sorted(items, key=lambda item: (item.audio_hash, item.audio_path))
    holdout_count = min(len(ordered) - 1, max(1, round(len(ordered) * ratio))) if len(ordered) > 1 else 0
    return tuple(ordered[holdout_count:]), tuple(ordered[:holdout_count])


def inspect_audio(path: Path) -> tuple[float, int | None, int | None, list[str]]:
    if path.suffix.lower() != ".wav":
        ffprobe = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffprobe.exe"
        ffmpeg = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
        if not ffprobe.exists() or not ffmpeg.exists():
            return 0.0, None, None, ["could not decode non-WAV audio without project FFmpeg"]
        probe = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "a:0", "-show_entries",
                                "stream=sample_rate,channels:format=duration", "-of", "json", str(path)],
                               capture_output=True, text=True, check=False)
        decoded = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(path), "-f", "null", "-"],
                                 capture_output=True, text=True, check=False)
        if probe.returncode or decoded.returncode:
            return 0.0, None, None, [f"could not decode audio: {(probe.stderr or decoded.stderr).strip()[:180]}"]
        try:
            data = json.loads(probe.stdout)
            stream = data["streams"][0]
            return float(data["format"]["duration"]), int(stream["sample_rate"]), int(stream["channels"]), []
        except (ValueError, KeyError, IndexError, TypeError):
            return 0.0, None, None, ["could not read audio metadata"]
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            duration = frames / float(sample_rate) if sample_rate else 0.0
            warnings: list[str] = []
            if handle.getsampwidth() not in {2, 4}:
                warnings.append(f"sample width is {handle.getsampwidth()} bytes; 16-bit PCM is recommended")
            if frames == 0:
                warnings.append("could not decode empty WAV")
            elif handle.getsampwidth() in {2, 4}:
                width = handle.getsampwidth()
                peak = 0
                quiet_frames = 0
                inspected_frames = 0
                while True:
                    data = handle.readframes(4096)
                    if not data:
                        break
                    peak = max(peak, audioop.max(data, width))
                    count = len(data) // (width * channels)
                    inspected_frames += count
                    if audioop.rms(data, width) < (2 ** (width * 8 - 1)) * 0.005:
                        quiet_frames += count
                if inspected_frames < frames:
                    warnings.append("could not decode complete WAV data")
                if inspected_frames and quiet_frames / inspected_frames > 0.8:
                    warnings.append(f"silence ratio {quiet_frames / inspected_frames:.1%} exceeds 80%")
                if peak >= (2 ** (width * 8 - 1)) * 0.99:
                    warnings.append("clipping detected near full scale")
            return duration, sample_rate, channels, warnings
    except (wave.Error, EOFError, OSError, ZeroDivisionError) as exc:
        return 0.0, None, None, [f"could not parse WAV header: {exc}"]


def _validation(
    config: TrainingConfig,
    items: list[DatasetItem],
    errors: list[str],
    warnings: list[str],
    stats: dict[str, object] | None = None,
) -> DatasetValidation:
    stats = dict(stats or {})
    train, holdout = split_items(tuple(items), config.dataset.validation_ratio)
    stats["train_samples"] = len(train)
    stats["validation_samples"] = len(holdout)
    hash_values = [
        item.audio_path
        + "|"
        + item.audio_hash
        + "|"
        + item.text
        + "|"
        + item.speaker
        + "|"
        + item.emotion
        + "|"
        + item.language
        for item in items
    ]
    dataset_hash = stable_hash(hash_values) if items else "empty"
    return DatasetValidation(
        ready=not errors and bool(items),
        manifest_path=config.dataset.raw_manifest,
        normalized_manifest=config.dataset.normalized_manifest,
        report_path=config.dataset.validation_report,
        items=tuple(items),
        errors=tuple(errors),
        warnings=tuple(warnings),
        dataset_hash=dataset_hash,
        total_duration_seconds=sum(item.duration for item in items),
        stats=stats,
    )


def _audio_index(config: TrainingConfig, audio_files: list[Path]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for audio_path in audio_files:
        duration, sample_rate, channels, _warnings = inspect_audio(audio_path)
        items.append(
            {
                "audio_path": audio_path.resolve().relative_to(config.dataset.raw_audio_dir.resolve()).as_posix(),
                "resolved_audio_path": str(audio_path.resolve()),
                "audio_hash": file_sha256(audio_path),
                "duration": round(duration, 3),
                "sample_rate": sample_rate,
                "channels": channels,
            }
        )
    return items


def _load_manifest_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            return []
        if not isinstance(record, dict):
            return []
        records.append(record)
    return records


def _manifest_current(existing: list[dict[str, object]], audio_index: list[dict[str, object]]) -> bool:
    if len(existing) != len(audio_index):
        return False
    existing_hashes = sorted(str(record.get("audio_hash", "")) for record in existing)
    current_hashes = sorted(str(record.get("audio_hash", "")) for record in audio_index)
    if not all(existing_hashes) or existing_hashes != current_hashes:
        return False
    return all(str(record.get("text", "")).strip() for record in existing)


def _apply_majority_language(records: list[dict[str, object]], fallback_language: str) -> tuple[str, bool]:
    fallback = _normalize_language(fallback_language) or "zh"
    languages = [_normalize_language(record.get("language")) for record in records]
    counts = Counter(language for language in languages if language and language != "unknown")
    majority = fallback
    if counts:
        max_count = max(counts.values())
        candidates = sorted(language for language, count in counts.items() if count == max_count)
        majority = fallback if fallback in candidates else candidates[0]
    changed = False
    for record in records:
        if _normalize_language(record.get("language")) != majority:
            record["language"] = majority
            changed = True
    return majority, changed


def _normalize_language(value: object) -> str:
    return str(value or "").strip().lower()


def _write_manifest_records(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def _map_sensevoice_emotion(emotion: str) -> str:
    normalized = emotion.strip().lower()
    if normalized == "surprise":
        return "surprised"
    if normalized in {"happy", "sad", "angry", "neutral", "fear", "disgust"}:
        return normalized
    return "neutral"


def _print_human_report(validation: DatasetValidation) -> None:
    status = "ready" if validation.ready else "not ready"
    print(f"GPT-SoVITS dataset is {status}: {_relative(validation.manifest_path)}")
    print(f"records: {len(validation.items)}")
    print(f"duration: {validation.total_duration_seconds:.3f}s")
    print(f"dataset_hash: {validation.dataset_hash}")
    for error in validation.errors:
        print(f"- error: {error}", file=sys.stderr)
    for warning in validation.warnings:
        print(f"- warning: {warning}")


def _cli_report(validation: DatasetValidation) -> dict[str, object]:
    report = validation.to_report()
    errors = list(validation.errors)
    warnings = list(validation.warnings)
    report["error_count"] = len(errors)
    report["warning_count"] = len(warnings)
    report["errors"] = _truncate_messages(errors)
    report["warnings"] = _truncate_messages(warnings)
    return report


def _truncate_messages(messages: list[str], limit: int = 20) -> list[str]:
    if len(messages) <= limit:
        return messages
    remaining = len(messages) - limit
    return [*messages[:limit], f"... {remaining} more; see validation_report.json"]


def _optional_float(value: Any) -> float | None:
    if value in {None, "", "null"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_duration_from_file_size(path: Path) -> float:
    return max(path.stat().st_size / 64000.0, 0.001)


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
