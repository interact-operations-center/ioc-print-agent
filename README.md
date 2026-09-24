# IOC Print Agent

Local agent that prints labels/receipts for [IOC](https://github.com/interact-operations-center/ioc).

The IOC server cannot reach the printers directly (they sit on isolated local networks), so it queues `PrintJob`s instead. This agent runs on a machine on the printers' network and loops:

1. `GET {serverUrl}/api/printer/agent/poll` with `Authorization: Bearer <token>` → `{ jobs: [{ id, targetUrl, contentType, body }] }`
2. `POST` each job's `body` to its `targetUrl` (the printer's ePOS endpoint)
3. `POST {serverUrl}/api/printer/agent/result` with `{ jobId, ok, error }`

The server side of this protocol lives in the IOC repo (`app/api/printer/agent/`). Any change to the payload format must be made in both repos.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json
```

In `config.json`:

- `serverUrl` — the IOC base URL
- `tokens` — one agent token per printer this machine serves (copied from the printer's edit page in IOC; regenerating it there invalidates the old one)
- `pollIntervalSeconds` — optional, defaults to 2

`config.json` contains secrets and is git-ignored. Set `AGENT_CONFIG` to load it from another path.

## Run

```bash
python agent.py
```

## Build a standalone executable

```bash
pyinstaller --distpath output/dist --workpath output/build --specpath output agent.py
```
