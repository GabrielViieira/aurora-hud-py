
from __future__ import annotations

import base64
from io import BytesIO

from mss import MSS
from PIL import Image


def capture_monitors(
    max_width: int = 960, monitor_index: int | None = None
) -> list[dict]:
    """Captura monitores em JPEG/Base64, opcionalmente apenas um.

    Não salva capturas em disco. O índice começa em 1; None captura todos
    para manter compatibilidade com o uso de diagnóstico no fim deste arquivo.
    """
    if max_width < 320:
        raise ValueError("max_width deve ser pelo menos 320")

    if monitor_index is not None and monitor_index < 1:
        raise ValueError("monitor_index deve ser maior ou igual a 1")

    results = []

    with MSS() as sct:
        # monitores[0] é a área virtual combinada;
        # monitores[1:] são os monitores individuais.
        monitors = list(enumerate(sct.monitors[1:], start=1))
        if monitor_index is not None:
            monitors = [(index, monitor) for index, monitor in monitors
                        if index == monitor_index]
        for index, monitor in monitors:
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
