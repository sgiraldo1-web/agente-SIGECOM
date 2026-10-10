"""API del servicio: GET /health, POST /tasks, GET /tasks/{task_id}."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.tasks import TaskManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

settings = get_settings()
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
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("la url debe empezar con ws:// o wss://")
        return v

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
