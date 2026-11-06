"""Temporary CI capture; only synthetic requests and scrubbed output leave the runner."""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    root, target = Path(sys.argv[1]), Path(sys.argv[2])
    key = secrets.token_urlsafe(32)
    wrong = "SYNTHETIC_WRONG_CAPTURE_KEY"
    env = {**os.environ, "LLAMA_API_KEY": key, "LLAMA_ARG_API_KEY": key}
    binary = root / "llama" / "llama-server.exe"
    model = next((root / "models").glob("*.gguf"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def scrub(text):
        text = text.replace(key, "<capture-api-key>").replace(wrong, "<wrong-api-key>")
        text = text.replace(str(Path.home()), r"C:\Users\fixture-user")
        return re.sub(r"(?i)([A-Z]:[\\/]Users[\\/])[^\\/\s]+", r"\1fixture-user", text)

    def args(path, port):
        return [str(binary), "--host", "127.0.0.1", "--port", str(port), "-m", str(path),
                "-c", "4096", "-t", str(max(1, (os.cpu_count() or 2) // 2 - 1)), "--parallel", "1"]

    def request(port, auth=None, body=None):
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = "Bearer " + auth
        url = f"http://127.0.0.1:{port}" + ("/health" if body is None else "/v1/chat/completions")
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                     headers=headers)
        try:
            with opener.open(req, timeout=120) as response:
                return {"status": response.status, "body": scrub(response.read().decode())}
        except urllib.error.HTTPError as exc:
            return {"status": exc.code, "body": scrub(exc.read().decode())}

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    lines = []
    proc = subprocess.Popen(args(model, port), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, text=True, encoding="utf-8", errors="replace")

    def read():
        for line in proc.stdout:
            lines.append(scrub(line.rstrip("\r\n")))

    reader = threading.Thread(target=read)
    reader.start()
    data = {"version": "b8936", "platform": sys.platform, "source_run": 42206790413,
            "argv": [scrub(arg) for arg in args(model, port)], "responses": {}, "cases": {}}
    body = {"messages": [{"role": "user", "content": "Reply with CAPTURE_COMPLETION_CANARY."}],
            "max_tokens": 8, "temperature": 0, "seed": 0}
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                if request(port, key)["status"] == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("Native server did not become healthy")
        data["responses"]["generation"] = request(port, key, body)
        data["responses"]["wrong_key"] = request(port, wrong, body)
        data["responses"]["no_key"] = request(port, None, body)
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        reader.join(timeout=10)
        data["cases"]["startup_generation_wrong_and_no_key"] = lines
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert data["responses"]["generation"]["status"] == 200
    assert data["responses"]["wrong_key"]["status"] == 401
    assert data["responses"]["no_key"]["status"] == 401
    bad = subprocess.run(args(model.with_name("missing-model.gguf"), port), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=env, timeout=60)
    data["cases"]["bad_model_path"] = scrub(bad.stdout.decode("utf-8", errors="replace")).splitlines()
    assert bad.returncode != 0
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen()
        busy = subprocess.run(args(model, port), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, timeout=120)
    data["cases"]["port_in_use"] = scrub(busy.stdout.decode("utf-8", errors="replace")).splitlines()
    assert busy.returncode != 0
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps({"cases": {name: len(lines) for name, lines in data["cases"].items()},
                      "statuses": {name: value["status"] for name, value in data["responses"].items()}}))


if __name__ == "__main__":
    main()
