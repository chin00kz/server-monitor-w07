from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
import sys

URL_FILE = (
    Path.home()
    / "homelab/docker/server-monitor/secrets/healthchecks-url"
)

def main():
    try:
        url = URL_FILE.read_text().strip()
        parsed = urlparse(url)

        if parsed.scheme != "https" or parsed.hostname != "hc-ping.com":
            print("Invalid Healthchecks URL configuration.")
            return 1

        with urlopen(url, timeout=10) as response:
            if response.status != 200:
                print(f"Heartbeat failed: HTTP {response.status}")
                return 1

        print("Heartbeat OK")
        return 0

    except Exception as exc:
        print(f"Heartbeat failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
