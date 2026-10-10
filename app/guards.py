"""Guardias deterministas entre el modelo y el mouse.

El texto en pantalla puede engañar al modelo, así que estos bloqueos no
dependen del prompt.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np

from app.planner import Decision

# Fracción de píxeles distintos por debajo de la cual dos pantallas son "la misma".
SAME_SCREEN_FRACTION = 0.02

DESTRUCTIVE_WORDS = ("elimin", "borr", "anul", "revert", "revier", "suprim", "remov",
                     "delete", "remove")
CONFIG_WORDS = ("configuraci", "integraci", "clave", "credencial", "api key", "token", "contrasen")
# Verbo de confirmación -> clave para detectar repeticiones de una acción que cambia datos.
COMMIT_WORDS = {"export": "exportar", "guard": "guardar", "grab": "guardar",
                "confirm": "confirmar", "registr": "guardar", "acept": "confirmar",
                "envi": "confirmar", "aplic": "confirmar", "crear": "guardar"}
# Palabras en la tarea que autorizan cambiar el estado de un registro (cancelar una cita,
# reactivarla). Una cita cancelada por pedido explícito no es una eliminación indebida.
STATE_CHANGE_WORDS = DESTRUCTIVE_WORDS + ("cancel", "reactiv", "desactiv", "inactiv", "reabr", "restaur",
                                         "como estaba", "deshac", "deshaz", "devuelv", "devolv",
                                         "valor anterior", "por error")
DATA_KINDS = {"crear", "actualizar", "exportar", "eliminar", "otra"}


def plain(text: str) -> str:
    """Minúsculas sin tildes, para buscar palabras clave."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# "sin guardar", "antes de exportar", "no eliminar"... no son la acción misma.
_NEGATED = re.compile(r"\b(?:sin|no|nunca|ni|antes de|evitando|evitar|para luego|luego|despues de)\s+(?:\w+\s+)?\w+")
# La intención de presionar un botón que confirma cambios: "clic en Guardar", "presionar Exportar".
_PRESS_COMMIT = re.compile(
    r"\b(?:clic|click|presion\w*|puls\w*|oprim\w*|toc\w*)\s+(?:en\s+)?(?:el\s+)?(?:boton\s+)?"
    r"[\"'«]?(export|guard|grab|confirm|acept|registr|crear|envi|aplic)")


def intent_words(intent: str) -> str:
    return _NEGATED.sub(" ", plain(intent))


@dataclass
class AgentState:
    tarea: str
    clasificacion: str | None = None
    plan: str = ""
    notes: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)  # claves de acciones que cambiaron datos
    commit_frames: dict[str, list[np.ndarray]] = field(default_factory=dict)
    data_actions: int = 0
    blocked: int = 0

    @property
    def task_plain(self) -> str:
        return plain(self.tarea)

    @property
    def allows_destructive(self) -> bool:
        return any(w in self.task_plain for w in STATE_CHANGE_WORDS)

    @property
    def allows_config(self) -> bool:
        return any(w in self.task_plain for w in CONFIG_WORDS)


@dataclass
class GuardResult:
    ok: bool
    actions: list[dict]
    riesgo: str
    message: str = ""


def _commit_key(text: str) -> str | None:
    m = _PRESS_COMMIT.search(plain(text))
    if m:
        return COMMIT_WORDS.get(m.group(1), "cambio")
    p = intent_words(text)
    for stem, key in COMMIT_WORDS.items():
        if stem in p:
            return key
    return None


def _thumb(frame: np.ndarray) -> np.ndarray:
    return frame[::4, ::4]


def _same_screen(a: np.ndarray, b: np.ndarray) -> bool:
    if a.shape != b.shape:
        return False
    return np.count_nonzero(np.any(a != b, axis=2)) < SAME_SCREEN_FRACTION * a.shape[0] * a.shape[1]


def check(decision: Decision, state: AgentState, frame: np.ndarray | None = None) -> GuardResult:
    intent = intent_words(decision.intencion)
    riesgo = decision.riesgo
    actions = decision.acciones

    # El riesgo declarado se corrige hacia arriba según la intención.
    if any(w in intent for w in DESTRUCTIVE_WORDS):
        riesgo = "destructivo"
    elif riesgo == "bajo" and _PRESS_COMMIT.search(plain(decision.intencion)):
        riesgo = "cambia_datos"

    if riesgo == "destructivo" and not state.allows_destructive:
        return GuardResult(False, [], riesgo,
                           "BLOQUEADO: la acción parece eliminar, anular o revertir datos y la tarea no lo pide. "
                           "No lo intentes de nuevo; sigue solo con lo que pide la tarea.")

    if not state.allows_config and any(w in intent for w in CONFIG_WORDS):
        return GuardResult(False, [], riesgo,
                           "BLOQUEADO: la tarea no requiere abrir configuración, integraciones ni claves.")

    if riesgo != "bajo":
        if len(actions) > 1:
            actions = actions[:1]  # solo una acción que cambia datos por paso
        key = _commit_key(decision.intencion) or "cambio"
        if key in state.commits:
            if key == "exportar":
                return GuardResult(False, [], riesgo,
                                   "BLOQUEADO: ya exportaste una vez. Exportar de nuevo crea un reporte de más. "
                                   "Comprueba el resultado en la lista de reportes y termina.")
            # Un segundo guardado es legítimo si es otro formulario (por ejemplo crear el
            # cliente y luego la venta). Se bloquea si la pantalla es la misma del anterior.
            previous = state.commit_frames.get(key, [])
            same = frame is None or any(_same_screen(_thumb(frame), p) for p in previous)
            if same and not decision.reintento_justificado:
                return GuardResult(False, [], riesgo,
                                   f"BLOQUEADO: ya ejecutaste una acción '{key}' en esta misma pantalla. Antes de "
                                   "repetirla comprueba que la anterior no tuvo efecto y explica en 'reintento_justificado'.")
        if state.clasificacion in ("crear", "actualizar") and not state.notes:
            return GuardResult(False, [], riesgo,
                               "BLOQUEADO: antes de guardar registra con note lo que encontraste en la búsqueda "
                               "previa (si el registro ya existe, o el identificador exacto del registro a modificar).")
    return GuardResult(True, actions, riesgo)


def commit(decision_intent: str, state: AgentState, frame: np.ndarray | None = None) -> None:
    key = _commit_key(decision_intent) or "cambio"
    state.commits.append(key)
    if frame is not None:
        state.commit_frames.setdefault(key, []).append(_thumb(frame))
    state.data_actions += 1
