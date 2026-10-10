"""Cliente RFB (VNC) mínimo sobre WebSocket, pensado para /websockify.

Implementa lo justo para operar un escritorio como lo haría una persona:
handshake 3.3/3.7/3.8, autenticación VNC (DES), framebuffer en Raw y
CopyRect, cambio de tamaño (DesktopSize) y eventos de mouse y teclado.

La contraseña solo vive en memoria dentro de esta clase y nunca se registra.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from urllib.parse import urlsplit

import numpy as np
from Crypto.Cipher import DES
from websockets.asyncio.client import ClientConnection, connect

log = logging.getLogger(__name__)

ENC_RAW = 0
ENC_COPYRECT = 1
ENC_DESKTOP_SIZE = -223

# Formato de píxel fijo: 32 bpp, profundidad 24, little endian, color verdadero.
# En memoria cada píxel llega como B, G, R, X.
PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)

KEYSYMS = {
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "backspace": 0xFF08,
    "escape": 0xFF1B, "esc": 0xFF1B, "delete": 0xFFFF, "supr": 0xFFFF,
    "insert": 0xFF63, "home": 0xFF50, "end": 0xFF57,
    "pageup": 0xFF55, "pagedown": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "ctrl": 0xFFE3, "control": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9,
    "meta": 0xFFE7, "super": 0xFFEB, "space": 0x20,
    **{f"f{i}": 0xFFBD + i for i in range(1, 13)},
}

BUTTON_MASKS = {"left": 1, "middle": 2, "right": 4}


class RFBError(Exception):
    pass


def _vnc_des_key(password: str) -> bytes:
    """Clave DES de VNC: 8 bytes con los bits de cada byte invertidos."""
    raw = password.encode("latin-1", errors="replace")[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in raw)


def char_keysym(ch: str) -> int:
    if ch == "\n":
        return KEYSYMS["enter"]
    if ch == "\t":
        return KEYSYMS["tab"]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp  # Latin-1: el keysym es el punto de código
    return 0x01000000 + cp


def parse_combo(combo: str) -> list[int]:
    """'ctrl+a' -> [Control_L, 'a']. Una sola letra o un nombre de KEYSYMS."""
    keys = []
    for part in combo.split("+"):
        name = part.strip()
        low = name.lower()
        if low in KEYSYMS:
            keys.append(KEYSYMS[low])
        elif len(name) == 1:
            keys.append(char_keysym(name))
        else:
            raise ValueError(f"tecla desconocida: {name!r}")
    return keys


class RFBClient:
    """Conexión a un escritorio VNC por WebSocket.

    Un bucle en segundo plano lee los mensajes del servidor y mantiene
    `self.fb` (arreglo NumPy alto x ancho x 3, RGB) siempre actualizado.
    """

    def __init__(self, url: str, password: str, *, open_timeout: float = 15.0):
        self.url = url
        self._password = password
        self.open_timeout = open_timeout
        self._ws: ClientConnection | None = None
        self._buf = bytearray()
        self._reader: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._frame_event = asyncio.Event()
        self.width = 0
        self.height = 0
        self.name = ""
        self.fb: np.ndarray | None = None
        self.frame_seq = 0
        self.last_update = 0.0
        self.bytes_in = 0
        self.error: BaseException | None = None
        self.mouse = (0, 0)

    def __repr__(self) -> str:  # nunca incluir la contraseña
        return f"<RFBClient {urlsplit(self.url).netloc} {self.width}x{self.height}>"

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    # ── conexión ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        parts = urlsplit(self.url)
        origin = f"{'https' if parts.scheme == 'wss' else 'http'}://{parts.netloc}"
        self._buf.clear()
        self.error = None
        self._ws = await connect(
            self.url,
            subprotocols=["binary"],
            origin=origin,
            max_size=None,
            compression=None,
            open_timeout=self.open_timeout,
            ping_interval=20,
            ping_timeout=20,
        )
        try:
            await asyncio.wait_for(self._handshake(), self.open_timeout)
        except Exception:
            await self.close()
            raise
        self._reader = asyncio.create_task(self._read_loop(), name="rfb-reader")
        await self._request_update(incremental=False)
        await self.wait_frame(timeout=self.open_timeout)

    async def reconnect(self) -> None:
        await self.close()
        await self.connect()

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def __aenter__(self) -> "RFBClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ── lectura del flujo ───────────────────────────────────────────────

    async def _readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            data = await self._ws.recv()
            if isinstance(data, str):
                data = data.encode("latin-1")
            self.bytes_in += len(data)
            self._buf += data
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def _send(self, data: bytes) -> None:
        if self._ws is None:
            raise RFBError("sin conexión")
        async with self._send_lock:
            await self._ws.send(data)

    async def _handshake(self) -> None:
        banner = await self._readexactly(12)
        if not banner.startswith(b"RFB "):
            raise RFBError(f"no es un servidor RFB: {banner!r}")
        major, minor = int(banner[4:7]), int(banner[8:11])
        version = 8 if (major, minor) >= (3, 8) else 7 if (major, minor) >= (3, 7) else 3
        await self._send(f"RFB 003.00{version}\n".encode())

        if version == 3:
            (sec,) = struct.unpack(">I", await self._readexactly(4))
            if sec == 0:
                raise RFBError(await self._read_reason())
        else:
            (count,) = struct.unpack(">B", await self._readexactly(1))
            if count == 0:
                raise RFBError(await self._read_reason())
            types = list(await self._readexactly(count))
            if 2 in types:
                sec = 2
            elif 1 in types:
                sec = 1
            else:
                raise RFBError(f"tipos de seguridad no soportados: {types}")
            await self._send(bytes([sec]))

        if sec == 2:
            challenge = await self._readexactly(16)
            cipher = DES.new(_vnc_des_key(self._password), DES.MODE_ECB)
            await self._send(cipher.encrypt(challenge))
        if sec == 2 or version == 8:
            (result,) = struct.unpack(">I", await self._readexactly(4))
            if result != 0:
                reason = await self._read_reason() if version == 8 else ""
                raise RFBError(f"autenticación VNC rechazada {reason}".strip())

        await self._send(b"\x01")  # ClientInit, shared-flag = 1
        w, h = struct.unpack(">HH", await self._readexactly(4))
        await self._readexactly(16)  # formato de píxel del servidor, se reemplaza
        (name_len,) = struct.unpack(">I", await self._readexactly(4))
        self.name = (await self._readexactly(name_len)).decode("utf-8", errors="replace")
        self._resize(w, h)

        await self._send(b"\x00\x00\x00\x00" + PIXEL_FORMAT)  # SetPixelFormat
        encodings = [ENC_COPYRECT, ENC_RAW, ENC_DESKTOP_SIZE]
        await self._send(struct.pack(f">BxH{len(encodings)}i", 2, len(encodings), *encodings))
        log.info("RFB conectado: %s %dx%d", self.name, w, h)

    async def _read_reason(self) -> str:
        (n,) = struct.unpack(">I", await self._readexactly(4))
        return (await self._readexactly(n)).decode("utf-8", errors="replace")

    def _resize(self, w: int, h: int) -> None:
        self.width, self.height = w, h
        old = self.fb
        self.fb = np.zeros((h, w, 3), dtype=np.uint8)
        if old is not None:
            hh, ww = min(h, old.shape[0]), min(w, old.shape[1])
            self.fb[:hh, :ww] = old[:hh, :ww]

    async def _read_loop(self) -> None:
        try:
            while True:
                msg_type = (await self._readexactly(1))[0]
                if msg_type == 0:
                    await self._on_framebuffer_update()
                elif msg_type == 1:  # SetColourMapEntries (no se usa con color verdadero)
                    _first, n = struct.unpack(">xHH", await self._readexactly(5))
                    await self._readexactly(6 * n)
                elif msg_type == 2:  # Bell
                    pass
                elif msg_type == 3:  # ServerCutText
                    (n,) = struct.unpack(">3xI", await self._readexactly(7))
                    await self._readexactly(n)
                else:
                    raise RFBError(f"mensaje del servidor desconocido: {msg_type}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            log.warning("RFB: conexión terminada: %s", exc)
        finally:
            self._signal_frame()

    async def _on_framebuffer_update(self) -> None:
        (n_rects,) = struct.unpack(">xH", await self._readexactly(3))
        for _ in range(n_rects):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self._readexactly(12))
            if enc == ENC_RAW:
                data = await self._readexactly(w * h * 4)
                px = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
                self.fb[y:y + h, x:x + w] = px[:, :, 2::-1]  # BGRX -> RGB
            elif enc == ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", await self._readexactly(4))
                self.fb[y:y + h, x:x + w] = self.fb[sy:sy + h, sx:sx + w].copy()
            elif enc == ENC_DESKTOP_SIZE:
                self._resize(w, h)
            else:
                raise RFBError(f"codificación no soportada: {enc}")
        if n_rects:
            self.frame_seq += 1
            self.last_update = time.monotonic()
            self._signal_frame()
        # Pide la siguiente actualización con una pausa corta, por si el
        # servidor responde enseguida aunque no haya cambios.
        asyncio.get_running_loop().call_later(0.05, self._schedule_request)

    def _schedule_request(self) -> None:
        if self.connected:
            asyncio.ensure_future(self._safe_request())

    async def _safe_request(self) -> None:
        try:
            await self._request_update(incremental=True)
        except Exception as exc:
            log.debug("RFB: no se pudo pedir actualización: %s", exc)

    async def _request_update(self, incremental: bool) -> None:
        await self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    def _signal_frame(self) -> None:
        event, self._frame_event = self._frame_event, asyncio.Event()
        event.set()

    async def wait_frame(self, timeout: float) -> bool:
        """Espera la próxima actualización del framebuffer. False si no llega."""
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        if self.error is not None:
            raise RFBError(f"conexión perdida: {self.error}")
        return True

    async def refresh(self, full: bool = False) -> None:
        await self._request_update(incremental=not full)

    def snapshot(self) -> np.ndarray:
        if self.error is not None:
            raise RFBError(f"conexión perdida: {self.error}")
        return self.fb.copy()

    # ── entrada: mouse ──────────────────────────────────────────────────

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        return (max(0, min(int(x), self.width - 1)), max(0, min(int(y), self.height - 1)))

    async def pointer(self, x: int, y: int, mask: int = 0) -> None:
        x, y = self._clamp(x, y)
        self.mouse = (x, y)
        await self._send(struct.pack(">BBHH", 5, mask, x, y))

    async def click(self, x: int, y: int, button: str = "left", count: int = 1,
                    hold: float = 0.15) -> None:
        """Clic con el botón sostenido `hold` s (la app ignora clics de menos de 100 ms)."""
        mask = BUTTON_MASKS[button]
        await self.pointer(x, y, 0)
        await asyncio.sleep(0.05)
        for i in range(count):
            await self.pointer(x, y, mask)
            await asyncio.sleep(hold)
            await self.pointer(x, y, 0)
            if i < count - 1:
                await asyncio.sleep(0.08)

    async def double_click(self, x: int, y: int) -> None:
        await self.click(x, y, count=2, hold=0.12)

    async def scroll(self, x: int, y: int, dy: int) -> None:
        """dy > 0 baja, dy < 0 sube. Cada unidad es una muesca de la rueda."""
        mask = 16 if dy > 0 else 8
        await self.pointer(x, y, 0)
        for _ in range(abs(int(dy))):
            await self.pointer(x, y, mask)
            await asyncio.sleep(0.03)
            await self.pointer(x, y, 0)
            await asyncio.sleep(0.05)

    async def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> None:
        await self.pointer(x1, y1, 0)
        await asyncio.sleep(0.05)
        await self.pointer(x1, y1, 1)
        await asyncio.sleep(0.15)
        for i in range(1, steps + 1):
            await self.pointer(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps, 1)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        await self.pointer(x2, y2, 0)

    # ── entrada: teclado ────────────────────────────────────────────────

    async def key_event(self, keysym: int, down: bool) -> None:
        await self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    async def press(self, combo: str, hold: float = 0.05) -> None:
        """Pulsa una tecla o combinación: 'enter', 'ctrl+a', 'shift+tab'."""
        keys = parse_combo(combo)
        for k in keys:
            await self.key_event(k, True)
            await asyncio.sleep(0.02)
        await asyncio.sleep(hold)
        for k in reversed(keys):
            await self.key_event(k, False)
            await asyncio.sleep(0.02)

    async def type_text(self, text: str, delay: float = 0.035) -> None:
        """Escribe carácter por carácter, un KeyEvent por tecla.

        El texto en bloque no llegó a los campos en las pruebas manuales.
        """
        for ch in text:
            ks = char_keysym(ch)
            await self.key_event(ks, True)
            await asyncio.sleep(0.01)
            await self.key_event(ks, False)
            await asyncio.sleep(delay)
