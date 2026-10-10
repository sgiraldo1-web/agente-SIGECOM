"""Guardias deterministas entre el modelo y el mouse.

El texto en pantalla puede engañar al modelo, así que estos bloqueos no
dependen del prompt.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.planner import Decision

DESTRUCTIVE_WORDS = ("elimin", "borr", "anul", "revert", "revier", "suprim", "remov",
                     "delete", "remove")
CONFIG_WORDS = ("configuraci", "integraci", "clave", "credencial", "api key", "token", "contrasen")
# Verbo de confirmación -> clave para detectar repeticiones de una acción que cambia datos.
COMMIT_WORDS = {"export": "exportar", "guard": "guardar", "grab": "guardar",
                "confirm": "confirmar", "registr": "guardar", "acept": "confirmar",
                "envi": "confirmar", "aplic": "confirmar", "crear": "guardar"}
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
    notes: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)  # claves de acciones que cambiaron datos
    data_actions: int = 0
    blocked: int = 0

    @property
    def task_plain(self) -> str:
        return plain(self.tarea)

    @property
    def allows_destructive(self) -> bool:
        return any(w in self.task_plain for w in DESTRUCTIVE_WORDS)

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


def check(decision: Decision, state: AgentState) -> GuardResult:
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
            if not decision.reintento_justificado:
                return GuardResult(False, [], riesgo,
                                   f"BLOQUEADO: ya ejecutaste una acción '{key}'. Antes de repetirla comprueba en "
                                   "pantalla que la anterior no tuvo efecto y explica en 'reintento_justificado'.")
        if state.clasificacion in ("crear", "actualizar") and not state.notes:
            return GuardResult(False, [], riesgo,
                               "BLOQUEADO: antes de guardar registra con note lo que encontraste en la búsqueda "
                               "previa (si el registro ya existe, o el identificador exacto del registro a modificar).")
    return GuardResult(True, actions, riesgo)


def commit(decision_intent: str, state: AgentState) -> None:
    state.commits.append(_commit_key(decision_intent) or "cambio")
    state.data_actions += 1
