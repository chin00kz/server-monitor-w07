import json
import time
import urllib.parse
import urllib.request

import telegram_controls as controls


def long_poll():
    allowed = controls.read(controls.SECRETS / "telegram-chat-id")
    if not allowed:
        print("Telegram chat configuration missing", flush=True)
        time.sleep(10)
        return

    try:
        offset = json.loads(
            controls.OFFSET_FILE.read_text()
        ).get("offset", 0)
    except (OSError, ValueError):
        offset = 0

    token = controls.read(controls.SECRETS / "telegram-token")
    if not token:
        print("Telegram token missing", flush=True)
        time.sleep(10)
        return

    payload = urllib.parse.urlencode({
        "offset": offset,
        "timeout": 20,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }).encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=payload,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)

        if not result.get("ok"):
            return

        # Reuse the existing command handler for the received updates.
        # The handler reads the same persisted offset.
        if result.get("result"):
            controls.process_updates(result["result"])

    except Exception as exc:
        print(f"Telegram polling error: {type(exc).__name__}", flush=True)
        time.sleep(5)


def main():
    print("Telegram controller starting", flush=True)

    while True:
        long_poll()


if __name__ == "__main__":
    main()
