from runtime.gpt_sovits.config import load_gpt_sovits_config
from runtime.gpt_sovits.service import GPTSoVITSSynthesizer
from runtime.gpt_sovits.types import SpeechRequest


def create_app(synthesizer: GPTSoVITSSynthesizer | None = None):
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import Response
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError("Missing web dependency 'fastapi'. Install stage 4 requirements first.") from exc

    class SpeechPayload(BaseModel):
        text: str = Field(min_length=1)
        character_id: str = "elysia"
        emotion: str = "neutral"
        speed: float = 1.0
        format: str = "wav"

    config = load_gpt_sovits_config()
    app = FastAPI(title=config.service_name, version="0.1.0")
    state = {"synthesizer": synthesizer}

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"service": config.service_name, "status": "ok"}

    @app.post("/v1/audio/speech")
    def speech(payload: SpeechPayload) -> Response:
        current = state["synthesizer"]
        if current is None:
            try:
                current = GPTSoVITSSynthesizer.from_config()
                state["synthesizer"] = current
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        try:
            result = current.synthesize(
                SpeechRequest(
                    text=payload.text,
                    character_id=payload.character_id,
                    emotion=payload.emotion,
                    speed=payload.speed,
                    format=payload.format,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(content=result.audio_bytes, media_type="audio/wav")

    return app


app = create_app()
