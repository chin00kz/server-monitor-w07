import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "homelab/docker/server-monitor"
SECRETS = ROOT / "secrets"
DATA = ROOT / "data/alerts"
STATE_FILE = DATA / "state.json"
QUEUE_FILE = DATA / "queue.json"

INTERVAL = 30


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2))
    os.replace(temp, path)


def log(message):
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with (DATA / "events.log").open("a") as file:
        file.write(f"{stamp} {message}\n")
    print(f"{stamp} {message}", flush=True)


def online(interface):
    try:
        result = subprocess.run(
            ["ping", "-I", interface, "-c", "1", "-W", "2", "1.1.1.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def preferred_network():
    try:
        result = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        parts = result.stdout.split()
        return parts[parts.index("dev") + 1] if result.returncode == 0 and "dev" in parts else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def snapshot():
    capacity = read("/sys/class/power_supply/BAT0/capacity")
    try:
        battery = int(capacity)
    except (TypeError, ValueError):
        battery = None

    if battery is None:
        battery_level = "unknown"
    elif battery < 10:
        battery_level = "critical"
    elif battery < 25:
        battery_level = "warning"
    else:
        battery_level = "normal"

    return {
        "charger": read("/sys/class/power_supply/AC/online"),
        "battery_level": battery_level,
        "battery_percent": battery,
        "starlink": online("wlp2s0"),
        "dialog": online("enp1s0"),
        "preferred": preferred_network(),
    }


def send_telegram(message):
    token = read(SECRETS / "telegram-token")
    chat_id = read(SECRETS / "telegram-chat-id")
    if not token or not chat_id:
        return False

    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
    }).encode()

    try:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.load(response).get("ok") is True
    except Exception:
        return False


def notify(severity, message, queue):
    text = f"[{severity.upper()}] {message}"
    queue.append(text)
    if len(queue) > 500:
        del queue[:-500]
        log("Alert queue limit reached; oldest notifications discarded")
    log(f"Queued: {text}")


def flush_queue(queue):
    while queue:
        if not send_telegram(queue[0]):
            break
        log(f"Delivered: {queue[0]}")
        queue.pop(0)
        save_json(QUEUE_FILE, queue)


def detect(previous, current, queue):
    if previous["charger"] != current["charger"]:
        if current["charger"] == "0":
            notify("warning", "Server charger disconnected. Running on battery.", queue)
        elif current["charger"] == "1":
            notify("info", "Server charger reconnected.", queue)

    if previous["battery_level"] != current["battery_level"]:
        level = current["battery_level"]
        percent = current["battery_percent"]
        if level == "warning":
            notify("warning", f"Server battery low: {percent}%.", queue)
        elif level == "critical":
            notify("critical", f"Server battery critically low: {percent}%.", queue)
        elif level == "normal":
            notify("info", f"Server battery recovered to {percent}%.", queue)

    for key, name in (("starlink", "Starlink Wi-Fi"), ("dialog", "Dialog Ethernet")):
        if previous[key] != current[key]:
            if current[key]:
                notify("info", f"{name} internet connection restored.", queue)
            else:
                notify("warning", f"{name} internet connection unavailable.", queue)

    if previous["preferred"] != current["preferred"]:
        notify(
            "info",
            f"Preferred network changed: {previous['preferred']} -> {current['preferred']}.",
            queue,
        )

    if (previous["starlink"] or previous["dialog"]) and not (
        current["starlink"] or current["dialog"]
    ):
        notify("critical", "Both internet connections appear unavailable.", queue)

    if not (previous["starlink"] or previous["dialog"]) and (
        current["starlink"] or current["dialog"]
    ):
        notify("info", "Internet connectivity recovered.", queue)


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    state = load_json(STATE_FILE, None)
    queue = load_json(QUEUE_FILE, [])
    failures = {"starlink": 0, "dialog": 0}
    recoveries = {"starlink": 0, "dialog": 0}

    log("Alert monitor started")

    while True:
        try:
            current = snapshot()

            for key in ("starlink", "dialog"):
                observed = current[key]

                if observed:
                    failures[key] = 0
                    recoveries[key] += 1
                else:
                    recoveries[key] = 0
                    failures[key] += 1

                if state is not None:
                    if state[key] and not observed and failures[key] < 3:
                        current[key] = True
                    elif not state[key] and observed and recoveries[key] < 2:
                        current[key] = False

            if state is None:
                state = current
                log(f"Initial baseline: {current}")
            else:
                detect(state, current, queue)
                state = current

            save_json(STATE_FILE, state)
            save_json(QUEUE_FILE, queue)

            if current["starlink"] or current["dialog"]:
                flush_queue(queue)

        except Exception as exc:
            log(f"Monitoring error: {type(exc).__name__}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Alert monitor stopped.", flush=True)
