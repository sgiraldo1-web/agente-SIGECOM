"""Formato del resumen final y filtro de secretos."""

from __future__ import annotations

import re

REDACTED = "[oculto]"

# Claves con aspecto de clave de integración: tres o más bloques con letras y dígitos
# mezclados (por ejemplo ABC-12XY-34ZW). No toca códigos como CL-0007, PD-0101 o FV-10159.
_KEY_BLOCKS = re.compile(r"\b[A-Z0-9]{2,8}(?:-[A-Z0-9]{2,8}){2,}\b")
_LONG_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-]{32,})\b")


def _looks_like_key(m: re.Match) -> bool:
    s = m.group(0)
    blocks = s.split("-")[1:]
    return any(re.search(r"[A-Z]", b) and re.search(r"\d", b) for b in blocks)


def redact(text: str, secrets: list[str]) -> str:
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = re.sub(re.escape(s), REDACTED, text, flags=re.I)
    text = _KEY_BLOCKS.sub(lambda m: REDACTED if _looks_like_key(m) else m.group(0), text)
    text = _LONG_TOKEN.sub(REDACTED, text)
    return text


def _clean(text: str) -> str:
    # Solo el resumen controlado lleva la línea "respuesta:".
    return re.sub(r"respuesta\s*:", "respuesta -", text, flags=re.I).strip()


def build_done(resumen: str, respuesta: str | None, secrets: list[str]) -> str:
    parts = []
    if respuesta:
        parts.append(f"respuesta: {_clean(respuesta)}")
    parts.append(_clean(resumen) or "Tarea completada.")
    return redact("\n".join(parts), secrets)


def build_fail(razon: str, datos: list[str], secrets: list[str]) -> str:
    text = _clean(razon) or "No se pudo completar la tarea."
    if datos:
        text += "\nDatos: " + "; ".join(_clean(d) for d in datos)
    return redact(text, secrets)
