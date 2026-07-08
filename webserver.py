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
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Кейсы читаем один раз при старте процесса и держим в памяти — это тот же
# файл, которым пользуется handlers/cases.py, так что список в Web App
# всегда совпадает с тем, что реально можно открыть в боте.
_CASES_CACHE: list | None = None


def _load_cases() -> list:
    global _CASES_CACHE
    if _CASES_CACHE is None:
        cases_path = os.path.join(STATIC_DIR, "cases.json")
        with open(cases_path, "r", encoding="utf-8") as f:
            _CASES_CACHE = json.load(f)
    return _CASES_CACHE


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


def _authenticate(request: web.Request) -> dict | None:
    """Общая проверка подписи для всех защищённых эндпоинтов. Возвращает
    словарь Telegram-пользователя (id, username, ...) или None, если
    initData отсутствует/невалиден — тогда вызывающий обработчик сам
    должен вернуть 401."""
    init_data = _get_init_data_from_request(request)
    return _check_telegram_init_data(init_data, config.BOT_TOKEN)


def _unauthorized() -> web.Response:
    return web.json_response({"error": "invalid_init_data"}, status=401)


async def handle_me(request: web.Request) -> web.Response:
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

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


async def handle_inventory(request: web.Request) -> web.Response:
    """Отдаёт реальный инвентарь игрока — те же предметы, что видны
    в боте через "📦 Инвентарь" (handlers/inventory.py), одна и та же
    таблица inventory в БД."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    rows = db.get_inventory(user_id)

    items = [
        {
            "id": row["id"],
            "name": row["item_name"],
            "rarity": row["item_rarity"],
            "price": row["item_price"],
            "obtained_at": row["obtained_at"],
        }
        for row in rows
    ]

    return web.json_response({
        "items": items,
        "count": len(items),
        "total_value": sum(i["price"] for i in items),
    })


async def handle_stats(request: web.Request) -> web.Response:
    """Отдаёт подробную статистику игрока — те же цифры, что доступны
    в боте через "👤 Профиль" (handlers/stats.py)."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    full = db.get_user_full_stats(user_id)
    user = full["user"]

    if user is None:
        return web.json_response({"error": "user_not_found"}, status=404)

    level_info = db.get_level_info(user_id)

    return web.json_response({
        "level": level_info["level"],
        "xp": level_info["xp"],
        "xp_needed": level_info["xp_needed"],
        "balance": user["balance"],
        "total_earned": user["total_earned"] or 0,
        "total_spent": user["total_spent"] or 0,
        "cases_opened": user["cases_opened"] or 0,
        "duels_played": user["duels_played"] or 0,
        "duels_won": user["duels_won"] or 0,
        "upgrades_success": user["upgrades_success"] or 0,
        "upgrades_failed": user["upgrades_failed"] or 0,
        "jackpot_wins": user["jackpot_wins"] or 0,
        "work_correct": user["work_correct"] or 0,
        "work_wrong": user["work_wrong"] or 0,
        "items_count": full["items_count"],
        "items_total_value": full["items_total_value"],
        "best_item_name": full["best_item_name"],
        "best_item_price": full["best_item_price"],
    })


async def handle_shop_cases(request: web.Request) -> web.Response:
    """Отдаёт список кейсов (тот же static/cases.json, которым пользуется
    handlers/cases.py) — пока только для просмотра. Сама покупка/открытие
    кейса прямо из Web App появится на Этапе 7 ("Скины + магазин за
    монеты"); сейчас кейсы по-прежнему открываются командой в боте."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    cases = _load_cases()
    # Наружу отдаём укороченную витрину: без весов дропа (это внутренняя
    # механика шанса) и не более 4 примеров предметов на кейс.
    preview = []
    for case in cases:
        items_sorted = sorted(case["items"], key=lambda i: i["price"], reverse=True)
        preview.append({
            "id": case["id"],
            "name": case["name"],
            "price": case["price"],
            "min_level": case["min_level"],
            "sample_items": [
                {"name": i["name"], "rarity": i["rarity"], "price": i["price"]}
                for i in items_sorted[:4]
            ],
        })

    return web.json_response({"cases": preview})


async def handle_index(request: web.Request) -> web.Response:
    # aiohttp's add_static НЕ отдаёт index.html автоматически на "/" —
    # он трактует "/" как запрос к самой папке и при show_index=False
    # отвечает 403 Forbidden. Поэтому явный маршрут для корня обязателен.
    return web.FileResponse(os.path.join(WEBAPP_DIR, "index.html"))


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/inventory", handle_inventory)
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/api/shop/cases", handle_shop_cases)
    app.router.add_get("/", handle_index)
    # Всё остальное (css, js, картинки) — статика самой игры.
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
