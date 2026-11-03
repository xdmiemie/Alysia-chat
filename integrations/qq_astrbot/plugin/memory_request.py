"""One bounded request copy and one accounting convention for all modules."""
from __future__ import annotations

from copy import deepcopy

from .shared_memory import encoded
from .memory_tasks import InputTooLarge


async def assemble(service, context, payload, *, count_request, count_text, window, output, margin, model):
    base = await count_request(payload)
    if window <= 0 or base + output + margin > window:
        raise InputTooLarge("mandatory_material_exceeds_runtime_window")
    view = await service.build_memory_view(context, count=count_text, available=window-output-margin-base,
        tokenizer_key=str(model))
    # Mandatory material is already represented once; don't also recall it as optional memory.
    mandatory = {r.get("id") for key in ("materials", "candidates", "current") for r in payload.get(key, []) if isinstance(r, dict)}
    pairs = [(item, attribution) for item, attribution in zip(view["items"], view["attribution"]) if item["id"] not in mandatory]
    view["items"], view["attribution"] = [p[0] for p in pairs], [p[1] for p in pairs]
    result = deepcopy(payload)
    while True:
        result["shared_memory"] = {"items": view["items"], "recent": view["recent"], "activity": context.activity, "data_only": True}
        total = await count_request(result)
        if total + output + margin <= window and total-base <= service.config["memory_tokens"]:
            break
        if view["items"]:
            view["items"].pop()
            view["attribution"].pop()
        elif view["recent"]:
            view["recent"], view["common_tokens"] = [], 0
        else:
            result.pop("shared_memory")
            total = base
            break
    usage = dict(model=model, window=window, total_input=total, reserved_output=output,
        memory_budget=view["available"], memory_tokens=max(0, total-base), attribution=view["attribution"],
        common_tokens=view["common_tokens"], wrapper_tokens=total-base-view["common_tokens"]-sum(a["tokens"] for a in view["attribution"]),
        count_method="actual_template_tokenizer", attribution_method="separate_blocks_estimate", purpose=context.purpose)
    return result, usage
