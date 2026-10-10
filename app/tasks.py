"""Gestor de tareas: estado en memoria con respaldo en SQLite, un worker por tarea.

Las tareas sobre un mismo escritorio corren una a la vez (dos agentes en la
misma sesión se pisarían). El plazo cuenta desde que llega la tarea.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

import httpx

from app.agent import TaskResult, run_task
from app.config import Settings
from app.screen import jpeg_bytes

log = logging.getLogger(__name__)

SAFETY_MARGIN_S = 15.0  # tope absoluto por encima del plazo interno del agente


@dataclass
class TaskRecord:
    id: str
    tarea: str
    created: float
    # estado y resumen se reemplazan juntos en una sola asignación
    result: tuple[str, str] = ("en_curso", "")
    events: list[dict] = field(default_factory=list)
    frame_jpeg: bytes | None = None
    worker: asyncio.Task | None = None

    @property
    def estado(self) -> str:
        return self.result[0]

    @property
    def resumen(self) -> str:
        return self.result[1]


class TaskManager:
    def __init__(self, settings: Settings, db_path: str | None = None):
        self.settings = settings
        self.tasks: dict[str, TaskRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._http: httpx.AsyncClient | None = None
        self.db_path = db_path or os.getenv("DB_PATH", "tasks.db")
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, tarea TEXT, "
                         "estado TEXT, resumen TEXT, created REAL)")
        self._db.commit()

    async def start(self) -> None:
        self._http = httpx.AsyncClient()

    async def stop(self) -> None:
        for rec in self.tasks.values():
            if rec.worker and not rec.worker.done():
                rec.worker.cancel()
        if self._http:
            await self._http.aclose()

    def _save(self, rec: TaskRecord) -> None:
        try:
            self._db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?)",
                             (rec.id, rec.tarea, rec.estado, rec.resumen, rec.created))
            self._db.commit()
        except sqlite3.Error as exc:
            log.warning("SQLite: %s", exc)

    def create(self, tarea: str, url: str, password: str) -> TaskRecord:
        rec = TaskRecord(id=uuid.uuid4().hex, tarea=tarea, created=time.monotonic())
        self.tasks[rec.id] = rec
        self._save(rec)
        rec.worker = asyncio.create_task(self._run(rec, url, password), name=f"task-{rec.id[:8]}")
        return rec

    def get(self, task_id: str) -> TaskRecord | None:
        rec = self.tasks.get(task_id)
        if rec is not None:
            return rec
        row = self._db.execute("SELECT id, tarea, estado, resumen FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        estado = row[2] if row[2] in ("terminada", "fallida") else "fallida"
        resumen = row[3] if row[2] in ("terminada", "fallida") else "La tarea se interrumpió por un reinicio del servicio."
        return TaskRecord(id=row[0], tarea=row[1], created=0, result=(estado, resumen))

    def _on_event(self, rec: TaskRecord, kind: str, data: dict) -> None:
        if kind == "captura":
            try:
                rec.frame_jpeg = jpeg_bytes(data["frame"], quality=60)
            except Exception:
                pass
            return
        rec.events.append({"tipo": kind, "t": round(time.monotonic() - rec.created, 1), **data})

    async def _run(self, rec: TaskRecord, url: str, password: str) -> None:
        lock = self._locks.setdefault(url, asyncio.Lock())
        result: TaskResult | None = None
        try:
            async with lock:
                budget = self.settings.task_deadline_s - (time.monotonic() - rec.created)
                if budget < 40:
                    rec.result = ("fallida", "No hubo tiempo para operar: el escritorio estaba ocupado con otra tarea.")
                    return
                settings = dataclasses.replace(self.settings, task_deadline_s=budget)
                result = await asyncio.wait_for(
                    run_task(rec.tarea, url, password, settings=settings, http=self._http,
                             on_event=lambda k, d: self._on_event(rec, k, d)),
                    timeout=budget + SAFETY_MARGIN_S)
                rec.result = (result.estado, result.resumen)
        except asyncio.TimeoutError:
            rec.result = ("fallida", "Se agotó el tiempo de la tarea y se detuvo la operación.")
        except asyncio.CancelledError:
            rec.result = ("fallida", "La tarea fue cancelada.")
            raise
        except Exception as exc:
            log.exception("worker falló")
            rec.result = ("fallida", f"Error interno del agente: {exc.__class__.__name__}.")
        finally:
            self._save(rec)
            log.info("tarea %s -> %s", rec.id[:8], rec.estado)
