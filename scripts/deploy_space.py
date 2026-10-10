"""Despliega el servicio en un Space de Hugging Face (SDK Docker).

1. Averigua el usuario a partir del token y crea el Space si no existe.
2. Carga los secretos (API_TOKEN, OPENROUTER_API_KEY) y las variables de
   configuración desde .env, sin imprimirlos.
3. Arma un paquete mínimo (app/, Dockerfile, requirements.txt y un README con
   la cabecera del Space) y lo sube con git. El repositorio de GitHub no cambia.
4. Espera a que el Space quede corriendo y prueba /health.

Lee de .env:
    HF_TOKEN=token con permiso de escritura (Settings → Access Tokens)
    HF_SPACE=usuario/nombre   (opcional; por defecto <usuario>/agente-sigecom)

    python scripts/deploy_space.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
HF = "https://huggingface.co"
FILES = ["Dockerfile", "requirements.txt", ".dockerignore"]
DIRS = ["app"]
SECRETS = ["API_TOKEN", "OPENROUTER_API_KEY"]
VARIABLES = {"MODEL_PRIMARY": "anthropic/claude-haiku-5.5", "MODEL_FALLBACK": "openai/gpt-6-luna",
             "TASK_DEADLINE_S": "270", "MAX_STEPS": "40"}

SPACE_README = """---
title: Agente SIGECOM
emoji: 🖥️
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# agente-SIGECOM

Servicio del agente que opera aplicaciones sin API (Frontier, Reto 1).
Código fuente: https://github.com/sgiraldo1-web/agente-SIGECOM

Endpoints: `GET /health`, `POST /tasks`, `GET /tasks/{task_id}` (con `Authorization: Bearer <token>`).
La consola web está en `/`.
"""


def git(cmd: list[str], cwd: Path, secret: str) -> None:
    res = subprocess.run(["git", *cmd], cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        out = (res.stdout + res.stderr).replace(secret, "***")
        sys.exit(f"falló git {cmd[0]}:\n{out}")


def main() -> None:
    env = dotenv_values(ROOT / ".env")
    token = (env.get("HF_TOKEN") or "").strip()
    if not token:
        sys.exit("Falta HF_TOKEN en .env (token de escritura de Hugging Face)")
    h = {"Authorization": f"Bearer {token}"}
    http = httpx.Client(timeout=60, headers=h)

    who = http.get(f"{HF}/api/whoami-v2")
    if who.status_code != 200:
        sys.exit(f"El token de Hugging Face no es válido ({who.status_code}).")
    user = who.json()["name"]
    space = (env.get("HF_SPACE") or "").strip() or f"{user}/agente-sigecom"
    owner, name = space.split("/", 1)
    print(f"usuario: {user} · Space: {space}")

    body = {"type": "space", "name": name, "private": False, "sdk": "docker"}
    if owner != user:
        body["organization"] = owner
    r = http.post(f"{HF}/api/repos/create", json=body)
    if r.status_code == 200:
        print("Space creado")
    elif r.status_code == 409:
        print("el Space ya existía")
    else:
        sys.exit(f"no se pudo crear el Space: {r.status_code} {r.text[:200]}")

    for key in SECRETS:
        value = (env.get(key) or "").strip()
        if not value:
            sys.exit(f"Falta {key} en .env")
        r = http.post(f"{HF}/api/spaces/{space}/secrets", json={"key": key, "value": value})
        print(f"secreto {key}: {'ok' if r.status_code == 200 else r.status_code}")
    for key, default in VARIABLES.items():
        value = (env.get(key) or "").strip() or default
        r = http.post(f"{HF}/api/spaces/{space}/variables", json={"key": key, "value": value})
        print(f"variable {key}={value}: {'ok' if r.status_code == 200 else r.status_code}")

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip() or "local"
    remote = f"https://{user}:{token}@huggingface.co/spaces/{space}"
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp)
        for f in FILES:
            shutil.copy2(ROOT / f, dst / f)
        for d in DIRS:
            shutil.copytree(ROOT / d, dst / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (dst / "README.md").write_text(SPACE_README, encoding="utf-8")
        # Los binarios (fuentes, logo) van por LFS/Xet en el Hub.
        (dst / ".gitattributes").write_text("*.woff2 filter=lfs diff=lfs merge=lfs -text\n"
                                            "*.png filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
        git(["init", "-q", "-b", "main"], dst, token)
        lfs = subprocess.run(["git", "lfs", "install", "--local"], cwd=dst, capture_output=True, text=True)
        if lfs.returncode != 0:
            print("aviso: git-lfs no está disponible; se intentará subir sin LFS")
            (dst / ".gitattributes").unlink()
        git(["add", "-A"], dst, token)
        git(["-c", "user.name=deploy", "-c", "user.email=deploy@localhost",
             "commit", "-q", "-m", f"Despliegue desde GitHub {commit}"], dst, token)
        git(["push", "-q", "--force", remote, "main"], dst, token)  # el Space es solo un artefacto
    print("código subido; el Space se está construyendo…")

    url = f"https://{space.replace('/', '-').replace('_', '-').replace('.', '-').lower()}.hf.space"
    t0 = time.monotonic()
    stage = ""
    while time.monotonic() - t0 < 900:
        time.sleep(15)
        try:
            stage = http.get(f"{HF}/api/spaces/{space}/runtime").json().get("stage", "")
        except Exception:
            continue
        print(f"  {int(time.monotonic() - t0)}s · {stage}")
        if stage in ("BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR"):
            sys.exit(f"el Space falló ({stage}); revisa los logs en {HF}/spaces/{space}")
        if stage == "RUNNING":
            try:
                if httpx.get(url + "/health", timeout=20).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
    print(f"\nSpace: {HF}/spaces/{space}\nServicio: {url}  (estado: {stage})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
