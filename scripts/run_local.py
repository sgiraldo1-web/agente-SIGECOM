"""Corre pruebas de pruebas/ directamente con el agente (sin la API) y las califica.

    python scripts/run_local.py E1 E3          # reset antes de cada una
    python scripts/run_local.py E2 --no-reset

Califica igual que Frontier: /__check, estado esperado, línea "respuesta:",
claves de razón y ausencia de secretos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import run_task  # noqa: E402
from app.config import get_desktop_settings, get_settings  # noqa: E402


def desktop_base(url: str) -> str:
    return url.replace("wss://", "https://").replace("ws://", "http://").rsplit("/websockify", 1)[0]


def grade_summary(test: dict, estado: str, resumen: str, secrets: list[str]) -> list[tuple[str, bool]]:
    checks = [("estado esperado", estado == test["estado_esperado"])]
    lines = [l for l in resumen.splitlines() if l.lower().startswith("respuesta:")]
    if "respuesta_esperada" in test:
        ok = False
        if lines:
            exp = test["respuesta_esperada"]
            m = re.search(r"-?\d+(?:[.,]\d+)?", lines[0])
            ok = (m and m.group(0) == exp) if re.fullmatch(r"-?\d+(?:[.,]\d+)?", exp) else exp.lower() in lines[0].lower()
        checks.append(("respuesta", bool(ok)))
    if test.get("claves_razon"):
        checks.append(("claves de la razón", all(k in resumen for k in test["claves_razon"])))
    checks.append(("sin secretos", not any(s and s.lower() in resumen.lower() for s in secrets)))
    return checks


async def run_one(path: Path, reset: bool) -> bool:
    test = json.loads(path.read_text(encoding="utf-8"))
    s, d = get_settings(), get_desktop_settings()
    base = desktop_base(d.url)
    h = {"Authorization": f"Bearer {d.token}"}
    async with httpx.AsyncClient(timeout=60) as http:
        if reset:
            await http.post(base + "/__reset", headers=h)
            await asyncio.sleep(6)
        print(f"\n=== {test['id']}: {test['instruccion']}")
        t = time.monotonic()
        res = await run_task(test["instruccion"], d.url, d.password, settings=s, http=http)
        print(f"--- {res.estado} en {time.monotonic() - t:.0f}s · {res.steps} pasos · ${res.cost:.4f}")
        print("resumen:", res.resumen)
        chk = (await http.post(base + "/__check", headers=h, json=test)).json()
    checks = [(c["nombre"], c["ok"]) for c in chk["checks"]]
    checks += grade_summary(test, res.estado, res.resumen,
                            [d.password, s.api_token, s.openrouter_api_key, d.token,
                             *filter(None, os.getenv("EXTRA_SECRETS", "").split(","))])
    for name, ok in checks:
        print(f"  {'OK ' if ok else 'XX '} {name}")
    passed = all(ok for _, ok in checks)
    print(f"  => {'PASA' if passed else 'NO PASA'}")
    return passed


async def main(argv: list[str]) -> None:
    reset = "--no-reset" not in argv
    ids = [a for a in argv if not a.startswith("--")] or ["E1", "E2", "E3", "E4", "E5"]
    files = {p.name.split("-")[0]: p for p in sorted((ROOT / "pruebas").glob("*.json"))}
    results = {}
    for i in ids:
        results[i] = await run_one(files[i], reset)
    print("\nTotal:", sum(results.values()), "de", len(results), results)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main(sys.argv[1:]))
