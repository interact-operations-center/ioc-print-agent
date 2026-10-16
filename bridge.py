"""IOC Print Agent — local Windows app that prints IOC labels on the PC's Windows printers.

Zero configuration: the IOC web page (already signed in) talks to this app over
http://127.0.0.1:17531, so no token or server URL is needed here.

    GET  /status                         -> {"app": "ioc-print-agent", "version": "..."}
    GET  /printers                       -> {"printers": [{"name": str, "isDefault": bool,
                                                           "status": "ready" | "offline" | "error",
                                                           "statusMessage": str | null}]}
    POST /print?printer=<name>&title=<t> -> body: application/pdf (one page per label)
                                            200 {"ok": true, "pages": n} | 4xx/5xx {"error": str}
                                            503: printer offline / in error, 504: job not taken in time

The browser side lives in the IOC repo (components/flows/label/printer-display.tsx,
PDF from app/api/printer/label-pdf). Any change to this contract must be made in both repos.

Run once (double-click the .exe): it copies itself to %LOCALAPPDATA%\\Programs\\IOC Print Agent,
starts with Windows (HKCU Run key, no admin rights) and runs in the background.
    "IOC Print Agent.exe" --uninstall   removes the autostart entry
    "IOC Print Agent.exe" --foreground  runs from the current location without installing (debug)
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

APP_NAME = "IOC Print Agent"
APP_ID = "ioc-print-agent"
VERSION = "2.2.0"

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
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        default = None

    # Level 2 carries Status/Attributes: the page can flag an offline printer before printing.
    printers: dict[str, dict] = {}
    for info in win32print.EnumPrinters(flags, None, 2):
        name = info["pPrinterName"]
        status, message = printer_state(info)
        printers[name] = {"name": name, "isDefault": name == default, "status": status, "statusMessage": message}
    return [printers[name] for name in sorted(printers)]


def render_pages(pdf_bytes: bytes, dpi: int, max_width: int, max_height: int = 0):
    """Renders each PDF page at `dpi`, shrunk to fit max_width x max_height (0 = no height limit), pure black & white ("L" mode, 0 or 255)."""
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

            ratio = min(1.0, max_width / image.width)
            if max_height > 0:
                ratio = min(ratio, max_height / image.height)
            if ratio < 1.0:
                image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))

            yield image.point(lambda p: 255 if p > MONO_THRESHOLD else 0)
    finally:
        pdf.close()


# ── Printer / job state ──
#
# The Windows spooler accepts a job even when the printer is unplugged or offline: StartDoc/EndDoc
# succeed and the job just waits in the queue. So the printer state is checked before printing and
# the job is followed in the queue afterwards. A job that fails or never leaves the queue is deleted
# (otherwise it would print by surprise once the printer comes back) and reported as an error.

PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400
PRINTER_STATUS_ERRORS = (
    (0x00000080, "printer is offline"),  # PRINTER_STATUS_OFFLINE
    (0x00001000, "printer is not available"),  # PRINTER_STATUS_NOT_AVAILABLE
    (0x00000002, "printer is in error"),  # PRINTER_STATUS_ERROR
    (0x00000010, "printer is out of labels"),  # PRINTER_STATUS_PAPER_OUT
    (0x00000008, "label jam"),  # PRINTER_STATUS_PAPER_JAM
    (0x00400000, "printer cover is open"),  # PRINTER_STATUS_DOOR_OPEN
    (0x00100000, "printer needs user intervention"),  # PRINTER_STATUS_USER_INTERVENTION
    (0x00000001, "printer queue is paused"),  # PRINTER_STATUS_PAUSED
)
JOB_STATUS_ERRORS = (
    (0x00000020, "printer is offline"),  # JOB_STATUS_OFFLINE
    (0x00000002, "print job failed"),  # JOB_STATUS_ERROR
    (0x00000040, "printer is out of labels"),  # JOB_STATUS_PAPEROUT
    (0x00000200, "printer driver blocked the job"),  # JOB_STATUS_BLOCKED_DEVQ
    (0x00000400, "printer needs user intervention"),  # JOB_STATUS_USER_INTERVENTION
)
JOB_STATUS_PRINTED = 0x00000080
JOB_CONTROL_DELETE = 5
# How long a job may stay in the queue before it is considered stuck (unplugged printer, etc.).
JOB_TIMEOUT_SECONDS = float(os.environ.get("IOC_JOB_TIMEOUT_SECONDS", "10"))
JOB_POLL_SECONDS = 0.25


def printer_info(printer_name: str) -> dict:
    import win32print

    try:
        handle = win32print.OpenPrinter(printer_name)
    except Exception as e:
        raise PrintError(f"Cannot open printer {printer_name}: {e}", 502)
    try:
        return win32print.GetPrinter(handle, 2)
    except Exception as e:
        raise PrintError(f"Cannot read printer {printer_name}: {e}", 502)
    finally:
        win32print.ClosePrinter(handle)


# Status flags that mean "not reachable" rather than "reachable but needs attention".
PRINTER_OFFLINE_FLAGS = frozenset({0x00000080, 0x00001000})


def printer_state(info: dict) -> tuple[str, str | None]:
    """("ready", None) | ("offline", message) | ("error", message), from a level 2 printer info (GetPrinter / EnumPrinters)."""
    if (info.get("Attributes") or 0) & PRINTER_ATTRIBUTE_WORK_OFFLINE:
        return "offline", "printer is offline (disconnected or set to 'Use printer offline')"
    status = info.get("Status") or 0
    for flag, message in PRINTER_STATUS_ERRORS:
        if status & flag:
            return ("offline" if flag in PRINTER_OFFLINE_FLAGS else "error"), message
    return "ready", None


def check_printer_ready(printer_name: str, info: dict) -> None:
    status, message = printer_state(info)
    if status != "ready":
        raise PrintError(f"{printer_name}: {message}", 503)


def _delete_job(handle, job_id: int) -> None:
    import win32print

    try:
        win32print.SetJob(handle, job_id, 0, None, JOB_CONTROL_DELETE)
    except Exception:
        log.warning("could not delete job %d", job_id)


def wait_for_job(printer_name: str, job_id: int | None) -> None:
    """Follows the spooled job until it leaves the queue. On error/timeout, deletes it and raises PrintError."""
    import win32print

    if not job_id or job_id <= 0:
        return

    try:
        handle = win32print.OpenPrinter(printer_name)
    except Exception as e:
        raise PrintError(f"Cannot open printer {printer_name}: {e}", 502)

    try:
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while True:
            try:
                job = win32print.GetJob(handle, job_id, 1)
            except Exception:
                return  # left the queue: handed over to the printer

            status = job.get("Status") or 0
            if status & JOB_STATUS_PRINTED:
                return

            driver_status = (job.get("pStatus") or "").strip()
            suffix = f" ({driver_status})" if driver_status else ""
            for flag, message in JOB_STATUS_ERRORS:
                if status & flag:
                    _delete_job(handle, job_id)
                    raise PrintError(f"{printer_name}: {message}{suffix}, job cancelled", 503)

            if time.monotonic() >= deadline:
                _delete_job(handle, job_id)
                raise PrintError(f"{printer_name}: printer did not take the job within {JOB_TIMEOUT_SECONDS:g}s (disconnected?){suffix}, job cancelled", 504)

            time.sleep(JOB_POLL_SECONDS)
    finally:
        win32print.ClosePrinter(handle)


# ── ESC/POS (RAW) ──
#
# "Generic / Text Only" (the usual driver for a TM printer added by IP without the Epson
# driver) drops every graphic: a GDI print comes out empty. For those printers the label is
# sent as raw ESC/POS instead, which the driver passes through untouched to port 9100.

ESC_POS_DPI = 203
# 80 mm roll on a TM-M30 / TM-T88 / TM-T20: 576 printable dots. Wider pages are shrunk.
ESC_POS_MAX_WIDTH_DOTS = int(os.environ.get("IOC_ESCPOS_WIDTH_DOTS", "576"))
# Rows per GS v 0 block: small blocks keep each command well inside the printers' buffer.
ESC_POS_BAND_ROWS = 256
RAW_DRIVERS = ("generic / text only",)
# IOC's label PDF has a fixed height (sized for a long comment) that only page-based drivers need.
# On a continuous roll, any blank band taller than this is shrunk to ESC_POS_KEEP_BLANK_DOTS,
# so the label fits its content. The label's own spacings (QR margins, separators) stay below it.
ESC_POS_MAX_BLANK_DOTS = int(os.environ.get("IOC_ESCPOS_MAX_BLANK_DOTS", "40"))
ESC_POS_KEEP_BLANK_DOTS = 24  # 3 mm at 203 dpi


def escpos_raster(image) -> bytes:
    """GS v 0 raster image(s) of a black & white "L" image (0 = black), 1 bit per dot, MSB first."""
    from PIL import Image

    width_bytes = (image.width + 7) // 8
    # ESC/POS bit 1 = printed dot: black pixels (0) become 1. Mode "1" packs rows MSB first,
    # padded to a whole byte with 0 (= not printed).
    bits = image.point(lambda p: 255 if p == 0 else 0).convert("1", dither=Image.Dither.NONE)
    data = bits.tobytes("raw", "1")

    out = bytearray()
    for top in range(0, image.height, ESC_POS_BAND_ROWS):
        rows = min(ESC_POS_BAND_ROWS, image.height - top)
        out += b"\x1dv0\x00" + bytes((width_bytes & 0xFF, width_bytes >> 8, rows & 0xFF, rows >> 8))
        out += data[top * width_bytes : (top + rows) * width_bytes]
    return bytes(out)


def collapse_blank_bands(image, max_blank: int = ESC_POS_MAX_BLANK_DOTS, keep: int = ESC_POS_KEEP_BLANK_DOTS):
    """Shrinks every run of fully white rows taller than max_blank to `keep` rows (0 = disabled)."""
    if max_blank <= 0:
        return image

    from PIL import Image

    width = image.width
    data = image.tobytes()
    blank = [b"\x00" not in data[y * width : (y + 1) * width] for y in range(image.height)]

    rows: list[int] = []
    y = 0
    while y < image.height:
        if not blank[y]:
            rows.append(y)
            y += 1
            continue
        end = y
        while end < image.height and blank[end]:
            end += 1
        rows.extend(range(y, y + (keep if end - y > max_blank else end - y)))
        y = end

    if len(rows) == image.height:
        return image
    out = Image.new("L", (width, len(rows)), 255)
    for i, src in enumerate(rows):
        out.paste(image.crop((0, src, width, src + 1)), (0, i))
    return out


def escpos_document(pdf_bytes: bytes) -> tuple[bytes, int]:
    out = bytearray(b"\x1b@")  # ESC @: reset
    pages = 0
    for image in render_pages(pdf_bytes, ESC_POS_DPI, ESC_POS_MAX_WIDTH_DOTS):
        out += b"\x1ba\x01"  # ESC a 1: center (pages narrower than the roll)
        out += escpos_raster(collapse_blank_bands(image))
        out += b"\x1dVB\x00"  # GS V 66 0: feed to the cutter, partial cut
        pages += 1
    return bytes(out), pages


def print_raw_escpos(printer_name: str, pdf_bytes: bytes, title: str) -> int:
    import win32print

    data, pages = escpos_document(pdf_bytes)
    if pages == 0:
        raise PrintError("The PDF has no pages", 400)

    try:
        handle = win32print.OpenPrinter(printer_name)
    except Exception as e:
        raise PrintError(f"Cannot open printer {printer_name}: {e}", 502)

    job_id = None
    try:
        job_id = win32print.StartDocPrinter(handle, 1, (title, None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
    except Exception as e:
        raise PrintError(f"Print failed on {printer_name}: {e}", 502)
    finally:
        win32print.ClosePrinter(handle)

    wait_for_job(printer_name, job_id)
    return pages


# ── GDI (printer's own driver) ──


def print_gdi(printer_name: str, pdf_bytes: bytes, title: str) -> int:
    import win32con
    import win32ui
    from PIL import ImageWin

    dc = win32ui.CreateDC()
    try:
        dc.CreatePrinterDC(printer_name)
    except Exception as e:
        raise PrintError(f"Cannot open printer {printer_name}: {e}", 502)

    pages = 0
    job_id = None
    started = False
    try:
        dpi = dc.GetDeviceCaps(win32con.LOGPIXELSX)
        width = dc.GetDeviceCaps(win32con.HORZRES)
        height = dc.GetDeviceCaps(win32con.VERTRES)

        job_id = dc.StartDoc(title)  # spooler job id
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

    wait_for_job(printer_name, job_id if isinstance(job_id, int) else None)
    return pages


def print_pdf(printer_name: str, pdf_bytes: bytes, title: str) -> tuple[int, str]:
    """Prints the PDF; returns (pages, mode) where mode is "escpos" or "gdi"."""
    known = {p["name"] for p in list_printers()}
    if printer_name not in known:
        raise PrintError(f"Printer not found on this PC: {printer_name}", 404)

    with print_lock:
        # Read inside the lock: the state may have changed while waiting for a previous job.
        info = printer_info(printer_name)
        check_printer_ready(printer_name, info)

        driver = info.get("pDriverName") or ""
        if driver.strip().lower() in RAW_DRIVERS:
            return print_raw_escpos(printer_name, pdf_bytes, title), "escpos"
        return print_gdi(printer_name, pdf_bytes, title), "gdi"


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
            pages, mode = print_pdf(printer, pdf_bytes, title)
        except PrintError as e:
            log.error("print %r on %r failed: %s", title, printer, e)
            self._json(e.status, {"error": str(e)})
            return
        except Exception as e:
            log.exception("print %r on %r failed", title, printer)
            self._json(500, {"error": f"Print failed: {e}"})
            return

        log.info("printed %r on %r (%d page(s), %s)", title, printer, pages, mode)
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