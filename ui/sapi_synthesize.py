"""Isolated Windows SAPI synthesis worker used by the Aurora HUD.

Run by ui/launch.py in a child process so COM/SAPI stalls can be bounded by
subprocess.run(timeout=...). Do not import this module into the HUD process.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthesize Japanese text to WAV using Windows SAPI.")
    parser.add_argument("--text-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--rate", type=int, default=165)
    parser.add_argument("--volume", type=float, default=0.5)
    args = parser.parse_args()

    if sys.platform != "win32":
        print("SAPI synthesis is available only on Windows.", file=sys.stderr)
        return 2

    try:
        import pyttsx3

        text = args.text_file.read_text(encoding="utf-8").strip()
        if not text:
            print("Input text is empty.", file=sys.stderr)
            return 2

        args.output.parent.mkdir(parents=True, exist_ok=True)
        engine = pyttsx3.init(driverName="sapi5")
        engine.setProperty("voice", args.voice_id)
        engine.setProperty("rate", max(80, min(300, args.rate)))
        engine.setProperty("volume", max(0.0, min(1.0, args.volume)))
        engine.save_to_file(text, str(args.output))
        engine.runAndWait()
        engine.stop()

        if not args.output.is_file() or args.output.stat().st_size <= 44:
            print("SAPI finished without producing a valid WAV file.", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"SAPI synthesis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
