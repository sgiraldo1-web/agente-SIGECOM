"""Percepción: espera de estabilidad, capturas JPEG/PNG y recortes ampliados."""

from __future__ import annotations

import asyncio
import base64
import io
import time

import numpy as np
from PIL import Image

from app.rfb import RFBClient

# Píxeles distintos que se toleran entre dos capturas para darlas por iguales
# (el cursor de texto parpadea y no debe impedir la estabilidad).
STABLE_TOLERANCE = 200


def diff_pixels(a: np.ndarray, b: np.ndarray) -> int:
    if a.shape != b.shape:
        return a.shape[0] * a.shape[1]
    return int(np.count_nonzero(np.any(a != b, axis=2)))


async def wait_stable(client: RFBClient, *, min_wait: float = 1.0, max_wait: float = 3.0,
                      interval: float = 0.3, tolerance: int = STABLE_TOLERANCE
                      ) -> tuple[np.ndarray, bool]:
    """Espera al menos `min_wait` s y luego hasta que dos capturas seguidas
    coincidan, con tope en `max_wait` s. Devuelve (captura, se_estabilizó)."""
    start = time.monotonic()
    await asyncio.sleep(min_wait)
    prev = client.snapshot()
    while time.monotonic() - start < max_wait:
        await asyncio.sleep(interval)
        cur = client.snapshot()
        if diff_pixels(prev, cur) <= tolerance:
            return cur, True
        prev = cur
    return client.snapshot(), False


def to_image(frame: np.ndarray) -> Image.Image:
    return Image.fromarray(frame, "RGB")


def jpeg_bytes(frame: np.ndarray | Image.Image, quality: int = 75) -> bytes:
    img = frame if isinstance(frame, Image.Image) else to_image(frame)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def jpeg_data_url(frame: np.ndarray | Image.Image, quality: int = 75) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes(frame, quality)).decode()


def save_png(frame: np.ndarray, path: str) -> None:
    to_image(frame).save(path, format="PNG")


def zoom(frame: np.ndarray, x: int, y: int, w: int, h: int, max_side: int = 1280) -> Image.Image:
    """Recorte ampliado de una zona. No toca la app, solo la imagen."""
    H, W = frame.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(W, x0 + max(1, int(w))), min(H, y0 + max(1, int(h)))
    crop = to_image(frame[y0:y1, x0:x1])
    scale = min(4.0, max_side / max(crop.width, crop.height))
    if scale > 1:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
    return crop
