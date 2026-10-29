from pathlib import Path
from uuid import uuid4

from runtime.sensevoice.config import load_sensevoice_config
from runtime.sensevoice.service import SenseVoiceRecognizer


def create_app(recognizer: SenseVoiceRecognizer | None = None):
    try:
        from fastapi import FastAPI, File, HTTPException, UploadFile
    except ImportError as exc:
        raise RuntimeError(
            "Missing web dependency 'fastapi'. Install stage 2 service requirements first."
        ) from exc

    app = FastAPI(title="sensevoice-service", version="0.1.0")
    config = load_sensevoice_config()
    state = {"recognizer": recognizer}

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"service": config.service_name, "status": "ok"}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(file: UploadFile = File(...)) -> dict[str, object]:
        current = state["recognizer"]
        if current is None:
            try:
                current = SenseVoiceRecognizer.from_config()
                state["recognizer"] = current
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        upload_dir = Path("storage/temporary/sensevoice_uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        audio_path = upload_dir / f"{uuid4().hex}{suffix}"
        audio_path.write_bytes(await file.read())

        try:
            return current.transcribe(audio_path).to_dict()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            audio_path.unlink(missing_ok=True)

    return app


app = create_app()
