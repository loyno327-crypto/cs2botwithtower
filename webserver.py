"""
Веб-сервер Telegram Web App.

Работает в ТОМ ЖЕ процессе, что и бот (см. bot.py) — просто ещё один
asyncio-таск рядом с polling'ом. Отдаёт статику из папки webapp/ (сама игра)
и небольшое API, которое читает ту же базу данных, что и бот (database.py),
так что баланс/уровень всегда общие с текстовым ботом.

Порт, который слушает сервер, берётся из переменной окружения PORT
(её обычно сама выставляет платформа-хостинг, чтобы знать, куда
проксировать публичный HTTPS-адрес). Если переменной нет — используется
8080 для локального теста.
"""

import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qsl

from aiohttp import web

import config
import database as db

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


def _check_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """Проверяет подпись initData, которую Telegram Web App передаёт на
    фронтенде. Это единственный способ убедиться, что запрос реально
    пришёл из Telegram, а не подделан кем-то в браузере.
    Возвращает распарсенные данные пользователя, либо None, если подпись
    неверна/данных нет.
    Алгоритм описан в официальной документации Telegram:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    if not init_data:
        return None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    user_raw = pairs.get("user")
    if not user_raw:
        return None

    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None


def _get_init_data_from_request(request: web.Request) -> str:
    # Фронтенд шлёт initData в заголовке — так его не видно в логах сервера
    # и в истории браузера, в отличие от query-параметра.
    return request.headers.get("X-Telegram-Init-Data", "")


async def handle_me(request: web.Request) -> web.Response:
    init_data = _get_init_data_from_request(request)
    user_data = _check_telegram_init_data(init_data, config.BOT_TOKEN)

    if user_data is None:
        return web.json_response({"error": "invalid_init_data"}, status=401)

    user_id = user_data["id"]
    username = user_data.get("username") or user_data.get("first_name") or f"id{user_id}"

    db.get_or_create_user(user_id, username)
    balance = db.get_balance(user_id)
    level_info = db.get_level_info(user_id)

    return web.json_response({
        "user_id": user_id,
        "username": username,
        "balance": balance,
        "level": level_info["level"],
        "xp": level_info["xp"],
        "xp_needed": level_info["xp_needed"],
    })


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/me", handle_me)
    # Всё остальное — статика самой игры (index.html, css, js, картинки).
    app.router.add_static("/", WEBAPP_DIR, show_index=False)
    return app


async def start_webserver():
    port = int(os.environ.get("PORT", 8080))
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.info(f"Web App сервер запущен на порту {port}")
