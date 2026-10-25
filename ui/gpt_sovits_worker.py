"""Worker persistente de inferência direta para GPT-SoVITS da Aurora.

O stdout é um canal JSONL privado entre processos; logs do GPT-SoVITS vão para stderr.
Não inicia Gradio nem um servidor HTTP.
"""
from __future__ import annotations

import argparse
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


def _same_path(first: Any, second: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(os.path.abspath(str(second)))
    except (TypeError, ValueError):
        return False


def _restore_weight_file(path: Path, existed: bool, original: bytes | None) -> None:
    """O import do WebUI atualiza weight.json; não alteramos a preferência do usuário."""
    try:
        if existed and original is not None:
            path.write_bytes(original)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[Aurora direct TTS] Não foi possível restaurar {path}: {exc}", file=sys.stderr)


def _load_engine(args: argparse.Namespace):
    root = Path(args.root).resolve()
    inference_dir = root / "GPT_SoVITS"
    required = [
        (inference_dir / "inference_webui.py", "GPT_SoVITS/inference_webui.py"),
        (Path(args.gpt_model), "modelo GPT"),
        (Path(args.sovits_model), "modelo SoVITS"),
        (Path(args.reference), "WAV de referência"),
    ]
    for path, label in required:
        if not path.is_file():
            raise FileNotFoundError(f"{label} não encontrado: {path}")

    # O import da inferência usa imports relativos a GPT_SoVITS e à raiz.
    for import_path in (str(root), str(inference_dir)):
        if import_path not in sys.path:
            sys.path.insert(0, import_path)

    # Correspondem às variáveis que a própria inferência-webui lê no início.
    os.environ["gpt_path"] = str(Path(args.gpt_model).resolve())
    os.environ["sovits_path"] = str(Path(args.sovits_model).resolve())
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("is_half", "True")

    weight_file = root / "weight.json"
    existed = weight_file.is_file()
    original = weight_file.read_bytes() if existed else None
    try:
        with redirect_stdout(sys.stderr), redirect_stderr(sys.stderr):
            from GPT_SoVITS import inference_webui as infer

            # A maioria das versões carrega os caminhos dados acima durante o import.
            # Corrige a seleção somente se a versão instalada não tiver respeitado as variáveis.
            loaded_sovits = getattr(infer, "sovits_path", None)
            if not _same_path(loaded_sovits, Path(args.sovits_model)):
                sovits_result = infer.change_sovits_weights(str(Path(args.sovits_model).resolve()))
                if hasattr(sovits_result, "__next__"):
                    next(sovits_result)

            loaded_gpt = getattr(infer, "gpt_path_global", None)
            if not _same_path(loaded_gpt, Path(args.gpt_model)):
                infer.change_gpt_weights(str(Path(args.gpt_model).resolve()))
    finally:
        _restore_weight_file(weight_file, existed, original)

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

            # Usa a mesma família de parâmetros da integração anterior da Aurora.
            # inp_refs=[] evita os defaults problemáticos de algumas versões antigas.
            language = infer.i18n("日文")
            generated = list(infer.get_tts_wav(
                ref_wav_path=str(Path(args.reference).resolve()),
                prompt_text=args.prompt_text,
                prompt_language=language,
                text=text,
                text_language=language,
                top_k=15,
                top_p=0.8,
                temperature=0.8,
                speed=1.0,
                inp_refs=[],
                # O caminho direto não usa a aceleração CUDA Graph/Flash Attention.
                # É mais conservador para GPUs como a GTX 1660 e evita sinal inválido.
                use_cuda_graph=False,
                use_flash_attention=False,
            ))
            if not generated:
                raise RuntimeError("GPT-SoVITS não retornou áudio.")
            sample_rate, audio = generated[-1]
            audio_array = np.asarray(audio)
            # Diagnóstico em amplitude antes da etapa de reprodução do Windows.
            audio_float = audio_array.astype(np.float64, copy=False)
            peak = float(np.max(np.abs(audio_float))) if audio_float.size else 0.0
            rms = float(np.sqrt(np.mean(np.square(audio_float)))) if audio_float.size else 0.0
            frames = int(audio_array.shape[0]) if audio_array.ndim else int(audio_array.size)
            duration = frames / max(1, int(sample_rate))
            if not np.isfinite(peak) or not np.isfinite(rms) or peak < (2.0 if audio_array.dtype.kind in "iu" else 1e-5):
                raise RuntimeError(
                    f"GPT-SoVITS retornou áudio silencioso (pico={peak:.4g}, RMS={rms:.4g}); "
                    "o WAV não será salvo como uma síntese válida."
                )
            print(
                f"[Aurora direct TTS] Sinal gerado: shape={audio_array.shape}, "
                f"dtype={audio_array.dtype}, pico={peak:.1f}, RMS={rms:.1f}, "
                f"duração={duration:.2f}s, taxa={int(sample_rate)} Hz.",
                file=sys.stderr,
            )
            sf.write(str(output_path), audio, int(sample_rate), subtype="PCM_16")
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
