from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from training.qwen3.config import BaseModelConfig, DatasetConfig, GenerationConfig, TeacherConfig, TrainingConfig
from training.qwen3.generate.pipeline import DatasetPaused, build_quote_dataset, normalize_quotes
from training.qwen3.preprocess.validate import validate_files
from training.qwen3.train.train_lora import tokenize_chat_messages


class FakeTeacher:
    name = "fake-local-teacher-v1"

    def json(self, *, task: str, payload: dict[str, object]) -> dict[str, object]:
        if task == "extract_persona":
            return {"personality": ["温柔"], "interaction_style": {"initiative": "high"}, "language_style": {}, "forbidden_tendencies": []}
        if task == "analyze_quote":
            return {"intent": ["comfort"], "emotion": ["gentle"], "response_strategy": ["acknowledge"], "possible_contexts": ["遇到挫折"], "style_features": ["亲近"]}
        if task == "create_daily_scenario":
            quote = payload["analysis"]
            return {"category": "comfort", "user_prompt": f"我遇到挫折了，能陪陪我吗？{quote['quote_id']}", "target_behavior": ["acknowledge"]}
        if task == "create_coverage_scenario":
            return {"user_prompt": f"这是 {payload['category']} 场景的问题。", "target_behavior": ["natural"]}
        if task == "generate_character_response":
            scenario = payload["scenario"]
            return {"assistant_response": f"当然呀，我会陪着你面对 {scenario['category']}。{scenario['scenario_id']}"}
        if task == "judge_character_response":
            return {"persona_score": .95, "relevance_score": .95, "naturalness_score": .95, "behavior_score": .95, "copy_score": .02, "diversity_score": .95, "assistant_like_penalty": .01, "catchphrase_penalty": .01}
        raise AssertionError(task)


def _config(tmp_path: Path, quotes: Path) -> TrainingConfig:
    root = tmp_path / "dataset"
    return TrainingConfig(
        character_id="violet", run_name="quote-run", outputs_dir=tmp_path / "outputs",
        base_model=BaseModelConfig("base", tmp_path / "base.gguf"),
        dataset=DatasetConfig(raw=quotes, quotes=quotes, mode="quotes", normalized=root / "generated" / "normalized_quotes.jsonl", generated_dir=root / "generated", train=root / "final" / "train.jsonl", validation=root / "final" / "validation.jsonl"),
        generator=TeacherConfig(model="fake"), judge=TeacherConfig(model="fake"),
        generation=GenerationConfig(target_samples=2, candidates_per_scene=1, max_regenerate=0, max_near_duplicate_ratio=.92),
    )


def test_quote_to_sft_builds_traceable_final_dataset(tmp_path: Path) -> None:
    quotes = tmp_path / "quotes.txt"
    original = "别急，先慢慢来。\n别急，先慢慢来。\n我会陪着你的。\n"
    quotes.write_text(original, encoding="utf-8")
    config = _config(tmp_path, quotes)

    report = build_quote_dataset(config, system_prompt="你是 Violet。", teacher=FakeTeacher(), judge=FakeTeacher())

    assert quotes.read_text(encoding="utf-8") == original
    assert report["valid_quotes"] == 2
    assert report["rejected_quotes"] == 1
    assert report["category_counts"]["comfort"] >= 1
    generated = config.dataset.generated_dir
    assert all((generated / name).exists() for name in ("normalized_quotes.jsonl", "persona.json", "quote_analysis.jsonl", "scenarios.jsonl", "candidates.jsonl", "filtered.jsonl", "generation_report.json"))
    assert validate_files([config.dataset.train, config.dataset.validation])["total_examples"] >= 2
    record = json.loads(config.dataset.train.read_text(encoding="utf-8").splitlines()[0])
    assert record["metadata"]["source_quote_ids"] or record["metadata"]["source_type"] == "coverage_generated"


def test_model_display_name_does_not_invalidate_the_dataset_cache(tmp_path: Path) -> None:
    quotes = tmp_path / "quotes.txt"
    quotes.write_text("第一句。\n第二句。\n", encoding="utf-8")
    config = _config(tmp_path, quotes)
    build_quote_dataset(config, system_prompt="You are Violet.", teacher=FakeTeacher(), judge=FakeTeacher())
    reused_progress: list[dict] = []

    class UnexpectedTeacher(FakeTeacher):
        def json(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("the cached dataset must not invoke the Teacher")

    report = build_quote_dataset(
        replace(config, model_name="another-visible-model-name"),
        system_prompt="You are Violet.", teacher=UnexpectedTeacher(), judge=UnexpectedTeacher(), progress=reused_progress.append,
    )

    assert report["dataset_input_hash"]
    assert any(item["phase"] == "finalize" for item in reused_progress)


def test_quote_ids_are_stable_when_file_positions_change(tmp_path: Path) -> None:
    source = tmp_path / "quotes.txt"
    source.write_text("第一句。\n第二句。\n", encoding="utf-8")
    first, _ = normalize_quotes(source)
    source.write_text("新增一句。\n第一句。\n第二句。\n", encoding="utf-8")
    second, _ = normalize_quotes(source)
    assert {item.text: item.quote_id for item in first}["第一句。"] == {item.text: item.quote_id for item in second}["第一句。"]


def test_quote_generation_pause_stops_at_next_candidate_and_resumes_from_saved_candidates(tmp_path: Path) -> None:
    quotes = tmp_path / "quotes.txt"
    quotes.write_text("第一句。\n第二句。\n", encoding="utf-8")
    config = _config(tmp_path, quotes)
    pause_file = tmp_path / "pause.request"

    class PauseAfterFirstCandidate(FakeTeacher):
        def __init__(self) -> None:
            self.paused = False

        def json(self, *, task: str, payload: dict[str, object]) -> dict[str, object]:
            value = super().json(task=task, payload=payload)
            if task == "generate_character_response" and not self.paused:
                pause_file.write_text("pause", encoding="utf-8")
                self.paused = True
            return value

    teacher = PauseAfterFirstCandidate()
    with pytest.raises(DatasetPaused):
        build_quote_dataset(config, system_prompt="You are Violet.", teacher=teacher, judge=teacher, pause_file=pause_file)

    generated = config.dataset.generated_dir
    assert json.loads((generated / "candidate_generation_state.json").read_text(encoding="utf-8"))["complete"] is False
    assert len((generated / "candidates.jsonl").read_text(encoding="utf-8").splitlines()) == 1

    pause_file.unlink()
    report = build_quote_dataset(config, system_prompt="You are Violet.", teacher=FakeTeacher(), judge=FakeTeacher())

    assert report["accepted_count"] >= 2
    assert json.loads((generated / "candidate_generation_state.json").read_text(encoding="utf-8"))["complete"] is True


def test_quote_generation_resume_keeps_saved_scenarios(tmp_path: Path) -> None:
    quotes = tmp_path / "quotes.txt"
    quotes.write_text("第一句。\n第二句。\n", encoding="utf-8")
    config = _config(tmp_path, quotes)
    pause_file = tmp_path / "pause.request"

    class PauseAfterFirstScenario(FakeTeacher):
        def __init__(self) -> None:
            self.paused = False

        def json(self, *, task: str, payload: dict[str, object]) -> dict[str, object]:
            value = super().json(task=task, payload=payload)
            if task == "create_daily_scenario" and not self.paused:
                pause_file.write_text("pause", encoding="utf-8")
                self.paused = True
            return value

    teacher = PauseAfterFirstScenario()
    with pytest.raises(DatasetPaused):
        build_quote_dataset(config, system_prompt="You are Violet.", teacher=teacher, judge=teacher, pause_file=pause_file)

    generated = config.dataset.generated_dir
    assert len((generated / "scenarios.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads((generated / "scenario_generation_state.json").read_text(encoding="utf-8"))["complete"] is False

    pause_file.unlink()
    report = build_quote_dataset(config, system_prompt="You are Violet.", teacher=FakeTeacher(), judge=FakeTeacher())

    assert report["accepted_count"] >= 2
    assert json.loads((generated / "scenario_generation_state.json").read_text(encoding="utf-8"))["complete"] is True


class FakeTokenizer:
    pad_token_id = 0

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        result: list[int] = []
        for message in messages:
            result.extend({"system": [1], "user": [2], "assistant": [3]}[message["role"]])
            result.extend([10 + len(part) for part in str(message["content"]).split()])
        if add_generation_prompt:
            result.append(3)
        return result


def test_assistant_only_loss_masks_system_and_user_tokens() -> None:
    result = tokenize_chat_messages(FakeTokenizer(), [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}, {"role": "assistant", "content": "reply words"}], 128, True)
    assert result["labels"][:3] == [-100, -100, -100]
    assert any(value != -100 for value in result["labels"][3:])
