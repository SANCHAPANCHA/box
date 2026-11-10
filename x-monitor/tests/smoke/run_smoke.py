"""Process-level smoke test without real credentials.

Starts fake Telegram Bot API + fake X API servers, runs the real ``main.py`` against them
(PostgreSQL required) and drives a scenario: /start, add account, new X activity -> notification,
restart -> no duplicates, unauthorized user, health endpoint, secrets not in logs.

    DATABASE_URL=postgresql+asyncpg://... python -m tests.smoke.run_smoke
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.x_client import snowflake_from_datetime  # noqa: E402

PORT = 9911
API_PORT = 8099
TOKEN = "123456789:AAFakeTokenForSmokeTest_abcdefghijklmnop"
BEARER = "AAAAAAAAAAAAAAAAAAAAAFakeBearerForSmoke123"
OWNER = 1001
X_USER_ID = 44196397

# ------------------------------------------------------------------ fake services

app = FastAPI()
updates: list[dict] = []
sent: list[dict] = []
x_posts: list[dict] = []
x_includes: dict = {"users": [], "tweets": []}
_next = {"update": 1, "message": 100}


def _message(chat_id: int, text: str) -> dict:
    _next["message"] += 1
    return {
        "message_id": _next["message"],
        "date": int(time.time()),
        "chat": {"id": int(chat_id), "type": "private"},
        "text": text,
    }


@app.post("/tg/bot{token}/{method}")
async def telegram(token: str, method: str, request: Request) -> dict:
    if token != TOKEN:
        return {"ok": False, "error_code": 401, "description": "Unauthorized"}
    form = dict(await request.form())
    if method == "getMe":
        return {"ok": True, "result": {"id": 42, "is_bot": True, "first_name": "Fake", "username": "fake_bot"}}
    if method == "getUpdates":
        offset = int(form.get("offset") or 0)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            ready = [u for u in updates if u["update_id"] >= offset]
            if ready:
                return {"ok": True, "result": ready}
            await asyncio.sleep(0.05)
        return {"ok": True, "result": []}
    if method in ("sendMessage", "editMessageText"):
        sent.append({"method": method, **form})
        return {"ok": True, "result": _message(form.get("chat_id") or OWNER, form.get("text", ""))}
    return {"ok": True, "result": True}


@app.get("/x/2/users/by/username/{username}")
async def x_user(username: str, request: Request) -> dict:
    assert request.headers["authorization"] == f"Bearer {BEARER}"
    if username.lower() != "elonmusk":
        return {
            "errors": [
                {"detail": "Could not find user", "type": "https://api.twitter.com/2/problems/resource-not-found"}
            ]
        }
    return {"data": {"id": str(X_USER_ID), "username": "elonmusk", "name": "Elon Musk"}}


@app.get("/x/2/users/{user_id}/tweets")
async def x_timeline(user_id: str) -> dict:
    # Deliberately ignores since_id: the app itself must never notify twice.
    return {"data": list(reversed(x_posts)), "includes": x_includes, "meta": {"result_count": len(x_posts)}}


def push_update(payload: dict) -> None:
    payload["update_id"] = _next["update"]
    _next["update"] += 1
    updates.append(payload)


def push_text(text: str, user_id: int = OWNER) -> None:
    push_update(
        {
            "message": {
                "message_id": _next["update"] + 5000,
                "date": int(time.time()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                "text": text,
            }
        }
    )


# ------------------------------------------------------------------ driver


async def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.2)
    raise AssertionError(f"timeout waiting for: {what}")


def notifications() -> list[dict]:
    return [m for m in sent if "НОВАЯ АКТИВНОСТЬ" in m.get("text", "")]


def start_app(log_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{PORT}/tg",
        "TELEGRAM_ALLOWED_USER_IDS": str(OWNER),
        "X_BEARER_TOKEN": BEARER,
        "X_API_BASE_URL": f"http://127.0.0.1:{PORT}/x/2",
        "DEFAULT_POLL_INTERVAL_SECONDS": "15",
        "MONITOR_TICK_SECONDS": "1",
        "DISPATCHER_TICK_SECONDS": "1",
        "API_PORT": str(API_PORT),
        "LOG_LEVEL": "DEBUG",
    }
    env.pop("REDIS_URL", None)
    log = log_path.open("a")
    return subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def stop_app(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=20)


def add_x_post(post_id: int, text: str, refs: list[dict] | None = None, reply_to: str | None = None) -> None:
    item = {
        "id": str(post_id),
        "author_id": str(X_USER_ID),
        "text": text,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    if refs:
        item["referenced_tweets"] = refs
    if reply_to:
        item["in_reply_to_user_id"] = reply_to
    x_posts.append(item)


async def scenario(log_path: Path) -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True, env=os.environ)
    proc = start_app(log_path)
    try:
        async with httpx.AsyncClient() as http:
            push_text("/start")
            await wait_for(lambda: any("X Monitor" in m.get("text", "") for m in sent), 30, "main menu")
            print("✔ /start -> main menu")

            push_text("/start", user_id=777)
            await wait_for(lambda: any("Доступ запрещён" in m.get("text", "") for m in sent), 10, "access denied")
            print("✔ unauthorized user rejected")

            # Old post (before the account is added) must never be notified.
            add_x_post(snowflake_from_datetime(datetime.now(UTC) - timedelta(days=2)), "ancient post")
            push_text("@elonmusk")
            await wait_for(lambda: any("успешно добавлен" in m.get("text", "") for m in sent), 15, "account added")
            print("✔ account added")

            x_includes["users"].append({"id": "11348282", "username": "NASA", "name": "NASA"})
            x_includes["tweets"].append({"id": "1", "author_id": "11348282", "text": "We have liftoff!"})
            add_x_post(
                snowflake_from_datetime(datetime.now(UTC) + timedelta(seconds=1)),
                "@NASA congrats <team> & co",
                refs=[{"type": "replied_to", "id": "1"}],
                reply_to="11348282",
            )
            await wait_for(lambda: notifications(), 40, "reply notification")
            note = notifications()[0]["text"]
            assert "Ответ на публикацию @NASA" in note and "We have liftoff!" in note, note
            assert "&lt;team&gt; &amp; co" in note, note
            print("✔ reply detected and delivered")

            health = await http.get(f"http://127.0.0.1:{API_PORT}/health")
            assert health.status_code == 200 and health.json()["database"] == "ok", health.text
            print("✔ /health ok:", health.json())
        stop_app(proc)
        print("✔ graceful shutdown")

        proc = start_app(log_path)
        await asyncio.sleep(25)  # > one poll interval; the fake X API returns the same posts again
        assert len(notifications()) == 1, f"duplicates after restart: {len(notifications())}"
        print("✔ no duplicates after restart")

        add_x_post(
            snowflake_from_datetime(datetime.now(UTC) + timedelta(seconds=1)),
            "my comment",
            refs=[{"type": "quoted", "id": "1"}],
        )
        await wait_for(lambda: len(notifications()) == 2, 40, "quote notification")
        assert "Цитата" in notifications()[1]["text"]
        await asyncio.sleep(18)
        assert len(notifications()) == 2
        print("✔ quote detected once")
    finally:
        if proc.poll() is None:
            stop_app(proc)

    logs = log_path.read_text()
    assert TOKEN not in logs and BEARER not in logs, "secret leaked into logs"
    assert "Traceback" not in logs, "unexpected traceback in logs"
    print("✔ no secrets / tracebacks in logs")


async def main() -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1)
    log_path = Path("/tmp/x-monitor-smoke.log")
    log_path.write_text("")
    try:
        await scenario(log_path)
        print("SMOKE TEST PASSED")
    finally:
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    asyncio.run(main())
