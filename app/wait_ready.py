import sys
import time
import urllib.request


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/health/ready"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(1)
    raise SystemExit("api not ready")


if __name__ == "__main__":
    main()
