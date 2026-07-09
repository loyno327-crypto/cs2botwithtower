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
import random
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config
import database as db
from handlers.crash import generate_crash_point
from handlers import cases as cases_handlers

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


# ---------- Конфиг боя (Этап 3, MVP игрового цикла) ----------
#
# Единственный источник правды по балансу боя. Фронтенд (webapp/index.html)
# запрашивает его через /api/battle/config и симулирует бой ЛОКАЛЬНО на
# Canvas (сервер не участвует в реальном времени боя — это нормально для
# однопользовательской PvE-волны). Когда бой заканчивается, фронтенд шлёт
# в /api/battle/finish только итог (side, wave_reached, won), а награда
# считается здесь же, на сервере, по тем же цифрам — чтобы игрок не мог
# подделать монеты, изменив число в браузере.
#
# Броня/каска/промахи/хедшоты добавлены на Этапе 4 (см. ниже).
# Экономика самих башен (стоимость, редкость, апгрейды) — Этапы 5-7,
# поэтому пока башни бесплатны и одинаковы (пока только один тип бойца,
# условно вооружённый "AK-47" — реальный выбор оружия появится вместе
# с редкостями бойцов на Этапе 5).
BATTLE_CONFIG = {
    "wave_count": 5,
    "point_hp": 100,
    "enemy_damage_to_point": 10,
    "base_enemy_count": 6,
    "enemy_count_step": 2,
    "base_enemy_hp": 40,
    "enemy_hp_step": 15,
    "base_enemy_speed": 0.09,   # доля пути в секунду
    "enemy_speed_step": 0.01,
    "max_towers": 4,
    "tower_range": 150,
    "tower_fire_interval": 0.6,  # секунд между выстрелами
    "tower_damage": 18,
    "reward_per_wave": 15,
    "reward_win_bonus": 50,
    "xp_per_wave": 3,
    "xp_win_bonus": 15,

    # ---- Этап 4: точность, зоны попадания, броня/каска ----
    # Базовая башня 1 уровня попадает не всегда — апгрейды (Этап 6) будут
    # поднимать это значение. Из попаданий часть — в голову.
    "tower_accuracy": 0.7,
    "tower_headshot_chance": 0.22,
    # Множитель урона по голове ДО учёта каски (как в CS: голова без
    # каски — почти всегда смертельна, с каской — уже не факт).
    "headshot_multiplier": 2.4,
    # Обычная броня режет урон по телу; каска — дополнительно режет урон
    # по голове (независимо от брони, как в реальном CS).
    "armor_damage_reduction": 0.35,
    "helmet_headshot_reduction": 0.45,
    # Элитные враги (последняя волна) — усиленная версия обоих параметров
    # плюс прибавка к скорости движения по пути.
    "elite_armor_reduction": 0.5,
    "elite_helmet_reduction": 0.55,
    "elite_speed_multiplier": 1.25,
    # Косметика Kill Feed — реальный выбор оружия/скинов появится на Этапах 5-7.
    "tower_weapon_name": "AK-47",
    "tower_weapon_icon": "🔫",
}


# ---------- Crash (перенос из handlers/crash.py в Web App) ----------
#
# Точка краха и формула роста множителя берутся из ТОГО ЖЕ кода, что
# использует бот (generate_crash_point импортирован из handlers/crash.py,
# CRASH_* константы — из общего config.py), так что вероятность краха и
# скорость роста один в один совпадают с ботом.
#
# В боте множитель растёт дискретными тиками (редактирование сообщения
# каждые CRASH_TICK_SECONDS) — в Web App вместо этого отдаём клиенту
# время старта раунда и считаем множитель НЕПРЕРЫВНОЙ функцией от
# прошедшего времени по той же формуле сложного роста
# (1 + CRASH_GROWTH_PER_TICK) ** (elapsed / CRASH_TICK_SECONDS) — в
# моменты, кратные тику, значение совпадает с бото-версией один в один,
# а между тиками получается гладкая кривая вместо скачков.
#
# Раунд — активная игра в памяти процесса (как active_games в
# handlers/crash.py), сервер — единственный источник правды по итогу:
# клиент не может подделать множитель на кэшауте, т.к. выплата всегда
# считается по СЕРВЕРНОМУ прошедшему времени, а не по числу, присланному
# из браузера.
_CRASH_GAMES: dict[int, dict] = {}


def _crash_multiplier_at(elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 1.0
    ticks = elapsed_seconds / config.CRASH_TICK_SECONDS
    return round((1 + config.CRASH_GROWTH_PER_TICK) ** ticks, 2)


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
            "protected": bool(row["is_protected"]),
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
        "td_battles_played": user["td_battles_played"] or 0,
        "td_wins": user["td_wins"] or 0,
        "td_best_wave": user["td_best_wave"] or 0,
        "td_shots_fired": user["td_shots_fired"] or 0,
        "td_hits": user["td_hits"] or 0,
        "td_headshots": user["td_headshots"] or 0,
        "td_damage_dealt": user["td_damage_dealt"] or 0,
        "td_accuracy_pct": (
            round((user["td_hits"] or 0) / user["td_shots_fired"] * 100)
            if user["td_shots_fired"] else 0
        ),
    })


async def handle_shop_cases(request: web.Request) -> web.Response:
    """Отдаёт список кейсов (тот же static/cases.json, которым пользуется
    handlers/cases.py) для витрины. Само открытие — см. handle_cases_open
    (поддерживает и одиночное открытие, и пачкой по config.CASE_BULK_OPEN_COUNT)."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    cases = _load_cases()
    # Наружу отдаём укороченную витрину: без весов дропа (это внутренняя
    # механика шанса). "sample_items" — 4 примера для карточки кейса,
    # "items" — полный список предметов (тоже без весов) для визуальной
    # прокрутки при открытии кейса в Web App.
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
            "items": [
                {"name": i["name"], "rarity": i["rarity"], "price": i["price"]}
                for i in case["items"]
            ],
        })

    return web.json_response({"cases": preview, "bulk_open_count": config.CASE_BULK_OPEN_COUNT})


async def handle_cases_open(request: web.Request) -> web.Response:
    """Открывает кейс прямо из Web App — та же самая логика (шансы,
    списание баланса, XP, скидка по уровню), что использует бот в
    handlers/cases.py (roll_item / case_discounted_price импортированы
    оттуда напрямую, чтобы гарантированно не разойтись с ботом).

    Поддерживает открытие пачкой: необязательное поле "count" (1..
    config.CASE_BULK_OPEN_COUNT) открывает несколько кейсов одним запросом.
    Цена и проверка уровня считаются один раз в начале по стартовому уровню
    игрока — так нельзя случайно получить более выгодную скидку на часть
    кейсов из-за уровня, поднятого за опыт от предыдущих кейсов в той же
    пачке."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    db.get_or_create_user(user_id, user_data.get("username") or f"id{user_id}")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    case_id = payload.get("case_id")
    case = cases_handlers.CASES_BY_ID.get(case_id)
    if not case:
        return web.json_response({"error": "case_not_found"}, status=404)

    try:
        count = int(payload.get("count", 1))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_count"}, status=400)
    if count < 1 or count > config.CASE_BULK_OPEN_COUNT:
        return web.json_response({"error": "invalid_count"}, status=400)

    level_info = db.get_level_info(user_id)
    level = level_info["level"]
    min_level = cases_handlers.case_min_level(case)
    if level < min_level:
        return web.json_response({"error": "level_too_low", "min_level": min_level}, status=400)

    price = cases_handlers.case_discounted_price(case, level)
    total_price = price * count
    balance = db.get_balance(user_id)
    if balance < total_price:
        return web.json_response({"error": "insufficient_balance", "price": total_price}, status=400)

    db.add_balance(user_id, -total_price)

    items = []
    for _ in range(count):
        item = cases_handlers.roll_item(case)
        item_id = db.add_item(user_id, item["name"], item["rarity"], item["price"])
        db.increment_stat(user_id, "cases_opened")
        items.append({"id": item_id, "name": item["name"], "rarity": item["rarity"], "price": item["price"]})

    xp_gained = cases_handlers.case_xp_reward(case) * count
    xp_result = db.add_xp(user_id, xp_gained)

    return web.json_response({
        "items": items,
        "item": items[0],  # для обратной совместимости со старым однокейсовым откликом
        "count": count,
        "price_paid": total_price,
        "xp_gained": xp_gained,
        "balance": db.get_balance(user_id),
        "level_info": xp_result,
    })


async def handle_shop_items(request: web.Request) -> web.Response:
    """Отдаёт список эксклюзивных предметов магазина (config.SHOP_ITEMS) —
    их нельзя получить из кейсов, только купить напрямую за монеты."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    level = db.get_level_info(user_id)["level"]

    items = [
        {**item, "unlocked": level >= item["min_level"]}
        for item in config.SHOP_ITEMS
    ]
    return web.json_response({"items": items, "level": level})


async def handle_shop_buy(request: web.Request) -> web.Response:
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    db.get_or_create_user(user_id, user_data.get("username") or f"id{user_id}")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    item_id = payload.get("item_id")
    shop_item = next((i for i in config.SHOP_ITEMS if i["id"] == item_id), None)
    if not shop_item:
        return web.json_response({"error": "item_not_found"}, status=404)

    level = db.get_level_info(user_id)["level"]
    if level < shop_item["min_level"]:
        return web.json_response({"error": "level_too_low", "min_level": shop_item["min_level"]}, status=400)

    balance = db.get_balance(user_id)
    if balance < shop_item["price"]:
        return web.json_response({"error": "insufficient_balance"}, status=400)

    db.add_balance(user_id, -shop_item["price"])
    new_item_id = db.add_item(user_id, shop_item["name"], shop_item["rarity"], shop_item["price"])

    return web.json_response({
        "item": {"id": new_item_id, "name": shop_item["name"], "rarity": shop_item["rarity"], "price": shop_item["price"]},
        "balance": db.get_balance(user_id),
    })


async def handle_inventory_protect(request: web.Request) -> web.Response:
    """Ставит/снимает защиту с предмета инвентаря (см. database.set_item_protected).
    Защищённый предмет нельзя продать/апгрейднуть ни из Web App, ни из бота —
    проверка идёт на уровне БД/хендлеров с обеих сторон."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_item_id"}, status=400)

    protected = bool(payload.get("protected"))
    ok = db.set_item_protected(item_id, user_id, protected)
    if not ok:
        return web.json_response({"error": "item_not_found"}, status=404)

    return web.json_response({"item_id": item_id, "protected": protected})


async def handle_inventory_sell(request: web.Request) -> web.Response:
    """Продаёт один предмет инвентаря из Web App (та же формула SELL_PERCENT,
    что и в боте) — защищённые предметы продать нельзя."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    try:
        item_id = int(payload.get("item_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_item_id"}, status=400)

    item = db.get_item_by_id(item_id, user_id)
    if not item:
        return web.json_response({"error": "item_not_found"}, status=404)
    if item["is_protected"]:
        return web.json_response({"error": "item_protected"}, status=400)

    price = max(1, int(item["item_price"] * config.SELL_PERCENT))
    db.remove_item(item_id, user_id)
    db.add_balance(user_id, price)

    return web.json_response({"sold_for": price, "balance": db.get_balance(user_id)})


async def handle_inventory_sell_all(request: web.Request) -> web.Response:
    """Продаёт весь незащищённый инвентарь разом — та же логика (и та же
    db.remove_all_items), что использует кнопка "🗑 Продать всё" в боте
    (handlers/inventory.py:sell_all)."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    items = db.get_inventory(user_id)
    sellable = [i for i in items if not i["is_protected"]]

    if not sellable:
        return web.json_response({"error": "nothing_to_sell"}, status=400)

    total_price = sum(max(1, int(i["item_price"] * config.SELL_PERCENT)) for i in sellable)
    count = len(sellable)

    db.remove_all_items(user_id)  # защищённые предметы не удаляются
    db.add_balance(user_id, total_price)

    return web.json_response({
        "sold_count": count,
        "sold_for": total_price,
        "protected_remaining": len(items) - count,
        "balance": db.get_balance(user_id),
    })


async def handle_top(request: web.Request) -> web.Response:
    """Три рейтинга (монеты / уровень / самый дорогой дроп), читают ту же
    общую базу, что и раздел «🏆 Топ» в боте (handlers/top.py)."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    top_type = request.query.get("type", "coins")

    if top_type == "level":
        rows = db.get_top_users_by_level(10)
        entries = [
            {"name": r["username"] or f"id{r['user_id']}", "value": r["level"], "sub": f"{r['xp']} XP"}
            for r in rows
        ]
    elif top_type == "item":
        rows = db.get_top_drops(10)
        entries = [
            {"name": r["username"] or f"id{r['user_id']}", "value": r["item_price"], "sub": r["item_name"]}
            for r in rows
        ]
    else:
        top_type = "coins"
        rows = db.get_top_users(10)
        entries = [
            {"name": r["username"] or f"id{r['user_id']}", "value": r["balance"], "sub": None}
            for r in rows
        ]

    return web.json_response({"type": top_type, "entries": entries})


async def handle_battle_config(request: web.Request) -> web.Response:
    """Отдаёт баланс боя (волны/HP/урон/характеристики башни), чтобы
    фронтенд не хардкодил цифры отдельно от сервера — правится в одном
    месте (BATTLE_CONFIG выше)."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    return web.json_response(BATTLE_CONFIG)


async def handle_battle_finish(request: web.Request) -> web.Response:
    """Принимает итог одного боя от клиента (сторона, до какой волны
    дошёл, победа/поражение) и начисляет награду СЕРВЕРНЫМ расчётом —
    клиент не может просто прислать произвольное число монет.

    Бой целиком идёт на Canvas в браузере (это однопользовательская
    PvE-волна, серверу незачем гонять тот же цикл ещё раз), поэтому
    полноценной защиты от накрутки (повторной отправки, скорости игры
    и т.д.) здесь пока нет — это нормально для тестового Этапа 3.
    Прежде чем давать реальную ценность наградам, на одном из следующих
    этапов стоит добавить идемпотентность (id боя) и серверную валидацию
    таймингов."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    side = payload.get("side")
    won = bool(payload.get("won"))
    try:
        wave_reached = int(payload.get("wave_reached", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_wave_reached"}, status=400)

    if side not in ("t", "ct"):
        return web.json_response({"error": "invalid_side"}, status=400)

    # Клампим на случай, если с фронтенда прилетит бессмысленное число.
    wave_reached = max(0, min(wave_reached, BATTLE_CONFIG["wave_count"]))

    # Статистика стрельбы (Этап 4) — на награду не влияет, только на
    # профиль/точность, но всё равно клампим на разумные значения, чтобы
    # в БД не улетело что-то абсурдное из испорченного запроса.
    def _clamp_int(value, lo, hi):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return lo
        return max(lo, min(n, hi))

    shots_fired = _clamp_int(payload.get("shots_fired", 0), 0, 100_000)
    hits = _clamp_int(payload.get("hits", 0), 0, shots_fired)
    headshots = _clamp_int(payload.get("headshots", 0), 0, hits)
    damage_dealt = _clamp_int(payload.get("damage_dealt", 0), 0, 10_000_000)

    reward_coins = wave_reached * BATTLE_CONFIG["reward_per_wave"]
    reward_xp = wave_reached * BATTLE_CONFIG["xp_per_wave"]
    if won:
        reward_coins += BATTLE_CONFIG["reward_win_bonus"]
        reward_xp += BATTLE_CONFIG["xp_win_bonus"]

    db.get_or_create_user(user_id, user_data.get("username") or f"id{user_id}")
    result = db.record_td_battle_result(
        user_id, wave_reached, won, reward_coins, reward_xp,
        shots_fired=shots_fired, hits=hits, headshots=headshots, damage_dealt=damage_dealt,
    )

    accuracy_pct = round(hits / shots_fired * 100) if shots_fired else 0

    return web.json_response({
        "reward_coins": reward_coins,
        "reward_xp": reward_xp,
        "best_wave": result["best_wave"],
        "balance": db.get_balance(user_id),
        "level_info": result["xp_result"],
        "accuracy_pct": accuracy_pct,
        "headshots": headshots,
    })


async def handle_crash_state(request: web.Request) -> web.Response:
    """Отдаёт текущее состояние раунда (если есть) плюс общий конфиг
    игры (доступные ставки, скорость роста) — фронтенд дергает этот
    эндпоинт при открытии вкладки Crash и периодическим поллингом во
    время раунда, чтобы узнать, не наступил ли крах."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    base = {
        "bet_options": config.CRASH_BET_OPTIONS,
        "tick_seconds": config.CRASH_TICK_SECONDS,
        "growth_per_tick": config.CRASH_GROWTH_PER_TICK,
    }

    state = _CRASH_GAMES.get(user_id)
    if not state:
        return web.json_response({**base, "active": False})

    elapsed = time.time() - state["start_time"]
    current_mult = _crash_multiplier_at(elapsed)

    if current_mult >= state["crash_point"]:
        # Игрок не успел забрать — раунд лопнул сам, без явного клика.
        crash_point = state["crash_point"]
        bet = state["bet"]
        _CRASH_GAMES.pop(user_id, None)
        return web.json_response({
            **base, "active": True, "crashed": True,
            "crash_point": crash_point, "bet": bet,
        })

    return web.json_response({
        **base, "active": True, "crashed": False,
        "bet": state["bet"],
        "multiplier": current_mult,
        "start_time": int(state["start_time"] * 1000),
        "potential_payout": int(state["bet"] * current_mult),
    })


async def handle_crash_start(request: web.Request) -> web.Response:
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    db.get_or_create_user(user_id, user_data.get("username") or f"id{user_id}")

    existing = _CRASH_GAMES.get(user_id)
    if existing:
        elapsed = time.time() - existing["start_time"]
        if _crash_multiplier_at(elapsed) < existing["crash_point"]:
            return web.json_response({"error": "already_active"}, status=409)
        _CRASH_GAMES.pop(user_id, None)

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    try:
        bet = int(payload.get("bet"))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_bet"}, status=400)

    if bet not in config.CRASH_BET_OPTIONS:
        return web.json_response({"error": "invalid_bet"}, status=400)

    balance = db.get_balance(user_id)
    if balance < bet:
        return web.json_response({"error": "insufficient_balance"}, status=400)

    db.add_balance(user_id, -bet)

    start_time = time.time()
    _CRASH_GAMES[user_id] = {
        "bet": bet,
        "crash_point": generate_crash_point(),
        "start_time": start_time,
    }

    return web.json_response({
        "bet": bet,
        "start_time": int(start_time * 1000),
        "tick_seconds": config.CRASH_TICK_SECONDS,
        "growth_per_tick": config.CRASH_GROWTH_PER_TICK,
        "balance": db.get_balance(user_id),
    })


async def handle_crash_cashout(request: web.Request) -> web.Response:
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    state = _CRASH_GAMES.get(user_id)
    if not state:
        return web.json_response({"error": "no_active_game"}, status=409)

    elapsed = time.time() - state["start_time"]
    current_mult = _crash_multiplier_at(elapsed)

    if current_mult >= state["crash_point"]:
        crash_point = state["crash_point"]
        bet = state["bet"]
        _CRASH_GAMES.pop(user_id, None)
        return web.json_response({
            "crashed": True, "crash_point": crash_point, "bet": bet,
            "balance": db.get_balance(user_id),
        })

    bet = state["bet"]
    payout = int(bet * current_mult)
    _CRASH_GAMES.pop(user_id, None)

    db.add_balance(user_id, payout)
    xp_result = db.add_xp(user_id, config.XP_CRASH_WIN)

    return web.json_response({
        "crashed": False,
        "multiplier": current_mult,
        "bet": bet,
        "payout": payout,
        "balance": db.get_balance(user_id),
        "level_info": xp_result,
    })


# ---------- Слоты (Web App, Этап 3) ----------
#
# Честная механика на сервере: каждый из 3 барабанов крутится независимо,
# символ выбирается взвешенным случайным выбором из config.SLOTS_SYMBOLS
# (чем выше "weight", тем чаще символ выпадает). Клиент получает готовый
# результат и только красиво его анимирует — подделать исход нельзя,
# т.к. деньги списываются/начисляются на сервере ДО ответа клиенту.
_SLOTS_WEIGHTS = [s["weight"] for s in config.SLOTS_SYMBOLS]


def _spin_reels() -> list[dict]:
    return random.choices(config.SLOTS_SYMBOLS, weights=_SLOTS_WEIGHTS, k=3)


def _slots_payout(reels: list[dict], bet: int) -> tuple[int, str]:
    """Возвращает (выигрыш_в_монетах, тип_комбинации)."""
    ids = [r["id"] for r in reels]
    if ids[0] == ids[1] == ids[2]:
        symbol = reels[0]
        return int(bet * symbol["payout"]), "triple"

    # Ищем совпадение ровно двух из трёх барабанов (пара) — считаем по
    # символу, который встретился дважды.
    for sym in reels:
        if ids.count(sym["id"]) == 2:
            return int(bet * sym["pair_payout"]), "pair"

    return 0, "none"


async def handle_upgrade_config(request: web.Request) -> web.Response:
    """Отдаёт настройки Апгрейда (см. handlers/upgrade.py) — сколько предметов
    можно объединить за раз и таблицу множитель/шанс из config.UPGRADE_MULTIPLIERS,
    отсортированную по возрастанию."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    multipliers = [
        {"multiplier": mult, "chance": chance}
        for mult, chance in sorted(config.UPGRADE_MULTIPLIERS.items())
    ]

    return web.json_response({
        "max_items": config.MAX_UPGRADE_ITEMS,
        "multipliers": multipliers,
    })


async def handle_upgrade_run(request: web.Request) -> web.Response:
    """Запускает апгрейд из Web App — та же логика, что в боте
    (handlers/upgrade.py:do_upgrade): выбранные предметы суммируются по цене,
    с шансом из config.UPGRADE_MULTIPLIERS цена умножается на множитель и все
    предметы объединяются в один новый, иначе всё сгорает в джекпот."""
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    db.get_or_create_user(user_id, user_data.get("username") or f"id{user_id}")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    raw_ids = payload.get("item_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return web.json_response({"error": "invalid_item_ids"}, status=400)

    try:
        item_ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_item_ids"}, status=400)

    if len(set(item_ids)) != len(item_ids):
        return web.json_response({"error": "duplicate_item_ids"}, status=400)
    if len(item_ids) > config.MAX_UPGRADE_ITEMS:
        return web.json_response({"error": "too_many_items"}, status=400)

    try:
        mult = float(payload.get("multiplier"))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_multiplier"}, status=400)
    if mult == int(mult):
        mult = int(mult)

    chance = config.UPGRADE_MULTIPLIERS.get(mult)
    if chance is None:
        return web.json_response({"error": "invalid_multiplier"}, status=400)

    items = [db.get_item_by_id(item_id, user_id) for item_id in item_ids]
    if any(i is None for i in items):
        return web.json_response({"error": "item_not_found"}, status=404)
    if any(i["is_protected"] for i in items):
        return web.json_response({"error": "item_protected"}, status=400)

    total_price = sum(i["item_price"] for i in items)
    success = random.random() < chance

    if success:
        new_price = int(total_price * mult)
        if len(items) == 1:
            new_name = items[0]["item_name"]
            new_rarity = items[0]["item_rarity"]
        else:
            best = max(items, key=lambda i: i["item_price"])
            new_name = f"Улучшенный набор ({len(items)} предм.)"
            new_rarity = best["item_rarity"]

        for i in items:
            db.remove_item(i["id"], user_id)
        new_item_id = db.add_item(user_id, new_name, new_rarity, new_price)
        db.increment_stat(user_id, "upgrades_success")
        xp_result = db.add_xp(user_id, config.XP_UPGRADE_SUCCESS)

        return web.json_response({
            "success": True,
            "total_price": total_price,
            "multiplier": mult,
            "chance": chance,
            "new_item": {
                "id": new_item_id,
                "name": new_name,
                "rarity": new_rarity,
                "price": new_price,
            },
            "level_info": xp_result,
        })
    else:
        for i in items:
            db.remove_item(i["id"], user_id)
        db.increment_stat(user_id, "upgrades_failed")
        db.add_to_jackpot(total_price)  # сгоревшие монеты уходят в джекпот

        return web.json_response({
            "success": False,
            "total_price": total_price,
            "multiplier": mult,
            "chance": chance,
        })


async def handle_slots_spin(request: web.Request) -> web.Response:
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    user_id = user_data["id"]
    db.get_or_create_user(user_id, user_data.get("username") or f"id{user_id}")

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid_json"}, status=400)

    try:
        bet = int(payload.get("bet"))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_bet"}, status=400)

    if bet not in config.SLOTS_BET_OPTIONS:
        return web.json_response({"error": "invalid_bet"}, status=400)

    balance = db.get_balance(user_id)
    if balance < bet:
        return web.json_response({"error": "insufficient_balance"}, status=400)

    db.add_balance(user_id, -bet)

    reels = _spin_reels()
    payout, combo = _slots_payout(reels, bet)

    xp_amount = config.XP_SLOTS_SPIN + (config.XP_SLOTS_WIN if payout > 0 else 0)
    if payout > 0:
        db.add_balance(user_id, payout)
    xp_result = db.add_xp(user_id, xp_amount)

    return web.json_response({
        "reels": [{"id": r["id"], "icon": r["icon"]} for r in reels],
        "combo": combo,
        "bet": bet,
        "payout": payout,
        "net": payout - bet,
        "balance": db.get_balance(user_id),
        "level_info": xp_result,
    })


async def handle_slots_config(request: web.Request) -> web.Response:
    user_data = _authenticate(request)
    if user_data is None:
        return _unauthorized()

    return web.json_response({
        "bet_options": config.SLOTS_BET_OPTIONS,
        "symbols": [
            {
                "id": s["id"],
                "icon": s["icon"],
                "name": s["name"],
                "payout": s["payout"],
                "pair_payout": s["pair_payout"],
            }
            for s in config.SLOTS_SYMBOLS
        ],
    })


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
    app.router.add_post("/api/cases/open", handle_cases_open)
    app.router.add_get("/api/shop/items", handle_shop_items)
    app.router.add_post("/api/shop/buy", handle_shop_buy)
    app.router.add_post("/api/inventory/protect", handle_inventory_protect)
    app.router.add_post("/api/inventory/sell", handle_inventory_sell)
    app.router.add_post("/api/inventory/sell_all", handle_inventory_sell_all)
    app.router.add_get("/api/top", handle_top)
    app.router.add_get("/api/slots/config", handle_slots_config)
    app.router.add_post("/api/slots/spin", handle_slots_spin)
    app.router.add_get("/api/upgrade/config", handle_upgrade_config)
    app.router.add_post("/api/upgrade/run", handle_upgrade_run)
    app.router.add_get("/api/battle/config", handle_battle_config)
    app.router.add_post("/api/battle/finish", handle_battle_finish)
    app.router.add_get("/api/crash/state", handle_crash_state)
    app.router.add_post("/api/crash/start", handle_crash_start)
    app.router.add_post("/api/crash/cashout", handle_crash_cashout)
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
