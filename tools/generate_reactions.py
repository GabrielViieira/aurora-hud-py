"""Generate local Japanese reaction WAVs using the configured SAPI voice ID.

Example (Windows):
  py tools/generate_reactions.py --voice-id "VOICE_ID_FROM_HUD_SETTINGS"

Generated audio is local-only and should not be committed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "ui" / "sapi_synthesize.py"
OUTPUT_DIR = ROOT / "voice" / "reactions"

REACTIONS = [
    ("hai", "はい"),
    ("etto", "えっと…"),
    ("ryokai", "了解です"),
    ("chotto_matte", "ちょっと待ってね"),
    ("naruhodo", "あ、なるほど"),
    ("sou_desu_ne", "そうですね"),
    ("naruhodo_ne", "なるほどね"),
    ("hai_hai", "はいはい"),
    ("un", "うん"),
    ("kangaemasu", "ちょっと考えますね"),
    ("shoushou", "少々お待ちを"),
    ("kashikomarimashita", "かしこまりました"),
    ("shouchi", "承知しました"),
    ("ok_desu", "OKです"),
    ("ii_desu_ne", "いいですね"),
    ("sou_nan_desu_ne", "そうなんですね"),
    ("hee", "へえ"),
    ("honto", "ほんと？"),
    ("maa", "まあ"),
    ("ano_ne", "あのね"),
    ("eeto", "えーと"),
    ("nn", "んー"),
    ("wakりました", "はい、わかりました"),
    ("ryokai_short", "了解"),
    ("ok", "オッケー"),
    ("haa_i", "はーい"),
    ("un_un", "うんうん"),
    ("sousou", "そうそう"),
    ("tashikani", "たしかに"),
    ("naruhodo_desu", "なるほどです"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Render cached Aurora Japanese reactions with SAPI.")
    parser.add_argument("--voice-id", required=True, help="SAPI voice ID for the Nanami voice")
    parser.add_argument("--rate", type=int, default=165)
    parser.add_argument("--volume", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=35.0)
    args = parser.parse_args()

    if sys.platform != "win32":
        print("This generator requires Windows SAPI.", file=sys.stderr)
        return 2
    if not WORKER.is_file():
        print(f"SAPI worker not found: {WORKER}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    with tempfile.TemporaryDirectory(prefix="aurora-reactions-") as temp_dir:
        temp = Path(temp_dir)
        for name, phrase in REACTIONS:
            target = args.output_dir / f"{name}.wav"
            if target.is_file() and target.stat().st_size > 44:
                print(f"[skip] {target.name} already exists")
                continue
            text_path = temp / f"{name}.txt"
            text_path.write_text(phrase, encoding="utf-8")
            command = [
                sys.executable, str(WORKER),
                "--text-file", str(text_path),
                "--output", str(target),
                "--voice-id", args.voice_id,
                "--rate", str(args.rate),
                "--volume", str(args.volume),
            ]
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=args.timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0 and target.is_file() and target.stat().st_size > 44:
                    print(f"[ok] {target.name}")
                else:
                    detail = (result.stderr or result.stdout or "invalid/empty WAV").strip()
                    failures.append((name, detail))
                    print(f"[fail] {name}: {detail}")
            except (subprocess.TimeoutExpired, OSError) as exc:
                failures.append((name, str(exc)))
                print(f"[fail] {name}: {exc}")

    print(f"Generated or already cached: {len(REACTIONS) - len(failures)}/{len(REACTIONS)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
