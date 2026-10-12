
import subprocess as _emergency_subprocess
import time as _emergency_time

_REBOOT_PENDING = {}

def _request_reboot(chat_id):
    _REBOOT_PENDING[str(chat_id)] = _emergency_time.monotonic() + 60
    api("sendMessage", {
        "chat_id": chat_id,
        "text": (
            "🔐 EMERGENCY REBOOT\n\n"
            "Authentication required\n\n"
            "Send the current 6-digit code from Ente Auth "
            "as a message in this chat.\n\n"
            "⏱️ Request expires in 60 seconds.\n\n"
            "⚠️ Successful verification will immediately restart "
            "the server and interrupt running services."
        ),
    })

def _verify_reboot(chat_id, code, message_id):
    try:
        api("deleteMessage", {
            "chat_id": chat_id,
            "message_id": message_id,
        })
    except Exception:
        pass

    expiry = _REBOOT_PENDING.get(str(chat_id), 0)

    if _emergency_time.monotonic() >= expiry:
        _REBOOT_PENDING.pop(str(chat_id), None)
        api("sendMessage", {
            "chat_id": chat_id,
            "text": "⌛ Reboot request expired. Start again.",
        })
        return

    if not (len(code) == 6 and code.isascii() and code.isdigit()):
        api("sendMessage", {
            "chat_id": chat_id,
            "text": "Enter a six-digit code from Ente Auth.",
        })
        return

    _REBOOT_PENDING.pop(str(chat_id), None)

    try:
        result = _emergency_subprocess.run(
            [
                "sudo", "-n", "/usr/bin/python3",
                "/usr/local/libexec/homelab-failover/verify_reboot_totp.py",
            ],
            input=code + "\n",
            text=True,
            capture_output=True,
            timeout=15,
        )
    except (OSError, _emergency_subprocess.TimeoutExpired):
        api("sendMessage", {
            "chat_id": chat_id,
            "text": "❌ Reboot authorization could not be completed.",
        })
        return

    if result.returncode != 0:
        api("sendMessage", {
            "chat_id": chat_id,
            "text": "❌ Invalid, expired, or blocked authentication code.",
        })
    else:
        api("sendMessage", {
            "chat_id": chat_id,
            "text": "🔄 Reboot authorized. Server restarting.",
        })

import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
OFFSET_FILE = ROOT / "data/alerts/telegram-offset.json"

def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None

def api(method, payload):
    token = read(SECRETS / "telegram-token")
    if not token:
        return None
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(payload).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.load(response)
            return result if result.get("ok") else None
    except Exception:
        return None

def run(*args):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "Unavailable"

def menu():
    return {
        "inline_keyboard": [
            [{"text": "Server Status", "callback_data": "server"},
             {"text": "Network Status", "callback_data": "network"}],
            [{"text": "Battery Info", "callback_data": "battery"},
             {"text": "Docker Status", "callback_data": "docker"}],
            [{"text": "Refresh", "callback_data": "server"}],
            [{"text": "Advanced / Emergency", "callback_data": "advanced"}],
        ]
    }


def advanced_menu():
    return {
        "inline_keyboard": [
            [{"text": "Check Both ISPs", "callback_data": "check_isps"}],
            [{"text": "Switch to Ethernet", "callback_data": "pending_ethernet"}],
            [{"text": "Switch to Starlink", "callback_data": "pending_starlink"}],
            [{"text": "Back to Default", "callback_data": "pending_default"}],
            [{"text": "Emergency Reboot", "callback_data": "pending_reboot"}],
            [{"text": "Back to Main Menu", "callback_data": "main_menu"}],
        ]
    }


def check_interface(name, interface):
    try:
        result = subprocess.run(
            ["ping", "-n", "-I", interface, "-c", "2", "-W", "2", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=7,
        )

        if result.returncode != 0:
            return f"{name}: Unreachable (ICMP test)"

        latency = "Unknown"
        for line in result.stdout.splitlines():
            if "min/avg/max" in line and "=" in line:
                latency = line.split("=")[1].strip().split("/")[1] + " ms"

        return f"{name}: Reachable\\nLatency: {latency}"
    except (OSError, subprocess.TimeoutExpired):
        return f"{name}: Test unavailable"


def internet_status():
    wifi = run("iw", "dev", "wlp2s0", "link")
    ssid = next(
        (line.strip().replace("SSID: ", "")
         for line in wifi.splitlines()
         if line.strip().startswith("SSID:")),
        "Unknown"
    )

    route = run("ip", "-4", "route", "get", "1.1.1.1")
    active = (
        "Wi-Fi" if "dev wlp2s0" in route
        else "Ethernet" if "dev enp1s0" in route
        else "Unknown"
    )

    def probe(interface):
        result = check_interface("Connection", interface)
        reachable = "Unreachable" not in result and "unavailable" not in result.lower()
        latency = "Unknown"
        if "Latency: " in result:
            latency = result.split("Latency: ", 1)[1].splitlines()[0]
        return reachable, latency

    # Probe both independent ISP interfaces concurrently.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        wifi_future = executor.submit(probe, "wlp2s0")
        eth_future = executor.submit(probe, "enp1s0")
        wifi_ok, wifi_ms = wifi_future.result()
        eth_ok, eth_ms = eth_future.result()

    def label(ok):
        return "🟢 Online" if ok else "🔴 Unreachable"

    overall = (
        "Both connections reachable" if wifi_ok and eth_ok
        else "One connection unavailable" if wifi_ok or eth_ok
        else "No connection verified"
    )

    return (
        "🌐 INTERNET CONNECTIVITY\n"
        f"{overall}\n\n"
        "📶 STARLINK / WI-FI\n"
        f"Network: {ssid}\n"
        f"Status: {label(wifi_ok)}\n"
        f"Latency: {wifi_ms if wifi_ok else '—'}\n"
        f"{'⭐ Preferred connection' if active == 'Wi-Fi' else 'Backup / Standby'}\n\n"
        "🔌 DIALOG ETHERNET\n"
        f"Status: {label(eth_ok)}\n"
        f"Latency: {eth_ms if eth_ok else '—'}\n"
        f"{'⭐ Preferred connection' if active == 'Ethernet' else 'Backup / Standby'}\n\n"
        f"⚙️ Active route: {active}"
    )


def status(kind):
    if kind == "server":
        uptime = run("uptime", "-p")
        memory = run("free", "-h")
        disk = shutil.disk_usage("/")
        return (
            "SERVER STATUS\n"
            f"Uptime: {uptime}\n"
            f"RAM:\n{memory[:500]}\n"
            f"Disk free: {disk.free / 1024**3:.1f} GiB"
        )
    if kind == "network":
        route = run("ip", "-4", "route", "get", "1.1.1.1")
        wifi = run("iw", "dev", "wlp2s0", "link")
        ssid = next((x.strip() for x in wifi.splitlines()
                     if x.strip().startswith("SSID:")), "SSID unavailable")
        return f"NETWORK STATUS\n{ssid}\n{route[:500]}"
    if kind == "battery":
        capacity = read("/sys/class/power_supply/BAT0/capacity")
        charging = read("/sys/class/power_supply/BAT0/status")
        return f"BATTERY STATUS\nCharge: {capacity or 'Unknown'}%\nState: {charging or 'Unknown'}"
    if kind == "docker":
        return "DOCKER STATUS\n" + run(
            "docker", "ps", "--format", "{{.Names}}: {{.Status}}"
        )[:3000]
    return "Unknown command"

def poll():
    allowed = read(SECRETS / "telegram-chat-id")
    if not allowed:
        return

    try:
        offset = json.loads(OFFSET_FILE.read_text()).get("offset", 0)
    except (OSError, ValueError):
        offset = 0

    response = api("getUpdates", {"offset": offset, "timeout": 0})
    if not response:
        return
    process_updates(response.get("result", []))


import secrets as _network_secrets

_NETWORK_PENDING = {}


def _network_confirmation(action):
    token = _network_secrets.token_hex(8)
    _NETWORK_PENDING.clear()
    _NETWORK_PENDING[token] = (action, _emergency_time.monotonic() + 60)
    return token


def _consume_network_confirmation(token, action):
    pending = _NETWORK_PENDING.pop(token, None)
    return (
        pending is not None
        and pending[0] == action
        and _emergency_time.monotonic() <= pending[1]
    )


def process_updates(updates):
    allowed = read(SECRETS / "telegram-chat-id")
    if not allowed:
        return

    try:
        offset = json.loads(OFFSET_FILE.read_text()).get("offset", 0)
    except (OSError, ValueError):
        offset = 0

    for update in updates:
        update_id = update["update_id"]
        offset = max(offset, update_id + 1)

        message = update.get("message") or {}
        callback = update.get("callback_query") or {}
        chat = message.get("chat") or callback.get("message", {}).get("chat", {})
        sender = message.get("from") or callback.get("from") or {}

        chat_id = str(chat.get("id", ""))
        sender_id = str(sender.get("id", ""))

        if (
            chat_id != allowed
            or sender_id != allowed
            or chat.get("type") != "private"
        ):
            continue

        if callback:
            api("answerCallbackQuery", {
                "callback_query_id": callback["id"]
            })

        command = message.get("text", "").split("@")[0].strip().lower()
        action = callback.get("data", "")

        if action == "pending_reboot":
            _request_reboot(allowed)

        elif message and str(allowed) in _REBOOT_PENDING:
            if command in ("/cancel", "/start", "/menu", "/advanced"):
                _REBOOT_PENDING.pop(str(allowed), None)
            else:
                _verify_reboot(
                    allowed,
                    (message.get("text") or "").strip(),
                    message["message_id"],
                )
                continue

        if command == "/advanced" or action == "advanced":
            api("sendMessage", {
                "chat_id": allowed,
                "text": "Advanced / Emergency Controls",
                "reply_markup": json.dumps(advanced_menu()),
            })
        elif action == "main_menu":
            api("sendMessage", {
                "chat_id": allowed,
                "text": "Homelab Control Panel",
                "reply_markup": json.dumps(menu()),
            })
        elif action == "check_isps":
            api("sendChatAction", {
                "chat_id": allowed,
                "action": "typing",
            })
            api("sendMessage", {
                "chat_id": allowed,
                "text": internet_status(),
                "reply_markup": json.dumps(advanced_menu()),
            })
        elif action in (
            "pending_ethernet",
            "pending_starlink",
            "pending_default",
        ):
            requested = action.removeprefix("pending_")
            descriptions = {
                "ethernet": (
                    "Switch preferred connection to Dialog Ethernet "
                    "for up to 60 minutes?"
                ),
                "starlink": (
                    "Switch preferred connection to Starlink "
                    "for up to 60 minutes?"
                ),
                "default": (
                    "Remove manual override and restore "
                    "automatic ISP failover?"
                ),
            }
            api("sendMessage", {
                "chat_id": allowed,
                "text": (
                    "🌐 NETWORK CONTROL\\n\\n"
                    + descriptions[requested]
                    + "\\n\\nConfirm only if you can tolerate "
                      "a brief network interruption."
                ).replace("\\n", "\n"),
                "reply_markup": json.dumps({
                    "inline_keyboard": [
                        [
                            {
                                "text": "Confirm",
                                "callback_data": (
                                    "confirm_net_" + requested + "_"
                                    + _network_confirmation(requested)
                                ),
                            },
                            {
                                "text": "Cancel",
                                "callback_data": "cancel_net",
                            },
                        ]
                    ]
                }),
            })

        elif action == "cancel_net":
            _NETWORK_PENDING.clear()
            api("sendMessage", {
                "chat_id": allowed,
                "text": "Network change cancelled.",
                "reply_markup": json.dumps(advanced_menu()),
            })

        elif action.startswith("confirm_net_"):
            parts = action.removeprefix("confirm_net_").split("_", 1)
            requested = parts[0]
            token = parts[1] if len(parts) == 2 else ""

            if (
                requested not in ("ethernet", "starlink", "default")
                or not _consume_network_confirmation(token, requested)
            ):
                api("sendMessage", {
                    "chat_id": allowed,
                    "text": (
                        "This confirmation has expired or was already used. "
                        "Open Advanced Controls to request a new one."
                    ),
                    "reply_markup": json.dumps(advanced_menu()),
                })
                continue

            api("sendMessage", {
                "chat_id": allowed,
                "text": "Applying network preference. Please wait...",
            })

            try:
                result = _emergency_subprocess.run(
                    [
                        "sudo", "-n", "/usr/bin/python3",
                        "/usr/local/libexec/homelab-failover/"
                        "emergency_control.py",
                        requested,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

                if result.returncode == 0:
                    response = (
                        "✅ NETWORK CONTROL SUCCESSFUL\\n\\n"
                        + result.stdout.strip()
                    )
                else:
                    response = (
                        "⚠️ NETWORK CONTROL FAILED\\n\\n"
                        + (
                            result.stderr.strip()
                            or result.stdout.strip()
                            or "Unknown error"
                        )
                    )

            except _emergency_subprocess.TimeoutExpired:
                response = (
                    "⚠️ Network operation timed out. "
                    "The watchdog and automatic failover timers "
                    "remain active. Check ISP status."
                )
            except Exception as exc:
                response = "⚠️ Network operation error: " + str(exc)

            api("sendMessage", {
                "chat_id": allowed,
                "text": response.replace("\\n", "\n")[:3500],
                "reply_markup": json.dumps(advanced_menu()),
            })
        elif command in ("/menu", "/start"):
            api("sendMessage", {
                "chat_id": allowed,
                "text": "Homelab Control Panel",
                "reply_markup": json.dumps(menu()),
            })
        elif command == "/status" or action in (
            "server", "network", "battery", "docker"
        ):
            api("sendMessage", {
                "chat_id": allowed,
                "text": status(action or "server"),
                "reply_markup": json.dumps(menu()),
            })

    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = OFFSET_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"offset": offset}))
    os.replace(temp, OFFSET_FILE)
