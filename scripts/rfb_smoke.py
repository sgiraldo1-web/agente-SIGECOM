"""Hito 1: conectar al escritorio de práctica y ejecutar una secuencia de pasos.

Lee DESKTOP_URL y DESKTOP_PASSWORD de .env. Ejemplo:

    python scripts/rfb_smoke.py shot capturas/01.png click 120 15 stable shot capturas/02.png

Pasos disponibles:
    shot ARCHIVO           guarda la captura actual en PNG
    zoom X Y W H ARCHIVO   guarda un recorte ampliado
    click X Y | dclick X Y | rclick X Y
    type TEXTO             escribe carácter por carácter (sin Ctrl+A)
    key COMBO              tecla o combinación: enter, ctrl+a, shift+tab
    scroll X Y DY          DY > 0 baja
    wait S                 espera S segundos
    stable                 espera a que la pantalla se estabilice
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_desktop_settings  # noqa: E402
from app.rfb import RFBClient  # noqa: E402
from app.screen import save_png, wait_stable, zoom  # noqa: E402


async def run(args: list[str]) -> None:
    cfg = get_desktop_settings()
    if not cfg.url or not cfg.password:
        sys.exit("Faltan DESKTOP_URL o DESKTOP_PASSWORD en .env")

    t0 = time.monotonic()
    client = RFBClient(cfg.url, cfg.password)
    await client.connect()
    print(f"conectado en {time.monotonic() - t0:.2f}s · {client.name!r} {client.width}x{client.height} "
          f"· {client.bytes_in / 1e6:.2f} MB recibidos")
    try:
        await wait_stable(client, min_wait=0.5)
        i = 0
        while i < len(args):
            cmd = args[i]
            t = time.monotonic()
            if cmd == "shot":
                Path(args[i + 1]).parent.mkdir(parents=True, exist_ok=True)
                save_png(client.snapshot(), args[i + 1]); i += 2
            elif cmd == "zoom":
                x, y, w, h = map(int, args[i + 1:i + 5])
                zoom(client.snapshot(), x, y, w, h).save(args[i + 5]); i += 6
            elif cmd in ("click", "dclick", "rclick"):
                x, y = int(args[i + 1]), int(args[i + 2])
                if cmd == "click":
                    await client.click(x, y)
                elif cmd == "dclick":
                    await client.double_click(x, y)
                else:
                    await client.click(x, y, button="right")
                i += 3
            elif cmd == "type":
                await client.type_text(args[i + 1]); i += 2
            elif cmd == "key":
                await client.press(args[i + 1]); i += 2
            elif cmd == "scroll":
                await client.scroll(int(args[i + 1]), int(args[i + 2]), int(args[i + 3])); i += 4
            elif cmd == "wait":
                await asyncio.sleep(float(args[i + 1])); i += 2
            elif cmd == "stable":
                _, ok = await wait_stable(client)
                print(f"  estable={ok}")
                i += 1
            else:
                sys.exit(f"paso desconocido: {cmd}")
            print(f"{cmd:<7} {time.monotonic() - t:.2f}s · frames={client.frame_seq} "
                  f"· {client.bytes_in / 1e6:.2f} MB")
    finally:
        await client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run(sys.argv[1:]))
