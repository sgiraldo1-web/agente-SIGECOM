"""API del servicio: GET /health, POST /tasks, GET /tasks/{task_id}."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.config import get_desktop_settings, get_settings
from app.tasks import TaskManager

STATIC = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

settings = get_settings()
desktop_default = get_desktop_settings()  # opcional: escritorio por defecto para la interfaz
manager = TaskManager(settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(title="Agente que opera aplicaciones sin API", lifespan=lifespan)


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.api_token
    scheme, _, token = (authorization or "").partition(" ")
    if not expected or scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), expected):
        raise HTTPException(status_code=401, detail="token inválido")


class Escritorio(BaseModel):
    url: str = Field(min_length=1)
    password: str

    @field_validator("url")
    @classmethod
    def _ws_url(cls, v: str) -> str:
        """Acepta wss://…/websockify y también la dirección del noVNC (https://host/…)
        o solo el host, que se convierten a wss://host/websockify."""
        v = v.strip()
        if v.startswith(("ws://", "wss://")):
            return v
        if "://" not in v:
            v = "https://" + v
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("la dirección del escritorio debe ser wss://<host>/websockify")
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/websockify"

    def __repr__(self) -> str:  # nunca mostrar la contraseña
        return f"Escritorio(url={self.url!r})"

    __str__ = __repr__


class TaskIn(BaseModel):
    tarea: str = Field(min_length=1)
    escritorio: Escritorio


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/tasks", dependencies=[Depends(require_token)])
async def create_task(body: TaskIn) -> dict:
    rec = manager.create(body.tarea, body.escritorio.url, body.escritorio.password)
    return {"task_id": rec.id}


@app.get("/tasks/{task_id}", dependencies=[Depends(require_token)])
async def get_task(task_id: str) -> dict:
    rec = manager.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="tarea no encontrada")
    estado, resumen = rec.result
    return {"estado": estado, "resumen": resumen}


# ── Interfaz web (consola del asistente) ───────────────────────────────
# No forma parte del contrato que evalúa Frontier. Usa el mismo token.

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


class UITaskIn(BaseModel):
    tarea: str = Field(min_length=1)
    escritorio: Escritorio | None = None
    supervisado: bool = True
    crear_faltantes: bool = True


class UIAnswerIn(BaseModel):
    texto: str = Field(min_length=1, max_length=1000)


def _ui_record(task_id: str):
    rec = manager.tasks.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="tarea no encontrada")
    return rec


@app.get("/ui/config", dependencies=[Depends(require_token)])
async def ui_config() -> dict:
    has_default = bool(desktop_default.url and desktop_default.password)
    return {"escritorio_por_defecto": has_default,
            "escritorio": urlsplit(desktop_default.url).netloc if has_default else "",
            "max_pasos": settings.max_steps, "limite_s": settings.task_deadline_s}


@app.post("/ui/tasks", dependencies=[Depends(require_token)])
async def ui_create(body: UITaskIn) -> dict:
    if body.escritorio is not None:
        url, password = body.escritorio.url, body.escritorio.password
    elif desktop_default.url and desktop_default.password:
        url, password = desktop_default.url, desktop_default.password
    else:
        raise HTTPException(status_code=422, detail="falta el escritorio (url y contraseña)")
    rec = manager.create(body.tarea, url, password, origin="ui", supervised=body.supervisado,
                         create_missing=body.crear_faltantes)
    return {"task_id": rec.id}


@app.get("/ui/tasks", dependencies=[Depends(require_token)])
async def ui_list() -> list[dict]:
    return [{"id": r.id, "tarea": r.tarea, "estado": r.estado, "cancelada": r.cancelled,
             "creada": r.created_wall, "origen": r.origin} for r in manager.recent()]


@app.get("/ui/tasks/{task_id}", dependencies=[Depends(require_token)])
async def ui_get(task_id: str) -> dict:
    return manager.view(_ui_record(task_id))


@app.get("/ui/tasks/{task_id}/screen", dependencies=[Depends(require_token)])
async def ui_screen(task_id: str) -> Response:
    rec = _ui_record(task_id)
    if rec.frame_jpeg is None:
        return Response(status_code=204)
    return Response(rec.frame_jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/ui/tasks/{task_id}/approve", dependencies=[Depends(require_token)])
async def ui_approve(task_id: str) -> dict:
    _ui_record(task_id)
    return {"ok": manager.answer(task_id, True)}


@app.post("/ui/tasks/{task_id}/answer", dependencies=[Depends(require_token)])
async def ui_answer(task_id: str, body: UIAnswerIn) -> dict:
    _ui_record(task_id)
    return {"ok": manager.answer(task_id, body.texto.strip())}


@app.post("/ui/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
async def ui_cancel(task_id: str) -> dict:
    _ui_record(task_id)
    return {"ok": manager.cancel(task_id)}
