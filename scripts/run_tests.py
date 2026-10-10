"""Corre las pruebas contra el servicio desplegado, igual que el evaluador de Frontier.

    SERVICE_URL=https://<servicio> python scripts/run_tests.py E1 E3

Por prueba: /__reset del escritorio → POST /tasks → GET /tasks/{id} cada 5 s
(máximo 300 s) → /__check y reglas del resumen.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_desktop_settings, get_settings  # noqa: E402
from scripts.run_local import desktop_base, grade_summary  # noqa: E402


async def run_one(http: httpx.AsyncClient, service: str, path: Path) -> bool:
    test = json.loads(path.read_text(encoding="utf-8"))
    s, d = get_settings(), get_desktop_settings()
    base = desktop_base(d.url)
    dh = {"Authorization": f"Bearer {d.token}"}
    sh = {"Authorization": f"Bearer {s.api_token}"}
    await http.post(base + "/__reset", headers=dh)
    await asyncio.sleep(6)
    print(f"\n=== {test['id']}: {test['instruccion']}")
    t0 = time.monotonic()
    r = await http.post(service + "/tasks", headers=sh,
                        json={"tarea": test["instruccion"], "escritorio": {"url": d.url, "password": d.password}})
    print(f"POST /tasks {r.status_code} en {(time.monotonic() - t0) * 1000:.0f} ms")
    task_id = r.json()["task_id"]
    estado, resumen = "en_curso", ""
    while time.monotonic() - t0 < 300:
        await asyncio.sleep(5)
        j = (await http.get(f"{service}/tasks/{task_id}", headers=sh)).json()
        estado, resumen = j["estado"], j["resumen"]
        if estado != "en_curso":
            break
    print(f"--- {estado} en {time.monotonic() - t0:.0f}s")
    print("resumen:", resumen)
    chk = (await http.post(base + "/__check", headers=dh, json=test)).json()
    checks = [(c["nombre"], c["ok"]) for c in chk["checks"]]
    checks += grade_summary(test, estado, resumen,
                            [d.password, s.api_token, s.openrouter_api_key, d.token,
                             *filter(None, os.getenv("EXTRA_SECRETS", "").split(","))])
    for name, ok in checks:
        print(f"  {'OK ' if ok else 'XX '} {name}")
    passed = all(ok for _, ok in checks)
    print(f"  => {'PASA' if passed else 'NO PASA'}")
    return passed


async def main(argv: list[str]) -> None:
    service = os.getenv("SERVICE_URL", "").rstrip("/")
    if not service:
        sys.exit("Falta SERVICE_URL")
    ids = argv or ["E1", "E2", "E3", "E4", "E5"]
    files = {p.name.split("-")[0]: p for p in sorted((ROOT / "pruebas").glob("*.json"))}
    async with httpx.AsyncClient(timeout=60) as http:
        print("health:", (await http.get(service + "/health")).status_code)
        results = {i: await run_one(http, service, files[i]) for i in ids}
    print("\nTotal:", sum(results.values()), "de", len(results), results)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(sys.argv[1:]))
