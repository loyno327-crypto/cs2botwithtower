"""
Топ игроков по балансу и ежедневный бонус.
"""

import random
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import config

router = Router()

MEDALS = ["🥇", "🥈", "🥉"]

RARITY_EMOJI = {
    "Милитари": "🔵",
    "Запрещённое": "🟪",
    "Засекреченное": "🌸",
    "Тайное": "🔴",
    "Редкий спецпредмет": "🟡",
}


def _balance_top_text() -> str:
    top_users = db.get_top_users(10)
    if not top_users:
        return "Пока никто не заработал ни монеты 🙂"
    lines = ["🏆 <b>Топ игроков по балансу:</b>\n"]
    for i, user in enumerate(top_users):
        prefix = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        name = user["username"] or f"id{user['user_id']}"
        lines.append(f"{prefix} {name} — {user['balance']} монет")
    return "\n".join(lines)


def _level_top_text() -> str:
    top_users = db.get_top_users_by_level(10)
    if not top_users:
        return "Пока ни у кого нет опыта 🙂"
    lines = ["⭐ <b>Топ игроков по уровню:</b>\n"]
    for i, user in enumerate(top_users):
        prefix = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        name = user["username"] or f"id{user['user_id']}"
        xp_needed = db.xp_needed_for_level(user["level"])
        xp_left = max(xp_needed - user["xp"], 0)
        lines.append(f"{prefix} {name} — {user['level']} ур. (ещё {xp_left} XP до след.)")
    return "\n".join(lines)


def _drop_top_text() -> str:
    top_drops = db.get_top_drops(10)
    if not top_drops:
        return "Пока никто ничего не выбил из кейсов 🙂"
    lines = ["💎 <b>Топ по дропу (лучший предмет каждого игрока):</b>\n"]
    for i, drop in enumerate(top_drops):
        name = drop["username"] or f"id{drop['user_id']}"
        lines.append(f"{i + 1}. {name} {drop['item_name']} — {drop['item_price']}")
    return "\n".join(lines)


def _top_keyboard(active: str) -> InlineKeyboardMarkup:
    balance_label = "💰 По балансу ✅" if active == "balance" else "💰 По балансу"
    level_label = "⭐ По уровню ✅" if active == "level" else "⭐ По уровню"
    drop_label = "💎 По дропу ✅" if active == "drop" else "💎 По дропу"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=balance_label, callback_data="top_switch:balance"),
            InlineKeyboardButton(text=level_label, callback_data="top_switch:level"),
        ],
        [
            InlineKeyboardButton(text=drop_label, callback_data="top_switch:drop"),
        ]
    ])


@router.message(F.text == "🏆 Топ")
async def show_top(message: Message):
    await message.answer(_balance_top_text(), reply_markup=_top_keyboard("balance"))


@router.callback_query(F.data.startswith("top_switch:"))
async def switch_top(callback: CallbackQuery):
    mode = callback.data.split(":")[1]
    if mode == "level":
        text = _level_top_text()
    elif mode == "drop":
        text = _drop_top_text()
    else:
        mode = "balance"
        text = _balance_top_text()
    await callback.message.edit_text(text, reply_markup=_top_keyboard(mode))
    await callback.answer()


@router.message(F.text == "🎉 Бонус")
async def claim_daily_bonus(message: Message):
    user_id = message.from_user.id
    db.get_or_create_user(
        user_id, message.from_user.username or message.from_user.full_name
    )

    last_bonus = db.get_last_bonus_time(user_id)
    if last_bonus:
        elapsed = datetime.now() - datetime.fromisoformat(last_bonus)
        cooldown = timedelta(hours=config.DAILY_BONUS_COOLDOWN_HOURS)
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            hours = int(remaining.total_seconds() // 3600)
            minutes = int((remaining.total_seconds() % 3600) // 60)
            await message.answer(
                f"⏳ Бонус уже получен. Приходи через {hours} ч {minutes} мин."
            )
            return

    reward = random.randint(config.DAILY_BONUS_MIN, config.DAILY_BONUS_MAX)
    db.add_balance(user_id, reward, reason="daily_bonus")
    db.set_last_bonus_time(user_id, datetime.now().isoformat())
    db.set_bonus_notified(user_id, 0)  # чтобы фоновая задача снова уведомила, когда бонус будет готов
    await message.answer(
        f"🎉 Бонус: <b>{reward}</b> монет! Заходи через "
        f"{config.DAILY_BONUS_COOLDOWN_HOURS} ч. — пришлём напоминание, когда он будет готов."
    )
