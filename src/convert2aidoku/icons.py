from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw


def create_aidoku_icon(source: Path | None, destination: Path, *, initials: str = "C2A") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source is not None:
        with Image.open(source) as original:
            rgba = original.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            image = background.convert("RGB").resize((128, 128), Image.Resampling.LANCZOS)
            image.save(destination, format="PNG", optimize=True)
            return

    digest = hashlib.sha256(initials.encode()).digest()
    color = (64 + digest[0] // 2, 64 + digest[1] // 2, 64 + digest[2] // 2)
    image = Image.new("RGB", (128, 128), color)
    draw = ImageDraw.Draw(image)
    text = initials[:3].upper()
    box = draw.textbbox((0, 0), text)
    width, height = box[2] - box[0], box[3] - box[1]
    draw.text(((128 - width) / 2, (128 - height) / 2), text, fill="white")
    image.save(destination, format="PNG", optimize=True)
