"""Gestor de tareas: estado en memoria con respaldo en SQLite, un worker por tarea.

Las tareas sobre un mismo escritorio corren una a la vez (dos agentes en la
misma sesión se pisarían). El plazo cuenta desde que llega la tarea.

Las tareas lanzadas desde la interfaz pueden ir en modo supervisado: antes de
una acción que cambia datos el agente se pausa y espera aprobación. Las que
llegan por la API nunca se pausan.
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
from urllib.parse import urlsplit

import httpx

from app.agent import TaskResult, run_task
from app.config import Settings
from app.planner import Decision
from app.screen import jpeg_bytes
from app.summary import redact

log = logging.getLogger(__name__)

SAFETY_MARGIN_S = 15.0  # tope absoluto por encima del plazo interno del agente
APPROVAL_TIMEOUT_S = 180.0
SUPERVISED_EXTRA_S = 300.0  # las pausas de aprobación o preguntas no deben comerse el plazo
QUESTION_TIMEOUT_S = 300.0

# Reglas extra para tareas lanzadas desde el panel: hay una persona que puede responder.
ASSISTED_RULES = """Esta tarea la pidió una persona desde el panel y puede responderte.
- Si te falta un dato necesario o la tarea es ambigua (por ejemplo, varios registros con el mismo nombre), usa la acción {"tipo":"preguntar","pregunta":"..."} (va sola) en lugar de fallar. Haz una sola pregunta clara con todo lo que necesitas. La respuesta aparecerá en las notas.
- Nunca inventes datos (NIT, cédula, teléfono, ciudad, valores, fechas): pregúntalos."""

CREATE_MISSING_RULE = """
- Si la tarea necesita un registro relacionado que no existe (por ejemplo, el cliente de una venta o de un pedido), primero confirma que no existe buscando por nombre y variantes. Luego créalo: pregunta en una sola pregunta los datos obligatorios que falten. Guárdalo una vez, confírmalo y después continúa con la tarea original."""



@dataclass
class TaskRecord:
    id: str
    tarea: str
    created: float
    created_wall: float = field(default_factory=time.time)
    origin: str = "api"
    supervised: bool = False
    create_missing: bool = False
    desktop_host: str = ""
    deadline_s: float = 0.0
    # estado y resumen se reemplazan juntos en una sola asignación
    result: tuple[str, str] = ("en_curso", "")
    events: list[dict] = field(default_factory=list)
    frame_jpeg: bytes | None = None
    frame_seq: int = 0
    pending: dict | None = None
    cancelled: bool = False
    started: float | None = None
    worker: asyncio.Task | None = None
    _approval: asyncio.Future | None = None

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
                             (rec.id, rec.tarea, rec.estado, rec.resumen, rec.created_wall))
            self._db.commit()
        except sqlite3.Error as exc:
            log.warning("SQLite: %s", exc)

    def create(self, tarea: str, url: str, password: str, *, origin: str = "api",
               supervised: bool = False, create_missing: bool = False) -> TaskRecord:
        # Por la interfaz hay pausas (aprobación, preguntas) que no deben comerse el plazo.
        deadline = self.settings.task_deadline_s + (SUPERVISED_EXTRA_S if origin == "ui" else 0)
        rec = TaskRecord(id=uuid.uuid4().hex, tarea=tarea, created=time.monotonic(), origin=origin,
                         supervised=supervised, create_missing=create_missing,
                         desktop_host=urlsplit(url).netloc, deadline_s=deadline)
        self.tasks[rec.id] = rec
        self._save(rec)
        rec.worker = asyncio.create_task(self._run(rec, url, password), name=f"task-{rec.id[:8]}")
        return rec

    def get(self, task_id: str) -> TaskRecord | None:
        rec = self.tasks.get(task_id)
        if rec is not None:
            return rec
        row = self._db.execute("SELECT id, tarea, estado, resumen, created FROM tasks WHERE id=?",
                               (task_id,)).fetchone()
        if row is None:
            return None
        finished = row[2] in ("terminada", "fallida")
        estado = row[2] if finished else "fallida"
        resumen = row[3] if finished else "La tarea se interrumpió por un reinicio del servicio."
        return TaskRecord(id=row[0], tarea=row[1], created=0, created_wall=row[4] or 0,
                          result=(estado, resumen))

    def recent(self, limit: int = 8) -> list[TaskRecord]:
        return sorted(self.tasks.values(), key=lambda r: r.created_wall, reverse=True)[:limit]

    # ── interfaz: aprobación y cancelación ─────────────────────────────

    def answer(self, task_id: str, value) -> bool:
        """Responde la pausa pendiente: True/False para una aprobación, texto para una pregunta."""
        rec = self.tasks.get(task_id)
        if rec is None or rec._approval is None or rec._approval.done():
            return False
        rec._approval.set_result(value)
        return True

    def cancel(self, task_id: str) -> bool:
        rec = self.tasks.get(task_id)
        if rec is None or rec.estado != "en_curso":
            return False
        rec.cancelled = True
        if rec._approval is not None and not rec._approval.done():
            rec._approval.set_result(None if (rec.pending or {}).get("tipo") == "pregunta" else False)
        elif rec.worker and not rec.worker.done():
            rec.worker.cancel()
        return True

    def _approver(self, rec: TaskRecord):
        async def approver(decision: Decision) -> bool:
            rec._approval = asyncio.get_running_loop().create_future()
            rec.pending = {"tipo": "aprobacion", "intencion": decision.intencion,
                           "riesgo": decision.riesgo, "observacion": decision.observacion}
            self._on_event(rec, "aprobacion", dict(rec.pending))
            try:
                ok = await asyncio.wait_for(rec._approval, APPROVAL_TIMEOUT_S)
            except asyncio.TimeoutError:
                ok = False
            finally:
                rec.pending = None
                rec._approval = None
            self._on_event(rec, "respuesta", {"aprobado": ok})
            if not ok:
                rec.cancelled = True
            return ok
        return approver

    def _asker(self, rec: TaskRecord):
        async def asker(question: str) -> str | None:
            rec._approval = asyncio.get_running_loop().create_future()
            rec.pending = {"tipo": "pregunta", "pregunta": question}
            self._on_event(rec, "pregunta", {"pregunta": question})
            try:
                answer = await asyncio.wait_for(rec._approval, QUESTION_TIMEOUT_S)
            except asyncio.TimeoutError:
                answer = None
            finally:
                rec.pending = None
                rec._approval = None
            answer = (answer or "").strip() if isinstance(answer, str) else None
            self._on_event(rec, "respuesta", {"texto": answer or ""})
            if not answer:
                rec.cancelled = True
            return answer or None
        return asker

    def _rules(self, rec: TaskRecord) -> str | None:
        if rec.origin != "ui":
            return None  # por la API el agente es estricto: no pregunta ni crea registros no pedidos
        return ASSISTED_RULES + (CREATE_MISSING_RULE if rec.create_missing else "")

    # ── ejecución ───────────────────────────────────────────────────────

    def _on_event(self, rec: TaskRecord, kind: str, data: dict) -> None:
        if kind == "captura":
            try:
                rec.frame_jpeg = jpeg_bytes(data["frame"], quality=70)
                rec.frame_seq += 1
            except Exception:
                pass
            return
        rec.events.append({"tipo": kind, "t": round(time.monotonic() - rec.created, 1), **data})

    async def _run(self, rec: TaskRecord, url: str, password: str) -> None:
        lock = self._locks.setdefault(url, asyncio.Lock())
        secrets = [password, self.settings.api_token, self.settings.openrouter_api_key]
        result: TaskResult | None = None
        try:
            async with lock:
                rec.started = time.monotonic()
                budget = rec.deadline_s - (rec.started - rec.created)
                if budget < 40:
                    rec.result = ("fallida", "No hubo tiempo para operar: el escritorio estaba ocupado con otra tarea.")
                    return
                settings = dataclasses.replace(self.settings, task_deadline_s=budget)
                result = await asyncio.wait_for(
                    run_task(rec.tarea, url, password, settings=settings, http=self._http,
                             on_event=lambda k, d: self._on_event(rec, k, d),
                             approver=self._approver(rec) if rec.supervised else None,
                             asker=self._asker(rec) if rec.origin == "ui" else None,
                             extra_rules=self._rules(rec)),
                    timeout=budget + SAFETY_MARGIN_S)
                if rec.cancelled:
                    rec.result = ("fallida", "Cancelaste la tarea. " + result.resumen)
                else:
                    rec.result = (result.estado, result.resumen)
        except asyncio.TimeoutError:
            rec.result = ("fallida", "Se agotó el tiempo de la tarea y se detuvo la operación.")
        except asyncio.CancelledError:
            rec.result = ("fallida", "Cancelaste la tarea; el asistente dejó de operar.")
            if not rec.cancelled:  # apagado del servicio: propagar
                raise
        except Exception as exc:
            log.exception("worker falló")
            rec.result = ("fallida", redact(f"Error interno del agente: {exc.__class__.__name__}.", secrets))
        finally:
            self._save(rec)
            log.info("tarea %s -> %s", rec.id[:8], rec.estado)

    # ── vista para la interfaz ──────────────────────────────────────────

    def view(self, rec: TaskRecord) -> dict:
        now = time.monotonic()
        elapsed = (now - rec.started) if rec.started else 0.0
        pasos = [e for e in rec.events if e["tipo"] == "paso"]
        return {
            "id": rec.id,
            "tarea": rec.tarea,
            "estado": rec.estado,
            "resumen": rec.resumen,
            "cancelada": rec.cancelled,
            "supervisada": rec.supervised,
            "crear_faltantes": rec.create_missing,
            "origen": rec.origin,
            "escritorio": rec.desktop_host,
            "en_espera": rec.started is None and rec.estado == "en_curso",
            "aprobacion": rec.pending,
            "segundos": round(elapsed),
            "limite": round(rec.deadline_s),
            "max_pasos": self.settings.max_steps,
            "pasos": [{"n": e["n"], "intencion": e["intencion"], "riesgo": e["riesgo"],
                       "acciones": e["acciones"], "puntos": e.get("puntos", [])} for e in pasos],
            "eventos": [e for e in rec.events if e["tipo"] in ("bloqueo", "verificacion", "aprobacion",
                                                               "pregunta", "respuesta")][-20:],
            "frame": rec.frame_seq,
            "creada": rec.created_wall,
        }
