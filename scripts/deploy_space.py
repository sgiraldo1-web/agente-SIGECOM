"""Despliega el servicio en un Space de Hugging Face (SDK Docker).

Arma un paquete mínimo (app/, Dockerfile, requirements.txt y un README con la
cabecera del Space) en una carpeta temporal y lo sube con git al repositorio del
Space. El repositorio de GitHub no cambia.

Lee de .env:
    HF_SPACE=usuario/nombre-del-space
    HF_TOKEN=token con permiso de escritura (Settings → Access Tokens)

    python scripts/deploy_space.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
FILES = ["Dockerfile", "requirements.txt", ".dockerignore"]
DIRS = ["app"]

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
"""


def run(cmd: list[str], cwd: Path, secret: str) -> None:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    out = (res.stdout + res.stderr).replace(secret, "***") if secret else res.stdout + res.stderr
    if res.returncode != 0:
        safe = " ".join(c.replace(secret, "***") for c in cmd)
        sys.exit(f"falló: {safe}\n{out}")
    if out.strip():
        print(out.strip())


def main() -> None:
    env = dotenv_values(ROOT / ".env")
    space, token = (env.get("HF_SPACE") or "").strip(), (env.get("HF_TOKEN") or "").strip()
    if not space or "/" not in space or not token:
        sys.exit("Faltan HF_SPACE (usuario/nombre) o HF_TOKEN en .env")
    user = space.split("/")[0]
    remote = f"https://{user}:{token}@huggingface.co/spaces/{space}"

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip() or "local"
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp)
        for f in FILES:
            shutil.copy2(ROOT / f, dst / f)
        for d in DIRS:
            shutil.copytree(ROOT / d, dst / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (dst / "README.md").write_text(SPACE_README, encoding="utf-8")

        run(["git", "init", "-q", "-b", "main"], dst, token)
        run(["git", "add", "-A"], dst, token)
        run(["git", "-c", "user.name=deploy", "-c", "user.email=deploy@localhost",
             "commit", "-q", "-m", f"Despliegue desde GitHub {commit}"], dst, token)
        # El Space es solo un artefacto de despliegue: se reemplaza completo.
        run(["git", "push", "-q", "--force", remote, "main"], dst, token)
    print(f"Subido a https://huggingface.co/spaces/{space}")
    print(f"URL del servicio: https://{space.replace('/', '-').replace('_', '-').lower()}.hf.space")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
