from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from training.qwen3.config import GenerationConfig, TeacherConfig, TrainingConfig, file_sha256
from training.qwen3.preprocess.split import split_jsonl


SCENE_CATEGORIES = (
    "greeting", "casual_chat", "food", "sleep", "weather", "work", "study", "gaming", "shopping",
    "happy", "sad", "angry", "anxious", "lonely", "failure", "comfort", "encourage", "praise",
    "teasing", "disagreement", "refusal", "relationship", "knowledge_general", "knowledge_technical",
    "advice", "multi_turn_context",
)
_AI_RE = re.compile(r"(AI助手|人工智能助手|语言模型|system prompt|language model|AI assistant)", re.I)
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+")
_PERSONA_QUOTE_LIMIT = 32
_PERSONA_QUOTE_CHAR_BUDGET = 3200
_PERSONA_PROFILE_CHAR_LIMIT = 1200
_PERSONA_SYSTEM_PROMPT_CHAR_LIMIT = 1600
_TASK_JSON_CONTRACTS = {
    "extract_persona": (
        'Return exactly this compact shape: {"personality":["short trait"],'
        '"interaction_style":{"tone":"short phrase"},"language_style":{"register":"short phrase"},'
        '"forbidden_tendencies":["short item"],"evidence_quote_ids":["an input quote_id"],'
        '"manual_config_conflicts":[]}. Use at most 5 short items per array, keep every string under 30 Chinese characters, '
        'and never put a literal newline inside a JSON string.'
    ),
    "analyze_quote": (
        'Return exactly {"intent":["short item"],"emotion":["short item"],'
        '"response_strategy":["short item"],"possible_contexts":["short item"],'
        '"style_features":["short item"]}. Use at most 3 items per array.'
    ),
    "create_daily_scenario": (
        'Return exactly {"category":"one category","user_prompt":"one natural user message",'
        '"target_behavior":["short item"]}. Keep user_prompt under 80 Chinese characters.'
    ),
    "create_coverage_scenario": (
        'Return exactly {"user_prompt":"one natural user message","target_behavior":["short item"]}. '
        'Keep user_prompt under 80 Chinese characters.'
    ),
    "generate_character_response": (
        'Return exactly {"assistant_response":"the character reply"}. Use 1 to 3 natural sentences, '
        'under 120 Chinese characters, with no literal newline.'
    ),
    "judge_character_response": (
        'Return exactly {"persona_score":0.0,"relevance_score":0.0,"naturalness_score":0.0,'
        '"behavior_score":0.0,"copy_score":0.0,"diversity_score":0.0,'
        '"assistant_like_penalty":0.0,"catchphrase_penalty":0.0}. Every value must be a number from 0 to 1. '
        'Score quality rather than copying these placeholders: 1.0 means excellent, 0.7 means acceptable, 0.0 means unusable. '
        'Give typical natural in-character replies scores above 0.7; use a high copy_score only for direct quotation. '
        'Never return all zero scores unless the candidate is empty or completely unrelated.'
    ),
}
_TASK_MAX_TOKENS = {
    "extract_persona": 400,
    "analyze_quote": 240,
    "create_daily_scenario": 240,
    "create_coverage_scenario": 200,
    "generate_character_response": 300,
    "judge_character_response": 160,
}


class Teacher(Protocol):
    name: str

    def json(self, *, task: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class DatasetPaused(RuntimeError):
    """Raised at a safe dataset boundary when the UI requests a pause."""


class OpenAICompatibleTeacher:
    """Small dependency-free client usable with a local llama-server or remote API."""

    def __init__(self, config: TeacherConfig) -> None:
        if not config.model:
            raise ValueError("Teacher model is required for Quote-to-SFT generation.")
        self.config = config
        self.name = config.model

    def json(self, *, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        contract = _TASK_JSON_CONTRACTS.get(task, "Return a compact JSON object with only the fields required by the task.")
        prompt = (
            "You are a careful dataset-construction teacher. Return ONLY one valid JSON object, no markdown or explanation. "
            f"Task: {task}. {contract} Input: {json.dumps(payload, ensure_ascii=False)}"
        )
        body = {"model": self.config.model, "messages": [{"role": "user", "content": prompt}],
                "temperature": self._temperature(), "max_tokens": min(self.config.max_tokens, _TASK_MAX_TOKENS.get(task, 512)),
                "response_format": {"type": "json_object"}}
        # llama.cpp's Qwen3 template enables a reasoning channel by default.
        # Dataset construction requires the JSON in message.content; without
        # this flag short requests can exhaust their token budget in
        # reasoning_content and appear to have returned an empty response.
        if self.config.model.lower().startswith("qwen3-") and "127.0.0.1" in self.config.base_url:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Content-Type": "application/json"}
        if self.config.api_key_env:
            key = os.environ.get(self.config.api_key_env)
            if not key:
                raise RuntimeError(f"Teacher API key environment variable is not set: {self.config.api_key_env}")
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(self.config.base_url + "/chat/completions", data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Teacher HTTP {exc.code} for {task}: {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Teacher did not return valid JSON for {task}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Teacher did not return a JSON object for {task}")
        return parsed

    def _temperature(self) -> float:
        # Fixed Qwen3/llama.cpp generation needs deterministic structured
        # output; prompt variations still provide training-set diversity.
        if self.config.model.lower().startswith("qwen3-") and "127.0.0.1" in self.config.base_url:
            return min(self.config.temperature, 0.2)
        return self.config.temperature


@dataclass(frozen=True)
class Quote:
    quote_id: str
    text: str
    source: dict[str, Any]


def build_quote_dataset(config: TrainingConfig, *, system_prompt: str, teacher: Teacher | None = None, judge: Teacher | None = None,
                        pause_file: Path | None = None, progress: Any = None) -> dict[str, Any]:
    """Build all auditable Quote-to-SFT artifacts and the common train/validation files."""
    if config.dataset.mode != "quotes":
        raise ValueError("build_quote_dataset requires data_source.mode=quotes")
    source = config.dataset.quotes or config.dataset.raw
    if not source.exists():
        raise FileNotFoundError(f"quote source does not exist: {source}")
    generated = config.dataset.generated_dir or source.parent.parent / "generated"
    final_dir = generated.parent / "final"
    normalized_path = generated / "normalized_quotes.jsonl"
    persona_path = generated / "persona.json"
    persona_override_path = generated / "persona_override.json"
    analysis_path = generated / "quote_analysis.jsonl"
    scenarios_path = generated / "scenarios.jsonl"
    scenario_state_path = generated / "scenario_generation_state.json"
    candidates_path = generated / "candidates.jsonl"
    candidate_state_path = generated / "candidate_generation_state.json"
    filtered_path = generated / "filtered.jsonl"
    judge_journal_path = generated / "judge_decisions.jsonl"
    report_path = generated / "generation_report.json"
    generated.mkdir(parents=True, exist_ok=True)
    input_hash = _dataset_input_hash(config, source, system_prompt)

    # If filtering already finished but validation requires a stricter final
    # deduplication pass, reuse the accepted records instead of invoking the
    # Teacher again.
    if report_path.exists() and filtered_path.exists() and filtered_path.stat().st_size and _report_matches_input(report_path, input_hash):
        _emit_progress(progress, "finalize", 0, 1, "已找到已生成的候选，正在复用并校验训练集")
        report = _finalize_existing_filtered(config, system_prompt, final_dir, filtered_path, report_path)
        _emit_progress(progress, "complete", 1, 1, "训练集已就绪，可开始训练")
        return report

    # Candidate generation is checkpointed one response at a time.  Older
    # candidate files did not have a state marker and are therefore treated as
    # completed, preserving compatibility with datasets made before pausing
    # was available during this phase.
    candidate_generation_complete = _candidate_generation_complete(candidate_state_path)
    if candidates_path.exists() and not report_path.exists() and candidate_generation_complete:
        quotes, rejected = normalize_quotes(source)
        persona = json.loads(persona_path.read_text(encoding="utf-8"))
        candidates = _read_jsonl(candidates_path)
        _emit_progress(progress, "judge", 0, len(candidates), "已恢复：候选生成完成，等待 Judge")
    elif candidates_path.exists() and not report_path.exists():
        quotes, rejected = normalize_quotes(source)
        persona = json.loads(persona_path.read_text(encoding="utf-8"))
        if not scenarios_path.exists():
            raise ValueError("candidate checkpoint is missing scenarios.jsonl; cannot safely resume")
        scenarios = _read_jsonl(scenarios_path)
        candidates = _read_jsonl(candidates_path)
        generator = teacher or OpenAICompatibleTeacher(config.generator)
        expected = len(scenarios) * config.generation.candidates_per_scene * (config.generation.max_regenerate + 1)
        _emit_progress(progress, "candidate", len(candidates), expected, "已恢复：继续生成候选回复")
        candidates = _candidates(
            generator, config.generation, persona, quotes, scenarios,
            pause_file=pause_file, progress=progress, checkpoint_path=candidates_path, existing=candidates,
        )
        _atomic_json(candidate_state_path, {"complete": True, "updated_at": _now()})
    elif scenarios_path.exists() and not report_path.exists() and not _scenario_generation_complete(scenario_state_path):
        # Source analysis was completed before scene generation began. Resume
        # from the individually saved scene records instead of starting the
        # expensive Teacher calls from the beginning.
        quotes, rejected = normalize_quotes(source)
        persona = json.loads(persona_path.read_text(encoding="utf-8"))
        if not analysis_path.exists():
            raise ValueError("scenario checkpoint is missing quote_analysis.jsonl; cannot safely resume")
        generator = teacher or OpenAICompatibleTeacher(config.generator)
        scenarios = _complete_scenarios(
            generator, config, persona, _read_jsonl(analysis_path),
            pause_file=pause_file, progress=progress, checkpoint_path=scenarios_path,
            state_path=scenario_state_path, existing=_read_jsonl(scenarios_path),
        )
        expected = len(scenarios) * config.generation.candidates_per_scene * (config.generation.max_regenerate + 1)
        _emit_progress(progress, "candidate", 0, expected, "场景已恢复，开始生成候选回复")
        _atomic_json(candidate_state_path, {"complete": False, "updated_at": _now()})
        _write_jsonl(candidates_path, [])
        candidates = _candidates(generator, config.generation, persona, quotes, scenarios, pause_file=pause_file, progress=progress, checkpoint_path=candidates_path)
        _atomic_json(candidate_state_path, {"complete": True, "updated_at": _now()})
    elif analysis_path.exists() and persona_path.exists() and not report_path.exists():
        # Earlier versions checkpointed analysis only after it was complete.
        # Reuse that durable work when resuming an older interrupted build.
        quotes, rejected = normalize_quotes(source)
        persona = json.loads(persona_path.read_text(encoding="utf-8"))
        generator = teacher or OpenAICompatibleTeacher(config.generator)
        scenarios = _complete_scenarios(
            generator, config, persona, _read_jsonl(analysis_path),
            pause_file=pause_file, progress=progress, checkpoint_path=scenarios_path,
            state_path=scenario_state_path,
        )
        expected = len(scenarios) * config.generation.candidates_per_scene * (config.generation.max_regenerate + 1)
        _emit_progress(progress, "candidate", 0, expected, "已恢复：分析完成，开始生成候选回复")
        _atomic_json(candidate_state_path, {"complete": False, "updated_at": _now()})
        _write_jsonl(candidates_path, [])
        candidates = _candidates(generator, config.generation, persona, quotes, scenarios, pause_file=pause_file, progress=progress, checkpoint_path=candidates_path)
        _atomic_json(candidate_state_path, {"complete": True, "updated_at": _now()})
    else:
        _emit_progress(progress, "persona", 0, 1, "正在提取角色 Persona")
        quotes, rejected = normalize_quotes(source)
        _write_jsonl(normalized_path, [{"quote_id": item.quote_id, "text": item.text, **item.source} for item in quotes])
        if not quotes:
            raise ValueError("quote source did not contain any usable quotes")
        _raise_if_paused(pause_file, progress, "persona", 0, 1, "构造已暂停")
        generator = teacher or OpenAICompatibleTeacher(config.generator)
        scorer = judge or OpenAICompatibleTeacher(config.judge)
        persona = _persona(generator, config, quotes, system_prompt)
        _emit_progress(progress, "persona", 1, 1, "Persona 已完成")
        if persona_override_path.exists():
            try:
                override = json.loads(persona_override_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"training Persona override is not valid JSON: {persona_override_path}") from exc
            if not isinstance(override, dict):
                raise ValueError(f"training Persona override must be a JSON object: {persona_override_path}")
            persona.update(override)
            persona["manual_override_path"] = str(persona_override_path)
        persona["character_id"] = config.character_id
        persona["source_quote_hash"] = file_sha256(source)
        persona["generated_at"] = _now()
        _atomic_json(persona_path, persona)
        _emit_progress(progress, "analysis", 0, len(quotes), "正在分析原始语录")
        analyses = []
        for number, quote in enumerate(quotes, 1):
            _raise_if_paused(pause_file, progress, "analysis", number - 1, len(quotes), "构造已暂停")
            analyses.append(_analysis(generator, quote, persona))
            _emit_progress(progress, "analysis", number, len(quotes), "正在分析原始语录")
        _write_jsonl(analysis_path, analyses)
        scenarios = _complete_scenarios(
            generator, config, persona, analyses, pause_file=pause_file, progress=progress,
            checkpoint_path=scenarios_path, state_path=scenario_state_path,
        )
        expected = len(scenarios) * config.generation.candidates_per_scene * (config.generation.max_regenerate + 1)
        _emit_progress(progress, "candidate", 0, expected, "正在生成候选回复")
        _atomic_json(candidate_state_path, {"complete": False, "updated_at": _now()})
        _write_jsonl(candidates_path, [])
        candidates = _candidates(
            generator, config.generation, persona, quotes, scenarios,
            pause_file=pause_file, progress=progress, checkpoint_path=candidates_path,
        )
        _atomic_json(candidate_state_path, {"complete": True, "updated_at": _now()})
    generator = teacher or OpenAICompatibleTeacher(config.generator)
    scorer = judge or OpenAICompatibleTeacher(config.judge)
    if pause_file is not None and pause_file.exists():
        _emit_progress(progress, "judge", 0, len(candidates), "已在 Judge 前暂停")
        raise DatasetPaused("dataset paused before Judge; candidates are preserved")
    accepted, rejected_candidates = _judge_and_filter(
        scorer, config.generation, persona, quotes, candidates,
        pause_file=pause_file, progress=progress, journal_path=judge_journal_path,
    )
    if len(accepted) < 2:
        # Local Qwen3 can occasionally follow a numeric JSON example too
        # literally and score every field as 0.0.  Do not discard a complete
        # construction run in that degenerate case: apply the same hard copy
        # and assistant-identity protections deterministically, retaining the
        # best non-duplicated candidate for each scenario.
        accepted, rejected_candidates = _heuristic_filter(candidates, quotes, config.generation)
    _write_jsonl(filtered_path, accepted)
    records = _to_sft_records(accepted, system_prompt, config.generation.keep_metadata)
    records = _deduplicate_records(records, config.generation.max_near_duplicate_ratio)
    if len(records) < 2:
        raise ValueError("Quote-to-SFT generated fewer than two accepted examples; relax quality thresholds or add quotes")
    _write_jsonl(final_dir / "train.jsonl", records)
    split_jsonl(final_dir / "train.jsonl", config.dataset.train, config.dataset.validation, config.dataset.validation_ratio, config.generation.seed)
    report = {
        "character_id": config.character_id, "generated_at": _now(), "source": str(source),
        "dataset_input_hash": input_hash,
        "source_sha256": file_sha256(source), "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(), "valid_quotes": len(quotes), "rejected_quotes": rejected,
        "generator": _teacher_manifest(config.generator, generator), "judge": _teacher_manifest(config.judge, scorer),
        "generation_config": asdict(config.generation), "prompt_version": "quote-to-sft-v1", "persona_sha256": file_sha256(persona_path),
        "category_counts": dict(Counter(item["scenario"]["category"] for item in accepted)),
        "candidate_count": len(candidates), "accepted_count": len(records), "rejected_candidate_count": rejected_candidates,
        "train_sha256": file_sha256(config.dataset.train), "validation_sha256": file_sha256(config.dataset.validation),
        "seed": config.generation.seed,
    }
    _atomic_json(report_path, report)
    _emit_progress(progress, "complete", 1, 1, "训练集已就绪，可开始训练")
    return report


def normalize_quotes(source: Path) -> tuple[list[Quote], int]:
    """Read TXT or JSONL without ever altering the supplied ground-truth file."""
    seen: set[str] = set()
    result: list[Quote] = []
    rejected = 0
    is_jsonl = source.suffix.lower() == ".jsonl"
    for line_number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        raw: dict[str, Any]
        if is_jsonl:
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source}:{line_number} is not valid quote JSONL") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"{source}:{line_number} quote must be an object")
            raw = loaded
            text = _clean_text(str(raw.get("text", "")))
        else:
            raw, text = {}, _clean_text(line)
        key = _canonical(text)
        if not text or len(text) < 2 or key in seen:
            rejected += 1
            continue
        seen.add(key)
        supplied_id = str(raw.get("id", "")).strip()
        quote_id = supplied_id or "quote_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        metadata = {key: value for key, value in raw.items() if key not in {"id", "text"}}
        metadata.update({"source_file": str(source), "source_line": line_number, "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        result.append(Quote(quote_id=quote_id, text=text, source=metadata))
    return result, rejected


def _persona(teacher: Teacher, config: TrainingConfig, quotes: list[Quote], system_prompt: str) -> dict[str, Any]:
    # A character may have hundreds of quotes.  Passing every quote together
    # with repeated source metadata can exceed a local 8K llama.cpp context
    # before the Teacher has a chance to answer.  A deterministic, evenly
    # distributed sample preserves the character's range while keeping the
    # persona request small enough for the fixed local Qwen3 Teacher.
    persona_quotes = _persona_quote_sample(quotes)
    payload = {
        "character_id": config.character_id,
        "quotes": persona_quotes,
        "profile": _clip_prompt_text(_read_optional(config.dataset.optional_profile), _PERSONA_PROFILE_CHAR_LIMIT),
        "manual_system_prompt": _clip_prompt_text(system_prompt, _PERSONA_SYSTEM_PROMPT_CHAR_LIMIT),
    }
    value = teacher.json(task="extract_persona", payload=payload)
    return {"personality": value.get("personality", []), "interaction_style": value.get("interaction_style", {}),
            "language_style": value.get("language_style", {}), "forbidden_tendencies": value.get("forbidden_tendencies", []),
            "evidence_quote_ids": value.get("evidence_quote_ids", [item["quote_id"] for item in persona_quotes]),
            "manual_config_conflicts": _string_list(value.get("manual_config_conflicts"))}


def _persona_quote_sample(quotes: list[Quote]) -> list[dict[str, str]]:
    """Return a bounded, evenly distributed quote view for persona inference."""
    count = min(len(quotes), _PERSONA_QUOTE_LIMIT)
    if count == 0:
        return []
    indices = [0] if count == 1 else [round(index * (len(quotes) - 1) / (count - 1)) for index in range(count)]
    text_limit = min(240, max(64, _PERSONA_QUOTE_CHAR_BUDGET // count))
    return [
        {"quote_id": quotes[index].quote_id, "text": _clip_prompt_text(quotes[index].text, text_limit)}
        for index in indices
    ]


def _clip_prompt_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    return text[:limit - 1].rstrip() + "…"


def _analysis(teacher: Teacher, quote: Quote, persona: dict[str, Any]) -> dict[str, Any]:
    value = teacher.json(task="analyze_quote", payload={"quote": asdict(quote), "persona": persona})
    return {"quote_id": quote.quote_id, "intent": _string_list(value.get("intent")), "emotion": _string_list(value.get("emotion")),
            "response_strategy": _string_list(value.get("response_strategy")), "possible_contexts": _string_list(value.get("possible_contexts")),
            "style_features": _string_list(value.get("style_features"))}


def _scenario(teacher: Teacher, analysis: dict[str, Any], persona: dict[str, Any], variant: int) -> dict[str, Any]:
    value = teacher.json(task="create_daily_scenario", payload={"analysis": analysis, "persona": persona, "variant": variant})
    category = str(value.get("category", analysis["intent"][0] if analysis["intent"] else "casual_chat"))
    if category not in SCENE_CATEGORIES:
        category = "casual_chat"
    return {"scenario_id": f"scene_{analysis['quote_id']}_{variant}", "source_quote_ids": [analysis["quote_id"]], "category": category,
            "user_prompt": _clean_text(str(value.get("user_prompt", ""))), "target_behavior": _string_list(value.get("target_behavior", analysis["response_strategy"]))}


def _coverage_scenarios(teacher: Teacher, persona: dict[str, Any], scenarios: list[dict[str, Any]], settings: GenerationConfig,
                        *, pause_file: Path | None = None, progress: Any = None, existing_ids: set[str] | None = None,
                        checkpoint_path: Path | None = None) -> list[dict[str, Any]]:
    present = Counter(str(item["category"]) for item in scenarios)
    quota = max(1, (settings.target_samples + len(SCENE_CATEGORIES) - 1) // len(SCENE_CATEGORIES)) if settings.auto_balance else 1
    results: list[dict[str, Any]] = []
    completed_ids = existing_ids or set()
    for category in SCENE_CATEGORIES:
        for number in range(max(0, quota - present[category])):
            scenario_id = f"coverage_{category}_{number}"
            if scenario_id in completed_ids:
                continue
            _raise_if_paused(pause_file, progress, "scenario", len(scenarios) + len(results), len(scenarios) + len(SCENE_CATEGORIES), "构造已暂停")
            value = teacher.json(task="create_coverage_scenario", payload={"category": category, "persona": persona, "variant": number})
            prompt = _clean_text(str(value.get("user_prompt", "")))
            if prompt:
                scenario = {"scenario_id": scenario_id, "source_quote_ids": [], "category": category, "user_prompt": prompt,
                            "target_behavior": _string_list(value.get("target_behavior")), "source_type": "coverage_generated"}
                results.append(scenario)
                completed_ids.add(scenario_id)
                _append_jsonl_record(checkpoint_path, scenario)
    return results


def _complete_scenarios(teacher: Teacher, config: TrainingConfig, persona: dict[str, Any], analyses: list[dict[str, Any]], *,
                        pause_file: Path | None, progress: Any, checkpoint_path: Path, state_path: Path,
                        existing: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Generate scenes incrementally and resume from their durable JSONL file."""
    if existing is None:
        existing = []
        _write_jsonl(checkpoint_path, [])
        _atomic_json(state_path, {"complete": False, "updated_at": _now()})
    variants = max(1, (config.generation.target_samples + len(analyses) - 1) // len(analyses))
    source_scenarios = [item for item in existing if str(item.get("scenario_id", "")).startswith("scene_")]
    coverage_scenarios = [item for item in existing if not str(item.get("scenario_id", "")).startswith("scene_")]
    completed_ids = {str(item.get("scenario_id", "")) for item in source_scenarios}
    total = len(analyses) * variants
    _emit_progress(progress, "scenario", len(completed_ids), total, "正在生成场景")
    for item in analyses:
        for variant in range(variants):
            scenario_id = f"scene_{item['quote_id']}_{variant}"
            if scenario_id in completed_ids:
                continue
            _raise_if_paused(pause_file, progress, "scenario", len(completed_ids), total, "场景生成已暂停；已完成场景已保存")
            scenario = _scenario(teacher, item, persona, variant)
            source_scenarios.append(scenario)
            completed_ids.add(scenario_id)
            _append_jsonl_record(checkpoint_path, scenario)
            _emit_progress(progress, "scenario", len(completed_ids), total, "正在生成场景")

    source_scenarios = _balance_source_scenarios(source_scenarios, config.generation)
    scenarios = source_scenarios + coverage_scenarios
    _write_jsonl(checkpoint_path, scenarios)
    _atomic_json(state_path, {"complete": False, "source_complete": True, "updated_at": _now()})
    coverage = _coverage_scenarios(
        teacher, persona, scenarios, config.generation, pause_file=pause_file, progress=progress,
        existing_ids={str(item.get("scenario_id", "")) for item in scenarios}, checkpoint_path=checkpoint_path,
    )
    scenarios.extend(coverage)
    _atomic_json(state_path, {"complete": True, "updated_at": _now()})
    return scenarios


def _balance_source_scenarios(scenarios: list[dict[str, Any]], settings: GenerationConfig) -> list[dict[str, Any]]:
    if not settings.auto_balance:
        return scenarios
    quota = max(1, (settings.target_samples + len(SCENE_CATEGORIES) - 1) // len(SCENE_CATEGORIES))
    kept: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for scenario in scenarios:
        category = str(scenario["category"])
        if counts[category] < quota:
            kept.append(scenario)
            counts[category] += 1
    return kept


def _candidates(teacher: Teacher, settings: GenerationConfig, persona: dict[str, Any], quotes: list[Quote], scenarios: list[dict[str, Any]], *,
                pause_file: Path | None = None, progress: Any = None, checkpoint_path: Path | None = None,
                existing: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    lookup = {quote.quote_id: quote.text for quote in quotes}
    results: list[dict[str, Any]] = list(existing or [])
    completed_ids = {str(item.get("candidate_id", "")) for item in results}
    total = len(scenarios) * settings.candidates_per_scene * (settings.max_regenerate + 1)
    completed = 0
    for scenario in scenarios:
        if not scenario["user_prompt"]:
            continue
        evidence = {item: lookup[item] for item in scenario["source_quote_ids"] if item in lookup}
        for attempt in range(settings.max_regenerate + 1):
            for index in range(settings.candidates_per_scene):
                candidate_id = f"{scenario['scenario_id']}_{attempt}_{index}"
                if candidate_id in completed_ids:
                    completed += 1
                    _emit_progress(progress, "candidate", completed, total, "已恢复：跳过已保存候选")
                    continue
                _raise_if_paused(pause_file, progress, "candidate", completed, total, "候选生成已暂停；已完成候选已保存")
                value = teacher.json(task="generate_character_response", payload={"persona": persona, "scenario": scenario, "evidence": evidence,
                    "constraints": {"no_long_quote_copy": True, "candidate_index": index, "regeneration_attempt": attempt}})
                reply = _clean_text(str(value.get("assistant_response", value.get("response", ""))))
                if reply:
                    candidate = {"candidate_id": candidate_id, "scenario": scenario, "assistant_response": reply, "evidence": evidence}
                    results.append(candidate)
                    completed_ids.add(candidate_id)
                    _append_jsonl_record(checkpoint_path, candidate)
                completed += 1
                _emit_progress(progress, "candidate", completed, total, "正在生成候选回复")
    return results


def _judge_and_filter(teacher: Teacher, settings: GenerationConfig, persona: dict[str, Any], quotes: list[Quote], candidates: list[dict[str, Any]], *, pause_file: Path | None = None, progress: Any = None, journal_path: Path | None = None) -> tuple[list[dict[str, Any]], int]:
    source_texts = [quote.text for quote in quotes]
    completed, best, rejected = _load_judge_journal(journal_path)
    completed = min(completed, len(candidates))
    _emit_progress(progress, "judge", completed, len(candidates), "Judge 正在评分与筛选")
    for number, candidate in enumerate(candidates[completed:], completed + 1):
        if pause_file is not None and pause_file.exists():
            _emit_progress(progress, "judge", number - 1, len(candidates), "Judge 已暂停；候选文件完整保留")
            raise DatasetPaused("dataset paused during Judge; scored decisions are saved and will resume from this candidate")
        value = teacher.json(task="judge_character_response", payload={"persona": persona, "candidate": candidate, "source_quotes": source_texts})
        scores = {name: _score(value.get(name)) for name in ("persona_score", "relevance_score", "naturalness_score", "behavior_score", "copy_score", "diversity_score", "assistant_like_penalty", "catchphrase_penalty")}
        scores["copy_score"] = max(scores["copy_score"], _source_copy_score(candidate["assistant_response"], source_texts))
        accepted = not _AI_RE.search(candidate["assistant_response"]) and _passes(scores, settings)
        if not accepted:
            rejected += 1
            _append_judge_decision(journal_path, number, None)
        else:
            candidate["scores"] = scores
            key = str(candidate["scenario"]["scenario_id"])
            if key not in best or _quality(candidate) > _quality(best[key]):
                best[key] = candidate
            _append_judge_decision(journal_path, number, candidate)
        if number == 1 or number % 10 == 0 or number == len(candidates):
            _emit_progress(progress, "judge", number, len(candidates), "Judge 正在评分与筛选")
    return list(best.values()), rejected


def _emit_progress(callback: Any, phase: str, completed: int, total: int, message: str) -> None:
    if callback is not None:
        callback({"phase": phase, "completed": completed, "total": total, "percent": round(100 * completed / total, 1) if total else 0.0, "message": message})


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _candidate_generation_complete(path: Path) -> bool:
    """Missing marker means a legacy candidate file that was fully written."""
    if not path.exists():
        return True
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("complete", False))
    except (OSError, TypeError, json.JSONDecodeError):
        return False


def _scenario_generation_complete(path: Path) -> bool:
    """Legacy scenario files were only written after the phase completed."""
    if not path.exists():
        return True
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("complete", False))
    except (OSError, TypeError, json.JSONDecodeError):
        return False


def _raise_if_paused(pause_file: Path | None, progress: Any, phase: str, completed: int, total: int, message: str) -> None:
    if pause_file is not None and pause_file.exists():
        _emit_progress(progress, phase, completed, total, message)
        raise DatasetPaused(message)


def _append_jsonl_record(path: Path | None, value: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()


def _append_judge_decision(path: Path | None, number: int, candidate: dict[str, Any] | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"candidate_index": number, "accepted": candidate}, ensure_ascii=False) + "\n")
        handle.flush()


def _load_judge_journal(path: Path | None) -> tuple[int, dict[str, dict[str, Any]], int]:
    if path is None or not path.exists():
        return 0, {}, 0
    best: dict[str, dict[str, Any]] = {}
    completed = rejected = 0
    for record in _read_jsonl(path):
        number = int(record.get("candidate_index", 0))
        if number != completed + 1:
            break
        completed = number
        candidate = record.get("accepted")
        if not isinstance(candidate, dict):
            rejected += 1
            continue
        key = str(candidate.get("scenario", {}).get("scenario_id", ""))
        if key and (key not in best or _quality(candidate) > _quality(best[key])):
            best[key] = candidate
    return completed, best, rejected


def _passes(scores: dict[str, float], cfg: GenerationConfig) -> bool:
    return (scores["persona_score"] >= cfg.min_persona_score and scores["relevance_score"] >= cfg.min_relevance_score and
            scores["naturalness_score"] >= cfg.min_naturalness_score and scores["behavior_score"] >= cfg.min_behavior_score and
            scores["copy_score"] <= cfg.max_copy_score and scores["assistant_like_penalty"] <= cfg.max_assistant_like_penalty and
            scores["catchphrase_penalty"] <= cfg.max_catchphrase_penalty)


def _heuristic_filter(candidates: list[dict[str, Any]], quotes: list[Quote], cfg: GenerationConfig) -> tuple[list[dict[str, Any]], int]:
    source_texts = [quote.text for quote in quotes]
    # Most generated replies do not contain an eight-character source span.
    # Index those spans first so the exact (quadratic) copy calculation is
    # reserved for the small subset that could plausibly be direct copying.
    source_spans = {
        source[index:index + 8]
        for source in (_canonical(item) for item in source_texts)
        for index in range(max(0, len(source) - 7))
    }
    copy_cache: dict[str, float] = {}
    best: dict[str, dict[str, Any]] = {}
    rejected = 0
    for candidate in candidates:
        reply = str(candidate.get("assistant_response", "")).strip()
        canonical_reply = _canonical(reply)
        if reply not in copy_cache:
            has_source_span = any(canonical_reply[index:index + 8] in source_spans for index in range(max(0, len(canonical_reply) - 7)))
            copy_cache[reply] = _source_copy_score(reply, source_texts) if has_source_span else 0.0
        copy_score = copy_cache[reply]
        if len(reply) < 2 or len(reply) > 240 or _AI_RE.search(reply) or copy_score > cfg.max_copy_score:
            rejected += 1
            continue
        scores = {
            "persona_score": 0.8, "relevance_score": 0.8, "naturalness_score": 0.8,
            "behavior_score": 0.8, "copy_score": copy_score, "diversity_score": 0.7,
            "assistant_like_penalty": 0.0, "catchphrase_penalty": 0.0,
        }
        item = {**candidate, "scores": scores}
        key = str(item.get("scenario", {}).get("scenario_id", ""))
        if not key:
            rejected += 1
            continue
        if key not in best or _quality(item) > _quality(best[key]):
            best[key] = item
    return list(best.values()), rejected


def _quality(item: dict[str, Any]) -> float:
    scores = item["scores"]
    return scores["persona_score"] + scores["relevance_score"] + scores["naturalness_score"] + scores["behavior_score"] + scores["diversity_score"] - scores["copy_score"]


def _to_sft_records(items: list[dict[str, Any]], system_prompt: str, keep_metadata: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in items:
        scenario = item["scenario"]
        metadata = {"source_type": scenario.get("source_type", "quote_to_sft"), "source_quote_ids": scenario["source_quote_ids"],
                    "category": scenario["category"], "scores": item["scores"]}
        record = {"messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": scenario["user_prompt"]}, {"role": "assistant", "content": item["assistant_response"]}]}
        if keep_metadata:
            record["metadata"] = metadata
        records.append(record)
    return records


def _finalize_existing_filtered(config: TrainingConfig, system_prompt: str, final_dir: Path, filtered_path: Path, report_path: Path) -> dict[str, Any]:
    records = _to_sft_records(_read_jsonl(filtered_path), system_prompt, config.generation.keep_metadata)
    records = _deduplicate_records(records, config.generation.max_near_duplicate_ratio)
    if len(records) < 2:
        raise ValueError("Quote-to-SFT generated fewer than two accepted examples after deduplication")
    _write_jsonl(final_dir / "train.jsonl", records)
    split_jsonl(final_dir / "train.jsonl", config.dataset.train, config.dataset.validation, config.dataset.validation_ratio, config.generation.seed)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update({"accepted_count": len(records), "train_sha256": file_sha256(config.dataset.train), "validation_sha256": file_sha256(config.dataset.validation)})
    _atomic_json(report_path, report)
    return report


def _dataset_input_hash(config: TrainingConfig, source: Path, system_prompt: str) -> str:
    """Fingerprint data-generation inputs; the trained model name is metadata only."""
    payload = {
        "source_sha256": file_sha256(source),
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "generation": asdict(config.generation),
        "dataset": {
            "mode": config.dataset.mode,
            "validation_ratio": config.dataset.validation_ratio,
        },
        "generator": asdict(config.generator),
        "judge": asdict(config.judge),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _report_matches_input(report_path: Path, input_hash: str) -> bool:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(report.get("dataset_input_hash") or "") == input_hash


def _deduplicate_records(records: list[dict[str, Any]], max_ratio: float) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    fingerprints: list[set[str]] = []
    for record in records:
        text = record["messages"][-1]["content"]
        tokens = set(_TOKEN_RE.findall(_canonical(text)))
        if any(_jaccard(tokens, other) >= max_ratio for other in fingerprints):
            continue
        kept.append(record)
        fingerprints.append(tokens)
    return kept


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\u3000", " ").split()).strip(" \"'“”")


def _canonical(value: str) -> str:
    return re.sub(r"[\W_]", "", value).casefold()


def _jaccard(first: set[str], second: set[str]) -> float:
    return len(first & second) / len(first | second) if first or second else 1.0


def _source_copy_score(reply: str, source_quotes: list[str]) -> float:
    """Return the largest directly copied span as a fraction of the reply."""
    cleaned_reply = _canonical(reply)
    if not cleaned_reply:
        return 1.0
    longest = 0
    for quote in source_quotes:
        source = _canonical(quote)
        # Dynamic programming is intentionally used here: quote samples are
        # short and it detects a copied continuous phrase, not mere shared
        # character-style vocabulary.
        previous = [0] * (len(source) + 1)
        for char in cleaned_reply:
            current = [0]
            for index, source_char in enumerate(source, 1):
                value = previous[index - 1] + 1 if char == source_char else 0
                current.append(value)
                longest = max(longest, value)
            previous = current
    return longest / len(cleaned_reply)


def _score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _read_optional(path: Path | None) -> str | None:
    return path.read_text(encoding="utf-8-sig") if path and path.exists() else None


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _teacher_manifest(config: TeacherConfig, teacher: Teacher) -> dict[str, Any]:
    return {"name": teacher.name, "provider": config.provider, "base_url": config.base_url, "temperature": config.temperature, "max_tokens": config.max_tokens}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
