"""IOC Print Agent — local Windows app that prints IOC labels on the PC's Windows printers.

Zero configuration: the IOC web page (already signed in) talks to this app over
http://127.0.0.1:17531, so no token or server URL is needed here.

    GET  /status                         -> {"app": "ioc-print-agent", "version": "..."}
    GET  /printers                       -> {"printers": [{"name": str, "isDefault": bool}]}
    POST /print?printer=<name>&title=<t> -> body: application/pdf (one page per label)
                                            200 {"ok": true, "pages": n} | 4xx/5xx {"error": str}

The browser side lives in the IOC repo (components/flows/label/printer-display.tsx,
PDF from app/api/printer/label-pdf). Any change to this contract must be made in both repos.

Run once (double-click the .exe): it copies itself to %LOCALAPPDATA%\\Programs\\IOC Print Agent,
starts with Windows (HKCU Run key, no admin rights) and runs in the background.
    "IOC Print Agent.exe" --uninstall   removes the autostart entry
    "IOC Print Agent.exe" --foreground  runs from the current location without installing (debug)
"""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

APP_NAME = "IOC Print Agent"
APP_ID = "ioc-print-agent"
VERSION = "2.0.0"

HOST = "127.0.0.1"
PORT = int(os.environ.get("IOC_PRINT_AGENT_PORT", "17531"))

# Sites allowed to list printers and print. Any other web page open in the browser is refused.
DEFAULT_ALLOWED_ORIGINS = (
    "https://ioc-demo.mstools.ch",
    "http://localhost:3000",
)
ALLOWED_ORIGINS = frozenset(o.strip().rstrip("/") for o in os.environ.get("IOC_ALLOWED_ORIGINS", ",".join(DEFAULT_ALLOWED_ORIGINS)).split(",") if o.strip())

# Guards against DNS rebinding: only requests addressed to the loopback name are served.
ALLOWED_HOSTS = frozenset({f"127.0.0.1:{PORT}", f"localhost:{PORT}"})

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_TITLE_LENGTH = 128
# Thermal printers only print black: anything lighter than this becomes white.
MONO_THRESHOLD = 160

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Programs", APP_NAME)
LOG_PATH = os.path.join(DATA_DIR, "agent.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

log = logging.getLogger(APP_ID)

# One job at a time: Windows spooler calls from several threads would interleave pages.
print_lock = threading.Lock()


class PrintError(Exception):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


# ─────────────────────────────────────────────
# Logging / message boxes
# ─────────────────────────────────────────────


def setup_logging() -> None:
    if log.handlers:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(fmt)
    log.addHandler(handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        log.addHandler(console)


def message_box(text: str, error: bool = False) -> None:
    # The frozen app has no console: the only way to tell the user something.
    if sys.platform != "win32":
        print(text, file=sys.stderr)
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10 if error else 0x40)


# ─────────────────────────────────────────────
# Windows printers
# ─────────────────────────────────────────────


def list_printers() -> list[dict]:
    import win32print

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = sorted({p[2] for p in win32print.EnumPrinters(flags, None, 1)})
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        default = None
    return [{"name": name, "isDefault": name == default} for name in names]


def render_pages(pdf_bytes: bytes, dpi: int, max_width: int, max_height: int):
    """Renders each PDF page at the printer's resolution, fitted in its printable area, black & white."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:
        raise PrintError(f"Invalid PDF: {e}", 400)

    try:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                # The label PDF is laid out in 203-dpi dots, so this is 1:1 on a TM printer.
                image = page.render(scale=dpi / 72).to_pil().convert("L")
            finally:
                page.close()

            ratio = min(1.0, max_width / image.width, max_height / image.height) if max_height > 0 else min(1.0, max_width / image.width)
            if ratio < 1.0:
                image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))

            yield image.point(lambda p: 255 if p > MONO_THRESHOLD else 0)
    finally:
        pdf.close()


def print_pdf(printer_name: str, pdf_bytes: bytes, title: str) -> int:
    import win32con
    import win32ui
    from PIL import ImageWin

    known = {p["name"] for p in list_printers()}
    if printer_name not in known:
        raise PrintError(f"Printer not found on this PC: {printer_name}", 404)

    with print_lock:
        dc = win32ui.CreateDC()
        try:
            dc.CreatePrinterDC(printer_name)
        except Exception as e:
            raise PrintError(f"Cannot open printer {printer_name}: {e}", 502)

        pages = 0
        started = False
        try:
            dpi = dc.GetDeviceCaps(win32con.LOGPIXELSX)
            width = dc.GetDeviceCaps(win32con.HORZRES)
            height = dc.GetDeviceCaps(win32con.VERTRES)

            dc.StartDoc(title)
            started = True
            for image in render_pages(pdf_bytes, dpi, width, height):
                dc.StartPage()
                left = max(0, (width - image.width) // 2)
                ImageWin.Dib(image).draw(dc.GetHandleOutput(), (left, 0, left + image.width, image.height))
                dc.EndPage()
                pages += 1
            dc.EndDoc()
            started = False
        except PrintError:
            raise
        except Exception as e:
            raise PrintError(f"Print failed on {printer_name}: {e}", 502)
        finally:
            if started:
                try:
                    dc.AbortDoc()
                except Exception:
                    pass
            dc.DeleteDC()

        if pages == 0:
            raise PrintError("The PDF has no pages", 400)
        return pages


# ─────────────────────────────────────────────
# HTTP API
# ─────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_ID}/{VERSION}"
    # Keep-alive off: one short request per connection, nothing to manage.
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # ── helpers ──

    def _origin(self) -> str | None:
        return self.headers.get("Origin")

    def _origin_allowed(self) -> bool:
        origin = self._origin()
        return origin is not None and origin.rstrip("/") in ALLOWED_ORIGINS

    def _host_allowed(self) -> bool:
        return (self.headers.get("Host") or "").lower() in ALLOWED_HOSTS

    def _cors_headers(self) -> None:
        if self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", self._origin())
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            # Chrome/Edge Private Network Access: a public site calling 127.0.0.1.
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, require_origin: bool) -> bool:
        if not self._host_allowed():
            self._json(403, {"error": "Forbidden host"})
            return False
        origin = self._origin()
        # Browsers always send Origin on cross-origin calls: a page from another site is refused.
        # Without Origin (curl, typing the URL) only read-only routes are served.
        if (origin is not None or require_origin) and not self._origin_allowed():
            log.warning("refused origin %r on %s", origin, self.path)
            self._json(403, {"error": "Origin not allowed"})
            return False
        return True

    # ── routes ──

    def do_OPTIONS(self):
        if not self._host_allowed() or not self._origin_allowed():
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            if self._guard(require_origin=False):
                self._json(200, {"app": APP_ID, "version": VERSION})
        elif path == "/printers":
            if not self._guard(require_origin=False):
                return
            try:
                self._json(200, {"printers": list_printers()})
            except Exception as e:
                log.exception("cannot list printers")
                self._json(500, {"error": f"Cannot list printers: {e}"})
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/print":
            self._json(404, {"error": "Not found"})
            return
        if not self._guard(require_origin=True):
            return

        query = parse_qs(url.query)
        printer = (query.get("printer") or [""])[0].strip()
        title = ((query.get("title") or [""])[0].strip() or "IOC labels")[:MAX_TITLE_LENGTH]
        if not printer:
            self._json(400, {"error": "Missing printer"})
            return

        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/pdf":
            self._json(415, {"error": "Body must be application/pdf"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_PDF_BYTES:
            self._json(413 if length > MAX_PDF_BYTES else 400, {"error": "Invalid PDF size"})
            return

        pdf_bytes = self.rfile.read(length)
        if not pdf_bytes.startswith(b"%PDF-"):
            self._json(400, {"error": "Body is not a PDF"})
            return

        try:
            pages = print_pdf(printer, pdf_bytes, title)
        except PrintError as e:
            log.error("print %r on %r failed: %s", title, printer, e)
            self._json(e.status, {"error": str(e)})
            return
        except Exception as e:
            log.exception("print %r on %r failed", title, printer)
            self._json(500, {"error": f"Print failed: {e}"})
            return

        log.info("printed %r on %r (%d page(s))", title, printer, pages)
        self._json(200, {"ok": True, "pages": pages})


def serve() -> None:
    setup_logging()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        # Already running (autostart + double-click): the other instance serves the site.
        log.warning("port %d unavailable, another instance is probably running: %s", PORT, e)
        return
    server.daemon_threads = True
    log.info("%s %s listening on http://%s:%d for %s", APP_NAME, VERSION, HOST, PORT, ", ".join(sorted(ALLOWED_ORIGINS)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log.info("stopped")


# ─────────────────────────────────────────────
# Install (per user, no admin rights)
# ─────────────────────────────────────────────


def _installed_exe() -> str:
    return os.path.join(INSTALL_DIR, f"{APP_NAME}.exe")


def _set_autostart(exe_path: str | None) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if exe_path:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def install_and_launch() -> None:
    """Copies the running .exe to the per-user install dir, registers autostart and starts it."""
    target = _installed_exe()
    os.makedirs(INSTALL_DIR, exist_ok=True)
    try:
        shutil.copy2(sys.executable, target)
    except PermissionError:
        # The installed copy is running (update over a live install): keep it, it serves the same API.
        log.warning("installed copy is in use, not replaced")
    _set_autostart(target)
    subprocess.Popen([target], close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    log.info("installed to %s", target)
    message_box(f"{APP_NAME} is installed and running.\n\nIt starts with Windows and lets IOC print on this PC's printers.")


def main() -> None:
    setup_logging()
    args = set(sys.argv[1:])

    if "--uninstall" in args:
        _set_autostart(None)
        message_box(f"{APP_NAME} will no longer start with Windows.\nYou can delete {INSTALL_DIR}.")
        return

    frozen = getattr(sys, "frozen", False)
    running_installed = frozen and os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(_installed_exe())

    if frozen and not running_installed and "--foreground" not in args:
        try:
            install_and_launch()
        except Exception as e:
            log.exception("install failed")
            message_box(f"Installation failed:\n{e}\n\nLog: {LOG_PATH}", error=True)
        return

    serve()


if __name__ == "__main__":
    main()
