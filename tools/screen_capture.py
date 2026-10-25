
from __future__ import annotations

import base64
from io import BytesIO

from mss import MSS
from PIL import Image


def capture_monitors(max_width: int = 1920) -> list[dict]:
    """Captura cada monitor e devolve imagens JPEG em Base64.

    Não salva capturas em disco.
    O índice de monitor começa em 1; 0 representa a área
    de trabalho virtual completa no MSS.
    """
    if max_width < 320:
        raise ValueError("max_width deve ser pelo menos 320")

    results = []

    with MSS() as sct:
        # monitores[0] é a área virtual combinada;
        # monitores[1:] são os monitores individuais.
        for index, monitor in enumerate(sct.monitors[1:], start=1):
            shot = sct.grab(monitor)

            image = Image.frombytes(
                "RGB",
                shot.size,
                shot.rgb,
            )

            original_size = image.size
            image.thumbnail((max_width, max_width * 2))

            buffer = BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=88,
                optimize=True,
            )

            encoded = base64.b64encode(
                buffer.getvalue()
            ).decode("ascii")

            results.append({
    "monitor": index,
    "left": monitor["left"],
    "top": monitor["top"],
    "width": monitor["width"],
    "height": monitor["height"],
    "original_size": original_size,
    "captured_size": image.size,
    "mime_type": "image/jpeg",
    "data_url": f"data:image/jpeg;base64,{encoded}",
    })

    return results


if __name__ == "__main__":
    screens = capture_monitors()

    for screen in screens:
        print(
            f"Monitor {screen['monitor']}: "
            f"{screen['width']}x{screen['height']} "
            f"(origem {screen['left']}, {screen['top']})"
        )
