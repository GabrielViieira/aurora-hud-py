"""Persistent isolated pyttsx3/SAPI synthesis worker.

Protocol: one JSON object per stdin line, one JSON response per stdout line.
The worker initializes COM/SAPI once and reuses the engine for successive WAV jobs.
"""
from __future__ import annotations

import json
import sys
import traceback


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    if sys.platform != "win32":
        emit({"ok": False, "error": "SAPI requires Windows", "ready": False})
        return 2
    try:
        import pyttsx3
        engine = pyttsx3.init(driverName="sapi5")
        emit({"ok": True, "ready": True})
    except Exception as exc:
        emit({"ok": False, "ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1

    for line in sys.stdin:
        try:
            request = json.loads(line)
        except Exception as exc:
            emit({"ok": False, "error": f"invalid request JSON: {exc}"})
            continue
        if request.get("cmd") == "shutdown":
            break
        request_id = request.get("id")
        try:
            text = str(request.get("text") or "").strip()
            output = str(request.get("output") or "")
            if not text or not output:
                raise ValueError("text/output are required")
            voice = str(request.get("voice_id") or "")
            if voice:
                engine.setProperty("voice", voice)
            engine.setProperty("rate", max(80, min(300, int(request.get("rate", 165)))))
            engine.setProperty("volume", max(0.0, min(1.0, float(request.get("volume", 0.5)))))
            engine.save_to_file(text, output)
            engine.runAndWait()
            engine.stop()
            emit({"id": request_id, "ok": True, "output": output})
        except Exception as exc:
            try:
                engine.stop()
            except Exception:
                pass
            emit({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    try:
        engine.stop()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
