# agente-SIGECOM

Un agente que aprende a operar sistemas sin API, por pantalla, y ejecuta tareas en lenguaje natural en apps que nunca ha visto.

Frontier, Reto 1 · El agente que opera lo que no tiene API.

Servicio que recibe una tarea en lenguaje natural y la ejecuta sobre un escritorio remoto
mirando la pantalla y usando mouse y teclado por VNC (RFB sobre WebSocket). El diseño completo
está en `base/CONTEXTO_DESARROLLO.md` y en el documento de diseño en PDF.

## Estado

- [x] Cliente RFB propio (`app/rfb.py`): handshake 3.3/3.7/3.8, autenticación VNC, Raw + CopyRect,
      mouse (clic sostenido 150 ms, doble clic, scroll, arrastre) y teclado (un KeyEvent por carácter).
- [x] Percepción (`app/screen.py`): espera de estabilidad, JPEG, recorte ampliado.
- [x] Planificador con OpenRouter y bucle del agente (`app/planner.py`, `app/agent.py`)
- [x] Guardias, verificador y resumen sin secretos (`app/guards.py`, `app/verifier.py`, `app/summary.py`)
- [x] API (`/health`, `POST /tasks`, `GET /tasks/{id}`) y gestor de tareas (`app/main.py`, `app/tasks.py`)
- [ ] Despliegue en Railway

Pruebas de práctica E1 a E5: 5 de 5 en la primera corrida completa (26 a 135 s por tarea,
unos 0,01 USD por tarea con `anthropic/claude-haiku-5.5` y respaldo `openai/gpt-6-luna`).

## Uso local

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env   # completar valores; .env está en .gitignore

# Correr las pruebas con el agente directo (reset + tarea + /__check + reglas del resumen)
.venv/Scripts/python scripts/run_local.py E1 E3

# Servicio
.venv/Scripts/python -m uvicorn app.main:app --port 8000

# Hito 1: conectar, capturar, clic y escribir
.venv/Scripts/python scripts/rfb_smoke.py shot capturas/a.png click 122 48 stable shot capturas/b.png
```

Las capturas se guardan en `capturas/`, que no se versiona porque puede mostrar datos o claves.

## Seguridad

Ningún secreto va en el código, los logs ni el repositorio. Todo se lee de variables de entorno
(ver `.env.example`).
