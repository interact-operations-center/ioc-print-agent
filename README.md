# IOC Print Agent

Local Windows app (`bridge.py`) that prints labels for [IOC](https://github.com/interact-operations-center/ioc) on the PC's Windows printers (USB or TCP/IP port).

The IOC print popup ("This PC" section) talks to this app on `http://127.0.0.1:17531`. The browser is already signed in to IOC, so the app needs no token and no server URL.

```
GET  /status                         → { app, version }
GET  /printers                       → { printers: [{ name, isDefault, status: "ready" | "offline" | "error", statusMessage }] }
POST /print?printer=<name>&title=<t> → body application/pdf (one page per label)
                                       200 { ok, pages } | 4xx/5xx { error }
                                       503 printer offline / in error, 504 job not taken in time
```

The browser side lives in the IOC repo: `components/flows/label/printer-display.tsx` (`NEXT_PUBLIC_PRINT_AGENT_URL`) and the PDF route `app/api/printer/label-pdf`. Any change to this contract must be made in both repos.

Each PDF page is rendered in pure black and white (the label PDF is laid out in 203-dpi dots, so it is 1:1 on an Epson TM) and sent through the Windows spooler, one job at a time, in one of two ways depending on the printer's driver:

- **Generic / Text Only** (a TM printer added by IP without the Epson driver): this driver drops graphics, so the label goes as **raw ESC/POS** — `GS v 0` raster at 203 dpi, centered, then feed + partial cut (`GS V 66 0`). Pages wider than 576 dots (80 mm roll) are shrunk; set `IOC_ESCPOS_WIDTH_DOTS` (e.g. `384` for a 58 mm roll) to change it. IOC's label PDF has a fixed height (room for a long comment); on the roll, every blank band taller than 40 dots (5 mm) is shrunk to 3 mm so the label fits its content — `IOC_ESCPOS_MAX_BLANK_DOTS=0` disables it.
- **Any other driver** (Epson APD, PDF printer…): drawn through the driver (GDI) at its resolution, fitted in its printable area.

The log line `printed … (n page(s), escpos|gdi)` tells which one was used.

The Windows spooler accepts a job even when the printer is unplugged or offline (it just waits in the queue), so the app checks the printer's state before printing (503 if it is offline, paused, out of labels, cover open…) and then follows the job in the queue. A job that fails, or is still queued after `IOC_JOB_TIMEOUT_SECONDS` (default 10) plus `IOC_JOB_SECONDS_PER_PAGE` (default 2) per label, is deleted, so it can't print by surprise once the printer comes back, and the page gets the error (503 / 504).

Security:

- listens on `127.0.0.1` only, and refuses any `Host` other than `127.0.0.1:17531` / `localhost:17531` (DNS rebinding);
- only the IOC origins may list printers or print (`DEFAULT_ALLOWED_ORIGINS` in `bridge.py`, overridable with the `IOC_ALLOWED_ORIGINS` env var, comma-separated). Any other website open in the browser gets a 403. `/print` always requires an allowed `Origin`;
- answers Chrome/Edge Private Network Access preflights (`Access-Control-Allow-Private-Network`). The browser may ask the user once to allow IOC to reach local devices.

When IOC gets a new domain (production), add it to `DEFAULT_ALLOWED_ORIGINS` and rebuild.

## Printer setup on the PC

A printer added with a TCP/IP address works like a USB one: the spooler forwards the job. Use a **Standard TCP/IP** port (RAW, 9100) with either the **Generic / Text Only** driver (no install needed, the app sends ESC/POS) or the Epson driver (APD / TM-M30). Avoid WSD/IPP ports with the "Microsoft IPP Class Driver". In the driver preferences, select the roll width that matches the paper (e.g. 80 mm). In IOC, the user picks the paper format once per printer (kept in the browser).

## Install (per user, no admin rights)

Download `IOC-Print-Agent.exe` from the [latest release](https://github.com/interact-operations-center/ioc-print-agent/releases/latest) and double-click it. It:

1. copies itself to `%LOCALAPPDATA%\Programs\IOC Print Agent\`;
2. adds itself to the user's startup programs (`HKCU\…\Run`);
3. starts in the background (no window) and shows a confirmation.

Running a newer download updates the installed copy (quit the running one first: Task Manager → IOC Print Agent). A second instance exits at once (port already in use).

- Logs: `%LOCALAPPDATA%\IOC Print Agent\agent.log`
- Uninstall: `"%LOCALAPPDATA%\Programs\IOC Print Agent\IOC Print Agent.exe" --uninstall`, then delete the install folder.
- Debug without installing: `python bridge.py`, or `IOC-Print-Agent.exe --foreground`.

It runs in the user session on purpose (not as a Windows service): services run in session 0 and do not see the user's printer connections.

## Release

The `.github/workflows/release.yml` workflow builds the .exe on a Windows runner, starts it to check that `/status` and `/printers` answer, and attaches it to a GitHub Release. To publish a version:

```bash
# bump VERSION in bridge.py, commit, then:
git tag v2.1.1 && git push origin v2.1.1
```

The workflow refuses a tag that doesn't match `VERSION`. A manual run (Actions → Release → Run workflow) only keeps the .exe as a workflow artifact.

Stable download link for the latest version: `https://github.com/interact-operations-center/ioc-print-agent/releases/latest/download/IOC-Print-Agent.exe` (repo is private: it needs a GitHub account with access).

Local build, on Windows:

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name IOC-Print-Agent --distpath output/dist --workpath output/build --specpath output bridge.py
```
