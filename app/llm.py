"""Llamadas a OpenRouter (API compatible con OpenAI) con modelo de respaldo."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    pass


def extract_json(text: str) -> dict:
    """Saca el primer objeto JSON de la respuesta (tolera ```json y texto alrededor)."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("la respuesta no trae JSON")
    return json.loads(text[start:end + 1])


@dataclass
class LLMStats:
    calls: int = 0
    fallback_calls: int = 0
    errors: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    models: list[str] = field(default_factory=list)


class LLM:
    def __init__(self, api_key: str, primary: str, fallback: str, http: httpx.AsyncClient):
        self.api_key = api_key
        self.models = [m for m in (primary, fallback) if m]
        if not self.models:
            raise LLMError("no hay modelos configurados (MODEL_PRIMARY)")
        self.http = http
        self.stats = LLMStats()

    async def _call(self, model: str, messages: list[dict], timeout: float, max_tokens: int) -> str:
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        t = time.monotonic()
        r = await self.http.post(OPENROUTER_URL, json=body, timeout=timeout,
                                 headers={"Authorization": f"Bearer {self.api_key}"})
        self.stats.seconds += time.monotonic() - t
        self.stats.calls += 1
        if r.status_code != 200:
            raise LLMError(f"OpenRouter {r.status_code}: {r.text[:200]}")
        data = r.json()
        if "choices" not in data:
            raise LLMError(f"respuesta sin choices: {str(data)[:200]}")
        self.stats.cost += float((data.get("usage") or {}).get("cost") or 0)
        self.stats.models.append(model)
        return data["choices"][0]["message"].get("content") or ""

    async def chat_json(self, messages: list[dict], *, timeout: float, max_tokens: int = 900,
                        validate=None) -> tuple[dict, str]:
        """Pide un JSON. Con el modelo principal: un intento y un reintento si el
        JSON es inválido. Si vuelve a fallar, pasa al de respaldo.

        `validate(obj)` puede lanzar ValueError con un mensaje para el reintento.
        Devuelve (objeto, modelo_usado).
        """
        deadline = time.monotonic() + timeout
        last_error = "sin intentos"
        for i, model in enumerate(self.models):
            if i > 0:
                self.stats.fallback_calls += 1
            msgs = list(messages)
            for _attempt in range(2):
                remaining = deadline - time.monotonic()
                if remaining < 2:
                    raise LLMError(f"sin tiempo para el modelo ({last_error})")
                try:
                    text = await self._call(model, msgs, timeout=remaining, max_tokens=max_tokens)
                except (httpx.HTTPError, LLMError) as exc:
                    self.stats.errors += 1
                    last_error = f"{model}: {exc}"
                    log.warning("LLM error %s", last_error)
                    break  # error de red o del proveedor: pasa al siguiente modelo
                try:
                    obj = extract_json(text)
                    if validate is not None:
                        validate(obj)
                    return obj, model
                except (ValueError, json.JSONDecodeError) as exc:
                    self.stats.errors += 1
                    last_error = f"{model}: JSON inválido ({exc})"
                    log.warning("LLM %s · inicio: %r", last_error, text[:200])
                    msgs = msgs + [
                        {"role": "assistant", "content": text[:2000]},
                        {"role": "user", "content": f"Tu respuesta no es válida: {exc}. "
                                                    "Responde solo con el objeto JSON pedido."},
                    ]
        raise LLMError(last_error)
