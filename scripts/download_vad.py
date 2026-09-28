"""Build step: fetch the Silero VAD ONNX model so the first call doesn't pay the download."""

import os

from app.services.vad import ensure_model_file

if __name__ == "__main__":
    path = ensure_model_file(os.environ.get("VAD_MODEL_PATH", "models/silero_vad.onnx"))
    print(f"Silero VAD model ready at {path} ({path.stat().st_size} bytes)")
