# Troubleshooting

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_runtime.ps1 -Doctor
```

Use the output to locate missing devices, dependencies, occupied ports, or model
files.

## Desktop text chat does not send

The chat input is intentionally disabled until the selected Qwen3 model is
ready. Click **启动模型** in the right panel and wait for the status to say that
the model is running. Initial model loading can take around 20 seconds on the
reference machine; the first reply can take another 15–20 seconds while the
prompt cache is cold.

If port 8080 is occupied by a server not started by Alysia Studio, stop it with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop_runtime.ps1
```

The desktop app records its Qwen3 PID and selected version under
`storage/temporary/`, allowing **关闭模型** and normal window shutdown to release
the process safely.
