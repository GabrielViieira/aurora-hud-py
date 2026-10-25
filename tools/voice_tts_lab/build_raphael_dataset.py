#!/usr/bin/env python3
"""Build a Japanese GPT-SoVITS dataset from Nanami -> Raphael RVC audio.

This script runs on the Windows machine that has Applio and Raphael_Sensei.pth.
It does not train GPT-SoVITS; it prepares converted WAVs plus the GPT-SoVITS
annotation list (audio_path|speaker|language|text).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

DEFAULT_APPLIO = Path(
    os.environ.get("AURORA_APPLIO_DIR", r"C:\Users\Administrator\Downloads\Applio-main")
)
DEFAULT_VOICE = "ja-JP-NanamiNeural"
SPEAKER_NAME = "Raphael"
DEFAULT_OUTPUT = Path.home() / "Documents" / "AuroraVoiceLab" / "Raphael_JA"

INDEX_CANDIDATES = (
    Path("logs/Raphael_Sensei/added_IVF2165_Flat_nprobe_1_Raphael_Sensei_v2.index"),
    Path("logs/Raphael_Sensei/added.index"),
    Path("logs/Raphael_Sensei/trained/added.index"),
    Path("assets/weights/Raphael_Sensei/added_IVF2165_Flat_nprobe_1_Raphael_Sensei_v2.index"),
)


def py_literal(value: str | Path) -> str:
    """Return a safely quoted Python string literal, including Windows paths."""
    return json.dumps(str(value), ensure_ascii=False)


def read_phrases(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo de frases não encontrado: {path}")
    phrases: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            raise ValueError(f"A frase contém '|', que é reservado no manifesto: {line}")
        phrases.append(line)
    if not phrases:
        raise ValueError(f"Nenhuma frase útil em {path}")
    return phrases


def find_model(applio_dir: Path, explicit_pth: Path | None) -> Path:
    candidates = [explicit_pth] if explicit_pth else [
        applio_dir / "logs/Raphael_Sensei.pth",
        applio_dir / "assets/weights/Raphael_Sensei/Raphael_Sensei.pth",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    checked = "\n".join(f"  - {p}" for p in candidates)
    raise FileNotFoundError("Não encontrei Raphael_Sensei.pth. Caminhos verificados:\n" + checked)


def find_index(applio_dir: Path, explicit_index: Path | None) -> Path | None:
    if explicit_index:
        if not explicit_index.is_file():
            raise FileNotFoundError(f"Index informado não existe: {explicit_index}")
        return explicit_index.resolve()
    env_index = os.environ.get("AURORA_RAPHAEL_INDEX")
    if env_index:
        candidate = Path(env_index)
        if candidate.is_file():
            return candidate.resolve()
        raise FileNotFoundError(f"AURORA_RAPHAEL_INDEX não existe: {candidate}")
    for relative in INDEX_CANDIDATES:
        candidate = applio_dir / relative
        if candidate.is_file():
            return candidate.resolve()
    return None


def run_python_snippet(
    python_exe: Path,
    applio_dir: Path,
    code: str,
    timeout: int,
) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [str(python_exe), "-c", code],
            cwd=str(applio_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"Timeout após {timeout}s"
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    return result.returncode, output.strip()


def synthesize_source(
    python_exe: Path,
    applio_dir: Path,
    text: str,
    voice: str,
    output_wav: Path,
    timeout: int,
) -> tuple[bool, str]:
    code = (
        "import sys, asyncio; "
        f"sys.path.insert(0, {py_literal(applio_dir)}); "
        "from rvc.lib.tools import tts; "
        f"asyncio.run(tts.generate_tts({py_literal(text)}, {py_literal(voice)}, 0, "
        f"{py_literal(output_wav)}))"
    )
    rc, output = run_python_snippet(python_exe, applio_dir, code, timeout)
    valid = output_wav.is_file() and output_wav.stat().st_size > 44
    if rc != 0 or not valid:
        return False, output or f"Edge TTS não criou um WAV válido: {output_wav}"
    return True, output


def convert_to_raphael(
    python_exe: Path,
    applio_dir: Path,
    source_wav: Path,
    output_wav: Path,
    pth: Path,
    index: Path | None,
    index_rate: float,
    embedder: str,
    timeout: int,
) -> tuple[bool, str]:
    index_path = str(index) if index else ""
    args = {
        "pitch": 0,
        "index_rate": index_rate,
        "volume_envelope": 1.0,
        "protect": 0.33,
        "f0_method": "rmvpe",
        "input_path": str(source_wav),
        "output_path": str(output_wav),
        "pth_path": str(pth),
        "index_path": index_path,
        "f0_autotune": False,
        "f0_autotune_strength": 1.0,
        "proposed_pitch": False,
        "proposed_pitch_threshold": 155.0,
        "export_format": "WAV",
        "embedder_model": embedder,
        "formant_shifting": False,
        "formant_qfrency": 1.0,
        "formant_timbre": 1.0,
        "sid": 0,
        "cleanup_mode": "off",
        "cleanup_strength": 12,
    }
    kwargs = ", ".join(f"{key}={py_literal(value) if isinstance(value, str) else repr(value)}"
                       for key, value in args.items())
    code = (
        "import sys; "
        f"sys.path.insert(0, {py_literal(applio_dir)}); "
        "from core import run_infer_script; "
        f"run_infer_script({kwargs})"
    )
    rc, output = run_python_snippet(python_exe, applio_dir, code, timeout)
    valid = output_wav.is_file() and output_wav.stat().st_size > 44
    if rc != 0 or not valid:
        return False, output or f"RVC não criou um WAV válido: {output_wav}"
    return True, output


def write_manifest(records: Iterable[tuple[Path, str]], destination: Path) -> int:
    lines = []
    for wav_path, phrase in records:
        if wav_path.is_file() and wav_path.stat().st_size > 44:
            # GPT-SoVITS format: audio_path|speaker_name|language|text
            lines.append(f"{wav_path.resolve()}|{SPEAKER_NAME}|ja|{phrase}")
    destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gera dataset japonês Nanami -> Raphael para experimentar GPT-SoVITS."
    )
    parser.add_argument(
        "--applio-dir", type=Path, default=DEFAULT_APPLIO,
        help="Pasta do Applio (padrão: AURORA_APPLIO_DIR ou caminho usado pela Aurora).",
    )
    parser.add_argument("--pth", type=Path, help="Caminho explícito para Raphael_Sensei.pth.")
    parser.add_argument("--index", type=Path, help="Caminho explícito para o .index (opcional).")
    parser.add_argument(
        "--phrases", type=Path,
        default=Path(__file__).with_name("phrases_ja.txt"),
        help="Arquivo UTF-8, uma frase japonesa por linha.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="Pasta local de saída.")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="Voz Edge TTS de entrada.")
    parser.add_argument("--speaker", default=SPEAKER_NAME, help="Rótulo de speaker no manifesto.")
    parser.add_argument("--limit", type=int, default=0, help="Processa só as primeiras N frases (teste).")
    parser.add_argument("--index-rate", type=float, default=0.75)
    parser.add_argument("--embedder", default="contentvec")
    parser.add_argument("--tts-timeout", type=int, default=90)
    parser.add_argument("--rvc-timeout", type=int, default=300)
    parser.add_argument("--force", action="store_true", help="Refaz áudios que já existem.")
    args = parser.parse_args()

    global SPEAKER_NAME
    SPEAKER_NAME = args.speaker

    applio_dir = args.applio_dir.expanduser().resolve()
    if not applio_dir.is_dir():
        print(f"[ERRO] Pasta do Applio não existe: {applio_dir}", file=sys.stderr)
        return 2
    if not (applio_dir / "core.py").is_file():
        print(f"[ERRO] Não encontrei core.py em {applio_dir}; confira a pasta do Applio.", file=sys.stderr)
        return 2

    try:
        phrases = read_phrases(args.phrases)
        pth = find_model(applio_dir, args.pth)
        index = find_index(applio_dir, args.index)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        return 2

    python_exe = applio_dir / "env" / "python.exe"
    if not python_exe.is_file():
        python_exe = Path(sys.executable).resolve()

    if args.limit < 0:
        print("[ERRO] --limit deve ser zero (todas) ou um número positivo.", file=sys.stderr)
        return 2
    selected = phrases[:args.limit] if args.limit else phrases
    source_dir = args.out / "source_nanami"
    target_dir = args.out / "converted_raphael"
    source_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "manifest.list"

    print(f"Applio: {applio_dir}")
    print(f"Python: {python_exe}")
    print(f"Modelo: {pth}")
    print(f"Index: {index if index else '(não encontrado; conversão sem index)'}")
    print(f"Frases: {len(selected)} / {len(phrases)}")
    print(f"Saída: {args.out.resolve()}")
    print("Nota: Edge TTS requer internet; a conversão RVC será executada localmente.")

    completed: list[tuple[Path, str]] = []
    failures = 0
    for number, phrase in enumerate(selected, start=1):
        stem = f"{number:04d}"
        source_wav = source_dir / f"{stem}_nanami.wav"
        output_wav = target_dir / f"{stem}_raphael.wav"
        if output_wav.is_file() and output_wav.stat().st_size > 44 and not args.force:
            print(f"[{number:03d}/{len(selected):03d}] existente; mantendo {output_wav.name}")
            completed.append((output_wav, phrase))
            write_manifest(completed, manifest)
            continue

        print(f"[{number:03d}/{len(selected):03d}] Nanami: {phrase}")
        ok, details = synthesize_source(
            python_exe, applio_dir, phrase, args.voice, source_wav, args.tts_timeout
        )
        if not ok:
            failures += 1
            print(f"  [FALHA TTS] {details[-1200:]}")
            continue

        print("              convertendo para Raphael...")
        ok, details = convert_to_raphael(
            python_exe, applio_dir, source_wav, output_wav, pth, index,
            args.index_rate, args.embedder, args.rvc_timeout
        )
        if not ok:
            failures += 1
            print(f"  [FALHA RVC] {details[-1600:]}")
            continue

        completed.append((output_wav, phrase))
        write_manifest(completed, manifest)
        print(f"  [OK] {output_wav.name}")

    valid_count = write_manifest(completed, manifest)
    print("\n=== Resultado ===")
    print(f"Áudios convertidos válidos: {valid_count}")
    print(f"Falhas nesta execução: {failures}")
    print(f"Manifesto GPT-SoVITS: {manifest.resolve()}")
    print("Próximo passo: abrir GPT-SoVITS, importar esse manifesto/lista, revisar os áudios/transcrições e fazer fine-tuning.")
    if valid_count == 0:
        return 1
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
