# GPT-SoVITS Elysia Base v1 Assets

Place the required stage 4 runtime assets here:

```text
Elysia_v2-e15.ckpt
Elysia_v2_e24_s13080.pth
references/neutral.wav
references/happy.wav
references/gentle.wav
references/sad.wav
references/surprised.wav
references/serious.wav
```

Each reference WAV should be a clean 5-10 second character clip with stable
volume and an exact transcript in a same-name `.txt` sidecar, for example
`references/gentle.txt`. `configs/characters/elysia/reference_texts.yaml` is
kept as a fallback.
