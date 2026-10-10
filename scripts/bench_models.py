"""Compara modelos con visión por precisión de clic, latencia y costo.

Usa una captura con elementos de posición conocida. Costo típico: centavos.

    python scripts/bench_models.py capturas/01_inicio.png qwen/qwen3.7-flash google/gemini-2.5-flash-lite
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.screen import jpeg_data_url  # noqa: E402

# Centros medidos a mano sobre capturas/01_inicio.png (SIGECOM, lista de Pedidos).
TARGETS = {
    "menu Herramientas": (296, 48),
    "botón Exportar": (176, 166),
    "botón Buscar": (705, 130),
    "campo de texto Fecha desde": (421, 131),
    "enlace PD-0101": (48, 517),
    "botón Siguiente (paginación)": (209, 684),
    "lista desplegable Estado": (259, 131),
}

PROMPT = """La imagen es una captura de pantalla de {w}x{h} píxeles.
Para cada elemento, da las coordenadas en píxeles (x, y) del centro, en el sistema de la imagen
(origen arriba a la izquierda, x de 0 a {w}, y de 0 a {h}).
Elementos: {names}
Además, lee la fecha que aparece después de "Hoy:".
Responde solo JSON: {{"elementos": {{"<nombre>": [x, y], ...}}, "hoy": "dd/mm/aaaa"}}"""


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


async def run_model(client: httpx.AsyncClient, key: str, model: str, img_url: str, w: int, h: int) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT.format(w=w, h=h, names=json.dumps(list(TARGETS), ensure_ascii=False))},
            {"type": "image_url", "image_url": {"url": img_url}},
        ]}],
        "temperature": 0,
        "max_tokens": 600,
        "usage": {"include": True},
    }
    t = time.monotonic()
    r = await client.post("https://openrouter.ai/api/v1/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {key}"}, timeout=90)
    dt = time.monotonic() - t
    data = r.json()
    if "choices" not in data:
        return {"model": model, "error": str(data)[:200], "s": dt}
    text = data["choices"][0]["message"].get("content") or ""
    usage = data.get("usage", {})
    try:
        out = parse_json(text)
    except Exception:
        return {"model": model, "error": "JSON inválido: " + text[:150], "s": dt}
    got = out.get("elementos", {})
    errs = {}
    for name, (tx, ty) in TARGETS.items():
        p = got.get(name)
        errs[name] = math.dist((tx, ty), p[:2]) if isinstance(p, list) and len(p) >= 2 else None
    return {"model": model, "s": dt, "errs": errs, "hoy": out.get("hoy"),
            "cost": usage.get("cost"), "tok_in": usage.get("prompt_tokens"), "raw": got, "text": text}


async def main(path: str, models: list[str]) -> None:
    key = get_settings().openrouter_api_key
    img = Image.open(path).convert("RGB")
    url = jpeg_data_url(img)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(run_model(client, key, m, url, img.width, img.height) for m in models))
    total = 0.0
    for r in results:
        if "error" in r:
            print(f"\n{r['model']}: ERROR {r['error']} ({r['s']:.1f}s)")
            continue
        vals = [e for e in r["errs"].values() if e is not None]
        hits = sum(1 for e in vals if e <= 12)
        total += r["cost"] or 0
        print(f"\n{r['model']}: {r['s']:.1f}s · costo ${r['cost']} · tokens in {r['tok_in']} · hoy={r['hoy']}")
        med = f"{sorted(vals)[len(vals) // 2]:.0f}px" if vals else "n/a"
        print(f"  aciertos (<=12px): {hits}/{len(TARGETS)} · error mediano {med}")
        if not vals:
            print("  respuesta:", " ".join(r["text"][:300].split()))
        for name, e in r["errs"].items():
            print(f"    {name:<32} {'—' if e is None else f'{e:5.0f}px'}  {r['raw'].get(name)}")
    print(f"\ncosto total: ${total:.5f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
