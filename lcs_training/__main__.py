"""python -m lcs_training  -> starts the app and opens it in the browser."""
import os
import threading
import webbrowser

import uvicorn

from .config import HOST, PORT


def main() -> None:
    url = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '') else HOST}:{PORT}"
    if not os.environ.get("LCS_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"LCS-Training is running at {url}  (Ctrl+C to stop)")
    uvicorn.run("lcs_training.app:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
