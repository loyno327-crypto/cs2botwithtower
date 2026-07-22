"""
Профиль игрока: баланс, сколько всего заработано/потрачено монет,
инвентарь, "Работа", кейсы, дуэли, апгрейды.
"""

from aiogram import Router, F
from aiogram.types import Message

import database as db

router = Router()


def _percent(part: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{round(part / total * 100)}%"


@router.message(F.text == "👤 Профиль")
async def show_profile(message: Message):
    user_id = message.from_user.id
    db.get_or_create_user(
        user_id, message.from_user.username or message.from_user.full_name
    )

    stats = db.get_user_full_stats(user_id)
    user = stats["user"]

    work_total = user["work_correct"] + user["work_wrong"]
    duels_total = user["duels_played"]
    upg_total = user["upgrades_success"] + user["upgrades_failed"]

    best_item_line = (
        f"{stats['best_item_name']} ({stats['best_item_price']} монет)"
        if stats["best_item_name"] else "—"
    )

    total_earned = user["total_earned"] or 0
    total_spent = user["total_spent"] or 0

    level = user["level"] or 1
    xp = user["xp"] or 0
    xp_needed = db.xp_needed_for_level(level)
    xp_left = max(xp_needed - xp, 0)

    text = (
        f"👤 <b>Твой профиль</b>\n\n"
        f"⭐ Уровень: <b>{level}</b> ({xp}/{xp_needed} XP)\n"
        f"⏳ До {level + 1} уровня: <b>{xp_left}</b> XP\n"
        f"💰 Баланс: <b>{user['balance']}</b> монет\n"
        f"📈 Всего получено: <b>{total_earned}</b> монет\n"
        f"📉 Всего потрачено/проиграно: <b>{total_spent}</b> монет\n\n"
        f"📦 <b>Инвентарь</b>\n"
        f"• Предметов: {stats['items_count']}\n"
        f"• Общая стоимость: {stats['items_total_value']} монет\n"
        f"• Лучший предмет: {best_item_line}\n\n"
        f"💼 <b>Работа</b>\n"
        f"• Верно / неверно: {user['work_correct']} / {user['work_wrong']} "
        f"({_percent(user['work_correct'], work_total)})\n\n"
        f"🎁 <b>Кейсы</b>\n"
        f"• Открыто: {user['cases_opened']}\n\n"
        f"⚔️ <b>Дуэли</b>\n"
        f"• Сыграно: {duels_total}, побед: {user['duels_won']} "
        f"({_percent(user['duels_won'], duels_total)})\n\n"
        f"🛠 <b>Апгрейды</b>\n"
        f"• Удачных / сгорело: {user['upgrades_success']} / {user['upgrades_failed']} "
        f"({_percent(user['upgrades_success'], upg_total)})\n\n"
        f"🎰 <b>Джекпот</b>\n"
        f"• Выигрышей: {user['jackpot_wins'] or 0}"
    )

    await message.answer(text)
