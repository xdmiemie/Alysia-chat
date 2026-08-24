# Qwen3 Role Dataset Format

Stage 7 defaults to **Quote-to-SFT**: add a UTF-8 `raw/quotes.txt` (one source
quote per line) or a `raw/quotes.jsonl` (`text` required; `id`, `source`,
`context`, `emotion` and `tags` optional). The source stays read-only. The
pipeline writes auditable intermediate data to `generated/` and standard Chat
JSONL to `final/train.jsonl` and `final/validation.jsonl`.

An already prepared Chat JSONL source remains supported with
`data_source.mode: chat_jsonl`. Both sources converge on the same final format:

```json
{"messages":[{"role":"system","content":"..."},{"role":"user","content":"..."},{"role":"assistant","content":"..."}]}
```

For Elysia, the source file is stored at `training/qwen3/datasets/elysia/raw/Elysia.jsonl`.
Run normalize, split, and validate before training:

```powershell
python training/qwen3/preprocess/normalize.py --config training/qwen3/configs/characters/elysia.yaml
python training/qwen3/preprocess/split.py --config training/qwen3/configs/characters/elysia.yaml
python training/qwen3/preprocess/validate.py --config training/qwen3/configs/characters/elysia.yaml
```
