"""Smoke test for local screen-image understanding through an Ollama endpoint.

Run from the Aurora source directory:
    py tools/test_local_vision.py
The screenshot is sent only to the configured loopback endpoint.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]


def _read_env_file() -> dict[str, str]:
    """Read only the simple KEY=VALUE lines needed by this diagnostic."""
    for path in (APP_ROOT.parent / ".env", APP_ROOT / ".env"):
        if not path.is_file():
            continue
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if key:
                values[key] = value
        return values
    return {}


def _capture_jpeg(max_width: int = 1280, quality: int = 80) -> tuple[str, tuple[int, int]]:
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError(
            "PyAutoGUI não está instalado. Ative o ambiente da Aurora e instale as dependências desktop."
        ) from exc

    image = pyautogui.screenshot()
    original_size = image.size
    if max_width > 0 and image.width > max_width:
        new_height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, new_height))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}", original_size


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Captura a tela e testa sua descrição usando apenas o modelo visual local."
    )
    parser.add_argument(
        "--prompt",
        default="Descreva em português o que está visível na tela. Cite o aplicativo e os elementos principais.",
        help="Instrução enviada ao modelo visual.",
    )
    parser.add_argument("--max-width", type=int, default=1280, help="Largura máxima da imagem (padrão: 1280).")
    args = parser.parse_args()

    settings = _read_env_file()
    settings.update(os.environ)
    base_url = settings.get(
        "AURORA_LLM_VISION_BASE_URL", "http://127.0.0.1:11434/v1"
    ).strip().rstrip("/")
    model = settings.get("AURORA_LLM_VISION_MODEL", "qwen2.5vl:3b").strip()
    if not base_url or not model:
        print("ERRO: configure AURORA_LLM_VISION_BASE_URL e AURORA_LLM_VISION_MODEL.")
        return 2

    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "localhost", "127.0.0.1", "::1"
    }:
        print(
            "ERRO: este teste é destinado a visão local; o endpoint precisa apontar para localhost "
            "ou 127.0.0.1. A captura não foi enviada."
        )
        return 2

    try:
        image_data, original_size = _capture_jpeg(max_width=max(0, args.max_width))
    except Exception as exc:
        print(f"ERRO ao capturar a tela: {exc}")
        return 2

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": args.prompt},
                    {"type": "image_url", "image_url": {"url": image_data}},
                ],
            }
        ],
        "temperature": 0.2,
        "max_tokens": 350,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer ollama-local",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    print(f"Endpoint local: {base_url}")
    print(f"Modelo visual: {model}")
    print(f"Tela original: {original_size[0]}x{original_size[1]}")
    print("Enviando a captura somente ao endpoint local...")
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        print(f"ERRO HTTP {exc.code} do Ollama: {detail}")
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"ERRO de conexão/timeout com o endpoint local: {exc}")
        print("Confira se o Ollama está aberto e se o modelo já foi baixado.")
        return 1
    except json.JSONDecodeError:
        print("ERRO: o endpoint local não devolveu JSON válido.")
        return 1

    choices = data.get("choices") or []
    if not choices:
        print(f"ERRO: resposta sem choices: {json.dumps(data, ensure_ascii=False)[:1200]}")
        return 1
    message = choices[0].get("message") or {}
    answer = message.get("content") or ""
    if isinstance(answer, list):
        answer = "".join(
            part.get("text", "") for part in answer if isinstance(part, dict)
        )
    print("\n--- Descrição da tela ---")
    print(str(answer).strip() or "(o modelo retornou texto vazio)")
    print("\nTeste concluído. A imagem foi enviada apenas ao endpoint local.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
