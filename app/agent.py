"""Bucle del agente: observar, decidir, validar, actuar, esperar, comprobar."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from app import guards, summary
from app.config import Settings
from app.guards import AgentState
from app.llm import LLM, LLMError
from app.planner import Decision, Planner
from app.rfb import RFBClient
from app.screen import diff_pixels, jpeg_data_url, wait_stable, zoom
from app.verifier import verify

log = logging.getLogger(__name__)

VERIFY_RESERVE_S = 20.0
MIN_STEP_S = 8.0
CHANGE_TOLERANCE = 200

EventFn = Callable[[str, dict], None]
ApproverFn = Callable[[Decision], Awaitable[bool]]
AskerFn = Callable[[str], Awaitable[str | None]]


@dataclass
class TaskResult:
    estado: str  # "terminada" | "fallida"
    resumen: str
    steps: int = 0
    seconds: float = 0.0
    cost: float = 0.0
    log: list[str] = field(default_factory=list)


def _describe(a: dict) -> str:
    t = a["tipo"]
    if t in ("click", "double_click", "right_click"):
        return f"{t}({a['x']},{a['y']})"
    if t == "type":
        where = f" en ({a['x']},{a['y']})" if "x" in a else ""
        return f"type({a['texto']!r}{where})"
    if t == "key":
        return f"key({a['tecla']})"
    if t == "scroll":
        return f"scroll({a['x']},{a['y']},{a['dy']})"
    if t == "drag":
        return f"drag({a['x1']},{a['y1']}→{a['x2']},{a['y2']})"
    if t == "zoom":
        return f"zoom({a['x']},{a['y']},{a['w']}x{a['h']})"
    if t == "wait":
        return f"wait({a['s']})"
    if t == "note":
        return f"note({a['texto'][:80]!r})"
    return t


async def _execute(client: RFBClient, a: dict) -> None:
    t = a["tipo"]
    if t == "click":
        await client.click(a["x"], a["y"])
    elif t == "double_click":
        await client.double_click(a["x"], a["y"])
    elif t == "right_click":
        await client.click(a["x"], a["y"], button="right")
    elif t == "type":
        if "x" in a:
            await client.click(a["x"], a["y"])
            await wait_stable(client, min_wait=0.4, max_wait=1.5, interval=0.2)
        await client.press("ctrl+a")
        await asyncio.sleep(0.1)
        await client.type_text(a["texto"])
    elif t == "key":
        await client.press(a["tecla"])
    elif t == "scroll":
        await client.scroll(a["x"], a["y"], a["dy"])
    elif t == "drag":
        await client.drag(a["x1"], a["y1"], a["x2"], a["y2"])
    elif t == "wait":
        await asyncio.sleep(a["s"])


async def run_task(tarea: str, url: str, password: str, *, settings: Settings,
                   on_event: EventFn | None = None, approver: ApproverFn | None = None,
                   asker: AskerFn | None = None, extra_rules: str | None = None,
                   http: httpx.AsyncClient | None = None) -> TaskResult:
    """Ejecuta una tarea completa. Nunca lanza: siempre devuelve un TaskResult."""
    t0 = time.monotonic()
    deadline = t0 + settings.task_deadline_s
    secrets = [password, settings.api_token, settings.openrouter_api_key]
    emit = on_event or (lambda kind, data: None)
    own_http = http is None
    http = http or httpx.AsyncClient()
    llm = LLM(settings.openrouter_api_key, settings.model_primary, settings.model_fallback, http)
    planner = Planner(llm)
    state = AgentState(tarea=tarea)
    client = RFBClient(url, password)
    step = 0

    def finish(estado: str, resumen: str) -> TaskResult:
        res = TaskResult(estado, resumen, step, time.monotonic() - t0, llm.stats.cost, state.history)
        emit("fin", {"estado": estado, "resumen": resumen})
        log.info("tarea %s en %.0fs, %d pasos, $%.4f", estado, res.seconds, step, res.cost)
        return res

    try:
        try:
            await client.connect()
        except Exception as exc:
            log.warning("conexión fallida, reintento: %s", exc.__class__.__name__)
            try:
                await client.reconnect()
            except Exception:
                return finish("fallida", "No se pudo conectar al escritorio remoto.")

        frame, _ = await wait_stable(client, min_wait=0.5, max_wait=2.0)
        emit("captura", {"frame": frame})
        feedback: list[str] = []
        zoom_url: str | None = None
        verify_retries = 0
        commit_shots: list[str] = []

        while True:
            step += 1
            remaining = deadline - time.monotonic()
            if step > settings.max_steps or remaining < VERIFY_RESERVE_S + MIN_STEP_S:
                return finish("fallida", summary.build_fail(
                    "Se agotó el tiempo o el número de pasos sin completar la tarea. "
                    f"Acciones que cambiaron datos: {state.data_actions}.",
                    [], secrets))

            if not client.connected:
                try:
                    await client.reconnect()
                    frame, _ = await wait_stable(client, min_wait=0.5, max_wait=2.0)
                except Exception:
                    return finish("fallida", "Se perdió la conexión con el escritorio remoto.")

            try:
                decision = await planner.decide(
                    tarea=tarea, step=step, max_steps=settings.max_steps, remaining_s=remaining,
                    clasificacion=state.clasificacion, notes=state.notes, history=state.history,
                    feedback=feedback, screenshot_url=jpeg_data_url(frame), zoom_url=zoom_url,
                    timeout=min(35.0, remaining - VERIFY_RESERVE_S), extra_rules=extra_rules)
            except LLMError as exc:
                log.warning("planificador sin respuesta: %s", exc)
                state.history.append(f"{step}. (el modelo no respondió)")
                feedback = ["El paso anterior no obtuvo respuesta válida; responde solo el JSON."]
                continue
            feedback, zoom_url = [], None
            if state.clasificacion is None and decision.clasificacion:
                state.clasificacion = decision.clasificacion
            log.info("paso %d [%s] %s · %s · %s", step, decision.model, decision.riesgo,
                     decision.intencion, ", ".join(_describe(a) for a in decision.acciones))
            emit("paso", {"n": step, "intencion": decision.intencion, "riesgo": decision.riesgo,
                          "acciones": [_describe(a) for a in decision.acciones],
                          "puntos": [[a["x"], a["y"]] for a in decision.acciones
                                     if "x" in a and a["tipo"] != "zoom"]})

            question = decision.question
            if question is not None:
                if asker is None:  # por la API nadie puede responder: no se puede completar
                    state.history.append(f"{step}. sin datos suficientes: {question}")
                    return finish("fallida", summary.build_fail(
                        f"Faltan datos para completar la tarea: {question}", [], secrets))
                answer = await asker(question)
                if not answer:
                    return finish("fallida", summary.build_fail(
                        f"La tarea quedó sin completar: no hubo respuesta a la pregunta «{question}».", [], secrets))
                state.notes.append(f"El usuario respondió a «{question}»: {answer}")
                state.history.append(f"{step}. preguntó: {question} → respuesta: {answer}")
                continue

            term = decision.terminal
            if term and term["tipo"] == "fail":
                state.history.append(f"{step}. fail: {term['razon']}")
                return finish("fallida", summary.build_fail(term["razon"], term["datos"], secrets))

            if term and term["tipo"] == "done":
                claim = term["resumen"] + (f" (respuesta: {term['respuesta']})" if term["respuesta"] else "")
                needs_check = state.data_actions > 0 or state.clasificacion in guards.DATA_KINDS
                if needs_check:
                    frame, _ = await wait_stable(client, min_wait=0.3, max_wait=2.0)
                    ok, motivo = await verify(
                        llm, tarea=tarea, history=state.history, notes=state.notes, claim=claim,
                        screenshot_url=jpeg_data_url(frame), commit_shots=commit_shots,
                        timeout=max(5.0, min(25.0, deadline - time.monotonic() - 3)))
                    log.info("verificador: %s · %s", ok, motivo)
                    emit("verificacion", {"ok": ok, "motivo": motivo})
                    if not ok:
                        state.history.append(f"{step}. done rechazado por el verificador: {motivo}")
                        if verify_retries == 0 and deadline - time.monotonic() > 60:
                            verify_retries += 1
                            feedback = [f"El verificador no encontró evidencia de que la tarea esté cumplida: "
                                        f"{motivo}. Deja en pantalla la evidencia (sin repetir acciones que "
                                        "cambian datos) o usa fail si no se cumplió."]
                            continue
                        return finish("fallida", summary.build_fail(
                            f"No se pudo confirmar que la tarea quedó cumplida: {motivo}. "
                            f"Lo que reportó el asistente: {term['resumen']}", [], secrets))
                return finish("terminada", summary.build_done(term["resumen"], term["respuesta"], secrets))

            guard = guards.check(decision, state, frame)
            if not guard.ok:
                state.blocked += 1
                state.history.append(f"{step}. {decision.intencion} → {guard.message}")
                feedback = [guard.message]
                log.info("guardia: %s", guard.message)
                emit("bloqueo", {"n": step, "mensaje": guard.message})
                continue

            if approver is not None and guard.riesgo != "bajo":
                if not await approver(decision):
                    return finish("fallida", "Tarea cancelada por el usuario antes de: "
                                  + summary.redact(decision.intencion, secrets))

            before = frame
            if guard.riesgo != "bajo":
                guards.commit(decision.intencion, state, frame)
            touched_screen = False
            for i, a in enumerate(guard.actions):
                if a["tipo"] == "note":
                    state.notes.append(a["texto"])
                elif a["tipo"] == "zoom":
                    zoom_url = jpeg_data_url(zoom(client.snapshot(), a["x"], a["y"], a["w"], a["h"]))
                else:
                    if touched_screen:
                        await wait_stable(client, min_wait=0.5, max_wait=2.0, interval=0.25)
                    await _execute(client, a)
                    touched_screen = True
            if touched_screen:
                frame, _ = await wait_stable(client)
            else:
                frame = client.snapshot()
            emit("captura", {"frame": frame})
            if guard.riesgo != "bajo":
                commit_shots.append(jpeg_data_url(frame))
            changed = diff_pixels(before, frame) > CHANGE_TOLERANCE
            acts = ", ".join(_describe(a) for a in guard.actions)
            result = "" if not touched_screen else (" → la pantalla cambió" if changed
                                                    else " → LA PANTALLA NO CAMBIÓ")
            state.history.append(f"{step}. {decision.intencion} · {acts}{result}")
    except Exception as exc:  # cualquier error inesperado termina la tarea limpia
        log.exception("error en la tarea")
        return finish("fallida", summary.redact(f"Error interno del agente: {exc.__class__.__name__}.", secrets))
    finally:
        await client.close()
        if own_http:
            await http.aclose()
