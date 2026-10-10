"""Lectura de variables de entorno. Ningún secreto vive en el código."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_token: str
    openrouter_api_key: str
    model_primary: str
    model_fallback: str
    task_deadline_s: float
    max_steps: int


def get_settings() -> Settings:
    return Settings(
        api_token=os.getenv("API_TOKEN", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
        model_primary=os.getenv("MODEL_PRIMARY", ""),
        model_fallback=os.getenv("MODEL_FALLBACK", ""),
        task_deadline_s=float(os.getenv("TASK_DEADLINE_S", "270")),
        max_steps=int(os.getenv("MAX_STEPS", "40")),
    )


@dataclass(frozen=True)
class DesktopSettings:
    """Solo para pruebas locales contra el escritorio de práctica."""
    url: str
    password: str
    token: str


def get_desktop_settings() -> DesktopSettings:
    return DesktopSettings(
        url=os.getenv("DESKTOP_URL", ""),
        password=os.getenv("DESKTOP_PASSWORD", ""),
        token=os.getenv("DESKTOP_TOKEN", ""),
    )
