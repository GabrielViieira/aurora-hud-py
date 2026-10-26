"""Worker persistente de inferência direta para GPT-SoVITS da Aurora.

O stdout é um canal JSONL privado entre processos; logs do GPT-SoVITS vão para stderr.
Não inicia Gradio nem um servidor HTTP.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import traceback
import shutil
import numpy as np
import unicodedata
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from typing import Any

_protocol_stdout = sys.stdout
sys.stdout = sys.stderr


def emit(payload: dict[str, Any]) -> None:
    _protocol_stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _protocol_stdout.flush()


def _load_engine(args: argparse.Namespace):
    root = Path(args.root).resolve()
    api_path = root / "api.py"
    required = [
        (api_path, "api.py do GPT-SoVITS"),
        (Path(args.gpt_model), "modelo GPT"),
        (Path(args.sovits_model), "modelo SoVITS"),
        (Path(args.reference), "WAV de referência"),
    ]
    for path, label in required:
        if not path.is_file():
            raise FileNotFoundError(f"{label} não encontrado: {path}")

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Importa o módulo Python de inferência usado pela API oficial, sem executar
    # uvicorn.run(). Os argumentos fazem api.py carregar os pesos corretos.
    original_argv = sys.argv[:]
    sys.argv = [
        str(api_path),
        "-s", str(Path(args.sovits_model).resolve()),
        "-g", str(Path(args.gpt_model).resolve()),
        "-dr", str(Path(args.reference).resolve()),
        "-dt", args.prompt_text,
        "-dl", "日文",
        "-d", args.device,
        "-a", "127.0.0.1",
        "-p", "9880",
        "-sm", "close",
        "-mt", "wav",
        "-st", "int16",
    ]
    sys.argv.append("-hp" if args.device.lower().startswith("cuda") else "-fp")
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        with redirect_stdout(sys.stderr), redirect_stderr(sys.stderr):
            import importlib
            infer = importlib.import_module("api")
    finally:
        sys.argv = original_argv

    # A importação executa a inicialização dos pesos mas NÃO inicia Uvicorn,
    # pois o bloco de servidor só roda quando api.py é executado como script.
    speakers = getattr(infer, "speaker_list", {})
    speaker = speakers.get("default") if isinstance(speakers, dict) else None
    if speaker is None:
        raise RuntimeError("api.py terminou de importar, mas não carregou o speaker default.")
    if getattr(speaker, "gpt", None) is None or getattr(speaker, "sovits", None) is None:
        raise RuntimeError("api.py não inicializou os modelos GPT e SoVITS.")
    print(
        "[Aurora direct TTS] Inferência carregada pelo api.py: "
        f"GPT={args.gpt_model}, SoVITS={args.sovits_model}",
        file=sys.stderr,
    )
    return infer


def _handle_request(infer, args: argparse.Namespace, request: dict[str, Any]) -> None:
    if request.get("cmd") == "shutdown":
        emit({"ok": True, "shutdown": True})
        raise SystemExit(0)

    output_path = Path(str(request.get("output_path", ""))).resolve()
    raw_text = str(request.get("text", "")).strip()

    # Defesa extra para texto Unicode vindo de processos Windows. A serialização
    # do lado da Aurora usa ASCII-JSON, mas removemos surrogates isolados e U+FFFD
    # para que o detector de idioma nunca receba caracteres corrompidos.
    text = "".join(
        char for char in raw_text
        if char != "\ufffd" and not ("\ud800" <= char <= "\udfff")
        and (char in "\n\r\t" or ord(char) >= 32)
    )
    text = unicodedata.normalize("NFC", text).strip()
    if not text:
        emit({"ok": False, "error": "O texto ficou vazio após a validação Unicode."})
        return
    if text != raw_text:
        print(
            f"[Aurora direct TTS] Removidos {len(raw_text) - len(text)} caracteres Unicode inválidos.",
            file=sys.stderr,
        )
    if not re.search(r"[\u3040-\u309f\u30a0-\u30ff]", text):
        emit({
            "ok": False,
            "error": "O texto recebido não contém hiragana/katakana válidos; a síntese foi cancelada.",
        })
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with redirect_stdout(sys.stderr), redirect_stderr(sys.stderr):
            import soundfile as sf

            # Chama a função de inferência do api.py, não a da WebUI.
            # O speaker default foi carregado no próprio módulo Python.
            generated = list(infer.get_tts_wav(
                ref_wav_path=str(Path(args.reference).resolve()),
                prompt_text=args.prompt_text,
                prompt_language="日文",
                text=text,
                text_language="日文",
                top_k=15,
                top_p=1.0,
                temperature=1.0,
                speed=1.0,
                inp_refs=[],
                spk="default",
            ))
            if not generated:
                raise RuntimeError("GPT-SoVITS não retornou áudio.")

            sf_dtype = "int16"
            returned = generated[-1]
            if isinstance(returned, (bytes, bytearray, memoryview)):
                # Na versão GPT-SoVITS-v2pro-20250604, api.get_tts_wav
                # retorna os bytes do WAV completo (modo close + media_type wav).
                audio_bytes = b"".join(bytes(part) for part in generated)
                if len(audio_bytes) < 44 or audio_bytes[:4] != b"RIFF" or b"WAVE" not in audio_bytes[:12]:
                    raise RuntimeError(
                        "api.get_tts_wav não retornou um WAV PCM válido; "
                        f"prefixo recebido: {audio_bytes[:20]!r}"
                    )
                output_path.write_bytes(audio_bytes)
                audio_array, sample_rate = sf.read(
                    io.BytesIO(audio_bytes), dtype=sf_dtype, always_2d=False
                )
            elif isinstance(returned, tuple) and len(returned) == 2:
                # Compatibilidade com forks que expõem (sample_rate, samples).
                sample_rate, audio = returned
                audio_array = np.asarray(audio)
                sf.write(str(output_path), audio_array, int(sample_rate), subtype="PCM_16")
            else:
                raise RuntimeError(
                    "Formato de retorno de GPT-SoVITS não reconhecido: "
                    f"{type(returned).__name__}"
                )

            audio_array = np.asarray(audio_array)
            audio_float = audio_array.astype(np.float64, copy=False)
            peak = float(np.max(np.abs(audio_float))) if audio_float.size else 0.0
            rms = float(np.sqrt(np.mean(np.square(audio_float)))) if audio_float.size else 0.0
            frames = int(audio_array.shape[0]) if audio_array.ndim else int(audio_array.size)
            duration = frames / max(1, int(sample_rate))
            if not np.isfinite(peak) or not np.isfinite(rms) or peak < (2.0 if audio_array.dtype.kind in "iu" else 1e-5):
                raise RuntimeError(
                    f"GPT-SoVITS retornou áudio silencioso (pico={peak:.4g}, RMS={rms:.4g}); "
                    "o WAV não será considerado uma síntese válida."
                )
            print(
                f"[Aurora direct TTS] Sinal gerado: shape={audio_array.shape}, "
                f"dtype={audio_array.dtype}, pico={peak:.1f}, RMS={rms:.1f}, "
                f"duração={duration:.2f}s, taxa={int(sample_rate)} Hz.",
                file=sys.stderr,
            )
            if not output_path.is_file() or output_path.stat().st_size < 44:
                raise RuntimeError("O arquivo WAV de saída não foi criado corretamente.")

        # Guarda somente o WAV mais recente para teste independente da Aurora.
        debug_wav = Path(os.environ.get(
            "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
        )) / "Aurora" / "debug" / "raphael_latest.wav"
        try:
            debug_wav.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, debug_wav)
            print(f"[Aurora direct TTS] WAV de diagnóstico salvo em: {debug_wav}", file=sys.stderr)
        except OSError as exc:
            print(f"[Aurora direct TTS] Não foi possível salvar WAV de diagnóstico: {exc}", file=sys.stderr)

        emit({"ok": True, "sample_rate": int(sample_rate), "duration": duration, "peak": peak, "rms": rms})
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit({"ok": False, "error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Inferência persistente GPT-SoVITS para Aurora")
    parser.add_argument("--root", required=True)
    parser.add_argument("--gpt-model", required=True)
    parser.add_argument("--sovits-model", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--prompt-text", required=True)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()

    try:
        infer = _load_engine(args)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit({"ok": False, "stage": "startup", "error": str(exc)})
        return 2

    emit({"ok": True, "ready": True})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("A mensagem precisa ser um objeto JSON.")
            _handle_request(infer, args, request)
        except SystemExit:
            return 0
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            emit({"ok": False, "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
