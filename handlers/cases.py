"""
Логика кейсов: показать список, показать содержимое конкретного кейса,
открыть кейс (списать баланс, выбрать случайный предмет с учётом весов,
положить предмет в инвентарь).
"""

import json
import os
import random

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import config

router = Router()

def _find_data_file(filename: str) -> str:
    """Ищет json-файл с данными рядом с ботом (см. подробный комментарий
    в handlers/work.py про папку /app/data на bothost.ru)."""
    base = os.path.join(os.path.dirname(__file__), "..")
    for folder in ("static", "assets", "data"):
        candidate = os.path.join(base, folder, filename)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(base, "static", filename)


CASES_PATH = _find_data_file("cases.json")

with open(CASES_PATH, "r", encoding="utf-8") as f:
    CASES = json.load(f)

CASES_BY_ID = {c["id"]: c for c in CASES}

RARITY_EMOJI = {
    "Милитари": "🔵",
    "Запрещённое": "🟪",
    "Засекреченное": "🌸",
    "Тайное": "🔴",
    "Редкий спецпредмет": "🟡",
}


def case_min_level(case: dict) -> int:
    """Минимальный уровень для открытия кейса. Если поле не задано в
    cases.json — кейс доступен всем, с 1 уровня (обратная совместимость)."""
    return case.get("min_level", 1)


def case_xp_reward(case: dict) -> int:
    """Сколько опыта даёт открытие кейса. Если поле не задано —
    используем старое общее значение XP_CASE_OPEN (обратная совместимость)."""
    return case.get("xp", config.XP_CASE_OPEN)


def case_discounted_price(case: dict, level: int) -> int:
    """Цена открытия кейса с учётом уровневой скидки игрока."""
    discount = config.level_discount_percent(level)
    price = case["price"] * (100 - discount) // 100
    return max(int(price), 1)


def cases_list_keyboard(level: int) -> InlineKeyboardMarkup:
    buttons = []
    for c in CASES:
        min_level = case_min_level(c)
        if level >= min_level:
            price = case_discounted_price(c, level)
            price_label = f"{price} монет" if price == c["price"] else f"{price} монет (было {c['price']})"
            text = f"{c['name']} — {price_label}"
        else:
            text = f"🔒 {c['name']} — открыт на {min_level} ур."
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"case_info:{c['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def level_up_notice(result: dict) -> str:
    """Общий текст про прогресс уровня: сколько опыта осталось до следующего
    уровня, плюс (если применимо) новый уровень + бонус + разблокированные
    кейсы. Используется во всех местах, где начисляется опыт (работа, дуэли,
    апгрейд, краш, кейсы), чтобы сообщение выглядело одинаково везде."""
    remaining = max(result["xp_needed"] - result["xp"], 0)
    text = f"\n\n⏳ До след. уровня ({result['new_level'] + 1}): <b>{remaining}</b> XP"

    if not result.get("leveled_up"):
        return text

    old_level = result["new_level"] - result["levels_gained"]
    unlocked = [c for c in CASES if old_level < case_min_level(c) <= result["new_level"]]
    text += (
        f"\n\n⭐ Новый уровень: <b>{result['new_level']}</b>! "
        f"Бонус: +{result['bonus_awarded']} монет."
    )
    for c in unlocked:
        text += f"\n🔓 Открыт новый кейс: {c['name']}!"
    discount = config.level_discount_percent(result["new_level"])
    prev_discount = config.level_discount_percent(old_level)
    if discount > prev_discount:
        text += f"\n💸 Скидка на все кейсы теперь {discount}%!"
    return text


def roll_item(case: dict) -> dict:
    items = case["items"]
    weights = [item["weight"] for item in items]
    return random.choices(items, weights=weights, k=1)[0]


@router.message(F.text == "🎁 Кейсы")
async def show_cases(message: Message):
    level_info = db.get_level_info(message.from_user.id)
    await message.answer(
        "🎁 <b>Доступные кейсы:</b>\nВыбери кейс, чтобы посмотреть содержимое и открыть его.\n"
        "🔒 — кейс откроется, когда прокачаешь уровень.",
        reply_markup=cases_list_keyboard(level_info["level"])
    )


@router.callback_query(F.data.startswith("case_info:"))
async def case_info(callback: CallbackQuery):
    case_id = callback.data.split(":")[1]
    case = CASES_BY_ID.get(case_id)
    if not case:
        await callback.answer("Кейс не найден.", show_alert=True)
        return

    level_info = db.get_level_info(callback.from_user.id)
    level = level_info["level"]
    min_level = case_min_level(case)
    price = case_discounted_price(case, level)
    discount = config.level_discount_percent(level)

    lines = [f"📦 <b>{case['name']}</b>"]
    if level < min_level:
        lines.append(f"🔒 Открывается с <b>{min_level}</b> уровня (у тебя {level}).")
    else:
        if discount > 0:
            lines.append(f"Цена открытия: <s>{case['price']}</s> <b>{price}</b> монет (скидка {discount}% за уровень)")
        else:
            lines.append(f"Цена открытия: <b>{price}</b> монет")
    lines.append(f"Опыт за открытие: +{case_xp_reward(case)} XP")
    lines.append("\nВозможные предметы:")
    for item in sorted(case["items"], key=lambda x: x["price"]):
        emoji = RARITY_EMOJI.get(item["rarity"], "◽")
        lines.append(f"{emoji} {item['name']} — ~{item['price']} монет")

    buttons = []
    if level >= min_level:
        buttons.append([InlineKeyboardButton(text=f"🔓 Открыть за {price} монет", callback_data=f"open_case:{case_id}")])
        buttons.append([InlineKeyboardButton(text=f"🔓×5 Открыть 5 шт. (~{price * 5} монет)", callback_data=f"open_case5:{case_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к кейсам", callback_data="back_to_cases")])

    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == "back_to_cases")
async def back_to_cases(callback: CallbackQuery):
    level_info = db.get_level_info(callback.from_user.id)
    await callback.message.edit_text(
        "🎁 <b>Доступные кейсы:</b>\nВыбери кейс, чтобы посмотреть содержимое и открыть его.\n"
        "🔒 — кейс откроется, когда прокачаешь уровень.",
        reply_markup=cases_list_keyboard(level_info["level"])
    )
    await callback.answer()


@router.callback_query(F.data.startswith("open_case:"))
async def open_case(callback: CallbackQuery):
    user_id = callback.from_user.id
    case_id = callback.data.split(":")[1]
    case = CASES_BY_ID.get(case_id)
    if not case:
        await callback.answer("Кейс не найден.", show_alert=True)
        return

    level_info = db.get_level_info(user_id)
    level = level_info["level"]
    min_level = case_min_level(case)
    if level < min_level:
        await callback.answer(
            f"Этот кейс открывается с {min_level} уровня. У тебя пока {level}.",
            show_alert=True
        )
        return

    price = case_discounted_price(case, level)
    balance = db.get_balance(user_id)
    if balance < price:
        await callback.answer(
            f"Недостаточно монет! Нужно {price}, у тебя {balance}.",
            show_alert=True
        )
        return

    db.add_balance(user_id, -price, reason="case_open")
    item = roll_item(case)
    db.add_item(user_id, item["name"], item["rarity"], item["price"])
    db.increment_stat(user_id, "cases_opened")
    result = db.add_xp(user_id, case_xp_reward(case))
    new_balance = db.get_balance(user_id)
    emoji = RARITY_EMOJI.get(item["rarity"], "◽")
    db.log_event(user_id, "case_open", details={
        "case_id": case_id, "case_name": case["name"], "price_paid": price,
        "item_name": item["name"], "item_rarity": item["rarity"], "item_price": item["price"],
    })

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔓 Открыть ещё за {price}", callback_data=f"open_case:{case_id}")],
        [InlineKeyboardButton(text=f"🔓×5 Открыть ещё 5 шт.", callback_data=f"open_case5:{case_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к кейсам", callback_data="back_to_cases")]
    ])

    text = (
        f"📦 Ты открыл <b>{case['name']}</b>!\n\n"
        f"Выпало: {emoji} <b>{item['name']}</b>\n"
        f"Редкость: {item['rarity']}\n"
        f"Примерная стоимость: {item['price']} монет\n\n"
        f"💰 Баланс: {new_balance} монет\n"
        f"✨ Опыт: +{case_xp_reward(case)} XP\n"
        f"Предмет добавлен в инвентарь 📦"
    )
    text += level_up_notice(result)

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer("Кейс открыт!")


OPEN_MULTI_COUNT = 5


@router.callback_query(F.data.startswith("open_case5:"))
async def open_case_multi(callback: CallbackQuery):
    """Открывает сразу несколько кейсов одного вида (OPEN_MULTI_COUNT штук)
    за один клик. Если по пути не хватает монет, открывает сколько получится
    и честно об этом сообщает — деньги списываются штучно перед каждым
    открытием, так что "зависнуть" на середине с потерей монет нельзя."""
    user_id = callback.from_user.id
    case_id = callback.data.split(":")[1]
    case = CASES_BY_ID.get(case_id)
    if not case:
        await callback.answer("Кейс не найден.", show_alert=True)
        return

    level_info = db.get_level_info(user_id)
    min_level = case_min_level(case)
    if level_info["level"] < min_level:
        await callback.answer(
            f"Этот кейс открывается с {min_level} уровня. У тебя пока {level_info['level']}.",
            show_alert=True
        )
        return

    opened_items = []
    total_spent = 0
    total_xp = 0
    level_before = level_info["level"]
    total_levels_gained = 0
    total_bonus_awarded = 0
    final_result = None

    for _ in range(OPEN_MULTI_COUNT):
        level = db.get_level_info(user_id)["level"]
        price = case_discounted_price(case, level)
        balance = db.get_balance(user_id)
        if balance < price:
            break

        db.add_balance(user_id, -price, reason="case_open_bulk")
        item = roll_item(case)
        db.add_item(user_id, item["name"], item["rarity"], item["price"])
        db.increment_stat(user_id, "cases_opened")
        xp_reward = case_xp_reward(case)
        result = db.add_xp(user_id, xp_reward)

        opened_items.append(item)
        total_spent += price
        total_xp += xp_reward
        total_levels_gained += result["levels_gained"]
        total_bonus_awarded += result["bonus_awarded"]
        final_result = result

    if not opened_items:
        price = case_discounted_price(case, db.get_level_info(user_id)["level"])
        await callback.answer(
            f"Недостаточно монет! Нужно хотя бы {price}, у тебя {db.get_balance(user_id)}.",
            show_alert=True
        )
        return

    new_balance = db.get_balance(user_id)
    db.log_event(user_id, "case_open_bulk", details={
        "case_id": case_id, "case_name": case["name"], "count": len(opened_items),
        "total_spent": total_spent,
        "items": [{"name": i["name"], "rarity": i["rarity"], "price": i["price"]} for i in opened_items],
    })
    lines = [f"📦 Открыто <b>{len(opened_items)}/{OPEN_MULTI_COUNT}</b> кейсов «{case['name']}»!\n"]
    for item in opened_items:
        emoji = RARITY_EMOJI.get(item["rarity"], "◽")
        lines.append(f"{emoji} <b>{item['name']}</b> — {item['rarity']} (~{item['price']} монет)")

    if len(opened_items) < OPEN_MULTI_COUNT:
        lines.append(f"\n⚠️ Дальше не хватило монет — остановились на {len(opened_items)}.")

    lines.append(f"\n💰 Потрачено: {total_spent} монет · Баланс: {new_balance} монет")
    lines.append(f"✨ Опыт: +{total_xp} XP")
    lines.append("Все предметы добавлены в инвентарь 📦")

    text = "\n".join(lines)
    if final_result:
        combined_result = dict(final_result)
        combined_result["leveled_up"] = total_levels_gained > 0
        combined_result["levels_gained"] = total_levels_gained
        combined_result["bonus_awarded"] = total_bonus_awarded
        text += level_up_notice(combined_result)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔓 Открыть ещё 1", callback_data=f"open_case:{case_id}")],
        [InlineKeyboardButton(text=f"🔓×5 Открыть ещё {OPEN_MULTI_COUNT}", callback_data=f"open_case5:{case_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к кейсам", callback_data="back_to_cases")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer(f"Открыто {len(opened_items)} кейсов!")
