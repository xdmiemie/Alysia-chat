from __future__ import annotations

import json
import math

import jsonschema

from integrations.qq_astrbot.plugin.persistent_memory import data_safe
from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
from integrations.qq_astrbot.plugin.vision_analysis import grammar_schema

FLAGS = ("advertisement", "spam", "misinformation", "sensitive", "private_information")
SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    **{key: {"type": "boolean"} for key in ("interesting", "funny", "high_quality", *FLAGS)},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "reasons": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
    "uncertainty": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 300}}},
    "required": ["interesting", "funny", "high_quality", *FLAGS, "confidence", "reasons", "uncertainty"]}
FORMAT = {"type": "json_schema", "json_schema": {"name": "douyin_interest", "strict": True, "schema": grammar_schema(SCHEMA)}}
TITLE_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"interested": {"type": "boolean"}, "reason": {"type": "string", "minLength": 1, "maxLength": 160}},
    "required": ["interested", "reason"]}
TITLE_FORMAT = {"type": "json_schema", "json_schema": {"name": "douyin_title_interest", "strict": True,
    "schema": grammar_schema(TITLE_SCHEMA)}}


async def title_interest(model, title, persona):
    raw = await model.complete([
        {"role": "system", "content": persona + "\n" + load_prompts()["douyin_title_interest"]},
        {"role": "user", "content": json.dumps({"title": title}, ensure_ascii=False)}],
        TITLE_FORMAT, phase="douyin_title_interest")
    value = json.loads(raw) if isinstance(raw, str) else raw
    jsonschema.validate(value, TITLE_SCHEMA)
    return value


def validate_interest(raw):
    value = json.loads(raw) if isinstance(raw, str) else raw
    jsonschema.validate(value, SCHEMA)
    if not math.isfinite(value["confidence"]):
        raise ValueError("invalid_interest_confidence")
    return value


def acceptable(analysis, interest):
    return (analysis.get("confidence", 0) >= 0.7 and interest["confidence"] >= 0.7
        and not analysis.get("uncovered_intervals") and not any(interest[k] for k in FLAGS)
        and data_safe(analysis.get("summary", "")) and bool(analysis.get("summary", "").strip()))


def recommend(analysis, interest):
    return acceptable(analysis, interest) and any(interest[k] for k in ("interesting", "funny", "high_quality"))


async def score(model, analysis, title, persona):
    return validate_interest(await model.complete([
        {"role": "system", "content": persona + "\n" + load_prompts()["douyin_interest"]},
        {"role": "user", "content": json.dumps({"title": title, "analysis": analysis}, ensure_ascii=False)}], FORMAT))
