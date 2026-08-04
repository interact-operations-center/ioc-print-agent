import json
import os
import sys
import time

import requests

CONFIG_PATH = os.environ.get("AGENT_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))

DEFAULT_POLL_INTERVAL_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 15


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    if not config.get("serverUrl"):
        raise ValueError("config.json is missing 'serverUrl'")
    if not config.get("tokens"):
        raise ValueError("config.json is missing 'tokens' (list of printer agent tokens)")

    return config


def poll_and_print(session: requests.Session, server_url: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}

    resp = session.get(f"{server_url}/api/printer/agent/poll", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])

    for job in jobs:
        ok = True
        error = None

        try:
            print_resp = session.post(
                job["targetUrl"],
                data=job["body"].encode("utf-8"),
                headers={"Content-Type": job["contentType"]},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            ok = print_resp.ok and 'success="false"' not in print_resp.text
            if not ok:
                error = print_resp.text[:500]
        except requests.RequestException as e:
            ok = False
            error = str(e)

        try:
            session.post(
                f"{server_url}/api/printer/agent/result",
                headers=headers,
                json={"jobId": job["id"], "ok": ok, "error": error},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            print(f"[agent] failed to report result for job {job['id']}: {e}", file=sys.stderr)


def main() -> None:
    config = load_config()
    server_url = config["serverUrl"].rstrip("/")
    tokens = config["tokens"]
    interval = config.get("pollIntervalSeconds", DEFAULT_POLL_INTERVAL_SECONDS)

    session = requests.Session()

    print(f"[agent] polling {server_url} every {interval}s for {len(tokens)} printer(s)")

    while True:
        for token in tokens:
            try:
                poll_and_print(session, server_url, token)
            except requests.RequestException as e:
                print(f"[agent] poll failed: {e}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    main()
