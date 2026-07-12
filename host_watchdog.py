#!/usr/bin/env python3
import base64
import json
import os
import subprocess
import time
import urllib.request

PROJECT_DIR = "/root/tg-video-downloader-aria2"
ENV_PATH = os.path.join(PROJECT_DIR, ".env")
STATUS_URL = "http://127.0.0.1:5003/api/download_status"
HEALTH_URL = "http://127.0.0.1:5003/api/health"
STALE_SECONDS = 300
LOG_PATH = os.path.join(PROJECT_DIR, "logs", "host_watchdog.log")


def log(message):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + message
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def read_env():
    env = {}
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def get_json(url, env):
    req = urllib.request.Request(url)
    user = env.get("WEB_AUTH_USERNAME", "")
    password = env.get("WEB_AUTH_PASSWORD", "")
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode())


def restart(reason):
    log("restart: " + reason)
    subprocess.run(
        ["docker", "compose", "restart", "tg-downloader"],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )


def main():
    env = read_env()
    now = time.time()
    try:
        status = get_json(STATUS_URL, env)
        health = get_json(HEALTH_URL, env)
    except Exception as exc:
        restart(f"api unavailable: {type(exc).__name__}: {exc}")
        return

    queue = status.get("queue") or {}
    tasks = status.get("tasks") or {}
    active = int(queue.get("active") or 0)
    queued = int(queue.get("queued") or 0)
    if active <= 0:
        if queued > 0:
            newest = max((float(t.get("updated_at") or 0) for t in tasks.values()), default=0)
            age = now - newest if newest else 999999
            if age >= STALE_SECONDS:
                restart(f"queue stalled: active=0 queued={queued} newest_age={int(age)}s")
        return

    for task_id, task in tasks.items():
        state = task.get("status")
        if state not in {"submitting", "downloading", "submitted"}:
            continue
        updated_at = float(task.get("updated_at") or 0)
        age = now - updated_at if updated_at else 999999
        speed = float(task.get("speed_bps") or 0)
        error = (health.get("telegram") or {}).get("error") or health.get("error") or ""
        if age >= STALE_SECONDS:
            restart(
                f"stale task {task_id}: state={state} age={int(age)}s "
                f"downloaded={task.get('downloaded')} speed={speed} error={error!r}"
            )
            return


if __name__ == "__main__":
    main()

