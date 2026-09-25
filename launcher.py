"""Start Zhixu locally and open it after the server is ready."""

import threading
import time
import urllib.request
import webbrowser

import uvicorn


URL = "http://127.0.0.1:8000/"


def open_when_ready() -> None:
    for _ in range(40):
        try:
            with urllib.request.urlopen(URL, timeout=1):
                webbrowser.open(URL)
                return
        except Exception:
            time.sleep(.25)


if __name__ == "__main__":
    threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run("app:app", host="127.0.0.1", port=8000)
