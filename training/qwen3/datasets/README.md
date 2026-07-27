# Qwen3 Role Dataset Format

Stage 7 normalizes role data to JSONL chat records:

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
