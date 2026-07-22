from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import asyncio
import html
import json
from datetime import datetime

import config
import database as db
from handlers.start import main_menu_keyboard

router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ---------- Просмотр журнала событий (диагностика / нечестная игра) ----------
# Человекочитаемые подписи для основных типов событий из database.event_log.
# Тип, которого нет в словаре, просто выводится как есть (с ❔), так что
# новые event_type из будущих фич не потеряются, даже если забыть добавить
# сюда подпись.
EVENT_LABELS = {
    "balance_change": "💰 Баланс",
    "work_answer": "🧠 Работа",
    "case_open": "📦 Кейс",
    "case_open_bulk": "📦 Кейсы (пачка)",
    "upgrade_result": "🛠 Апгрейд",
    "duel_queue_join": "⚔️ Дуэль: встал в очередь",
    "duel_result": "⚔️ Дуэль: итог",
    "duel_cancel": "⚔️ Дуэль: отмена поиска",
    "crash_start": "🚀 Краш: ставка",
    "crash_bust": "💥 Краш: сгорело",
    "crash_cashout": "💸 Краш: забрал",
    "item_sell": "🗑 Продажа предмета",
    "item_sell_all": "🗑 Продажа всего инвентаря",
    "jackpot_win": "🎰 Выигрыш джекпота",
    "td_battle_finish": "🎮 Бой Tower Defence",
    "admin_give": "🛡 Админ: начисление",
    "admin_broadcast": "🛡 Админ: рассылка",
    "admin_reset_drop_top": "🛡 Админ: очистка топа по дропу",
    "slots_spin": "🎰 Слоты",
}

# Какие поля из details показывать в компактной строке лога, в этом порядке.
_DETAIL_KEYS = (
    "reason", "result", "case_name", "item_name", "item_rarity",
    "price_paid", "sold_for", "original_price", "bet", "payout",
    "crash_point", "multiplier", "chance", "success", "target_id",
    "new_balance", "opponent_id", "winner_choice", "loser_choice",
    "text_preview", "sent", "failed", "count", "total_price", "total_spent",
    "wave_reached", "won", "reward_coins", "accuracy_pct", "flags",
)


def _fmt_time(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str).strftime("%d.%m %H:%M:%S")
    except (TypeError, ValueError):
        return iso_str or "?"


def _fmt_event_line(row) -> str:
    """Одна строка лога: время, тип события, сумма (если есть) и ключевые
    подробности из details — коротко, но так, чтобы по одной строке было
    понятно, что именно произошло."""
    label = EVENT_LABELS.get(row["event_type"], f"❔ {row['event_type']}")
    parts = [f"<b>{_fmt_time(row['created_at'])}</b> {label}"]

    if row["amount"] is not None:
        sign = "+" if row["amount"] >= 0 else ""
        amount_part = f"{sign}{row['amount']}"
        if row["balance_after"] is not None:
            amount_part += f" (→{row['balance_after']})"
        parts.append(amount_part)

    if row["details"]:
        try:
            details = json.loads(row["details"])
        except (TypeError, ValueError):
            details = {}
        bits = []
        for key in _DETAIL_KEYS:
            if key in details and details[key] not in (None, ""):
                bits.append(f"{key}={html.escape(str(details[key]))}")
        if bits:
            parts.append("[" + ", ".join(bits) + "]")

    line = " ".join(parts)
    if row["suspicious"]:
        line = "⚠️ " + line
    return line


def _events_reply(rows, title: str, with_user_id: bool = False) -> str:
    lines = [f"{title} (последние {len(rows)}):\n"]
    for r in rows:
        prefix = f"id{r['user_id']}: " if with_user_id else ""
        lines.append(prefix + _fmt_event_line(r))
    text = "\n".join(lines)
    # Telegram режет сообщения примерно на 4096 символов — подрезаем сами,
    # чтобы не словить ошибку отправки на большом количестве строк.
    if len(text) > 4000:
        text = text[:3990] + "\n… (обрезано, запроси меньше строк)"
    return text


def _parse_limit(parts, index, default=30, max_limit=100) -> int:
    if len(parts) > index:
        try:
            return max(1, min(int(parts[index]), max_limit))
        except ValueError:
            pass
    return default


@router.message(Command("admin_help"))
async def cmd_admin_help(message: Message):
    """Секретная команда: /admin_help
    Короткая шпаргалка по всем админским командам — сама по себе тоже
    скрытая (для не-админов бот молчит, как и для остальных команд ниже),
    чтобы список секретных команд нельзя было узнать, просто написав её."""
    if not _is_admin(message.from_user.id):
        return

    text = (
        "🛡 <b>Админские команды</b>\n\n"
        "<code>/find запрос</code>\n"
        "Найти id игрока прямо в боте — по нику (можно частично, без "
        "«@») или по точному id. Больше не нужны сторонние боты, чтобы "
        "узнать id перед /logs.\n\n"
        "<code>/players</code>\n"
        "Список всех игроков (новые сверху) с id, ником и балансом, "
        "листается кнопками — на случай, если /find ничего не нашёл.\n\n"
        "<code>/reset_drop_top</code>\n"
        "Полностью очистить топ по дропу (журнал drop_log), с подтверждением. "
        "Необратимо.\n\n"
        "<code>/give user_id amount</code>\n"
        "Начислить (или списать, если amount отрицательный) монеты игроку.\n\n"
        "<code>/broadcast текст</code>\n"
        "Разослать сообщение всем зарегистрированным игрокам.\n\n"
        "<code>/update_menu</code>\n"
        "Разослать всем короткое сообщение с актуальной клавиатурой — "
        "чтобы обновилось меню внизу экрана после изменений в коде.\n\n"
        "<code>/stats_global</code>\n"
        "Общая статистика бота: игроки, баланс, кейсы, дуэли, апгрейды и т.д.\n\n"
        "<code>/logs user_id [кол-во]</code>\n"
        "Журнал конкретного игрока: изменения баланса, кейсы, апгрейды, "
        "дуэли, краш, слоты, бои Tower Defence, покупки — всё подряд, "
        "новые события сверху.\n\n"
        "<code>/logs_suspicious [кол-во]</code>\n"
        "События, которые бот сам пометил как подозрительные (например, "
        "физически невозможная статистика боя) — по всем игрокам сразу.\n\n"
        "<code>/logs_recent [кол-во]</code>\n"
        "Общая лента последних событий по всем игрокам — если ID игрока "
        "заранее неизвестен.\n\n"
        "<code>/admin_help</code>\n"
        "Эта шпаргалка."
    )
    await message.answer(text)


@router.message(Command("give"))
async def cmd_give(message: Message):
    """Секретная команда: /give <user_id> <amount>
    Работает ТОЛЬКО для ID из config.ADMIN_IDS. Для всех остальных
    пользователей бот делает вид, что такой команды не существует —
    никакого ответа не отправляется, чтобы не палить её наличие.
    """
    if not _is_admin(message.from_user.id):
        return  # молча игнорируем — команда как будто не существует

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer(
            "Использование: <code>/give user_id amount</code>\n"
            "Пример: <code>/give 123456789 5000</code>"
        )
        return

    try:
        target_id = int(parts[1])
        amount = int(parts[2])
    except ValueError:
        await message.answer("user_id и amount должны быть целыми числами.")
        return

    # Начисляем (amount может быть и отрицательным, чтобы забрать монеты)
    db.get_or_create_user(target_id, None)
    db.add_balance(target_id, amount, reason=f"admin_give:{message.from_user.id}")
    new_balance = db.get_balance(target_id)
    db.log_event(
        message.from_user.id, "admin_give",
        details={"target_id": target_id, "amount": amount, "new_balance": new_balance}
    )

    await message.answer(
        f"✅ Готово.\n"
        f"Пользователю <code>{target_id}</code> начислено <b>{amount}</b> монет.\n"
        f"Текущий баланс: <b>{new_balance}</b>."
    )

    if target_id != message.from_user.id:
        try:
            await message.bot.send_message(
                target_id,
                f"💰 Тебе начислено <b>{amount}</b> монет администратором!"
            )
        except Exception:
            pass


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    """Секретная команда: /broadcast текст сообщения
    Отправляет текст всем зарегистрированным пользователям бота.
    Доступна только админам, как и /give."""
    if not _is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Использование: <code>/broadcast текст сообщения</code>\n"
            "Пример: <code>/broadcast Привет всем! Скоро новый ивент 🎉</code>"
        )
        return

    content = parts[1]
    user_ids = db.get_all_user_ids()
    status_msg = await message.answer(f"📢 Начинаю рассылку для {len(user_ids)} пользователей...")

    sent, failed = 0, 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, content)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # не спамим Telegram API слишком быстро

    db.log_event(message.from_user.id, "admin_broadcast", details={
        "text_preview": content[:200], "sent": sent, "failed": failed,
    })

    await status_msg.edit_text(
        f"📢 Рассылка завершена.\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Не доставлено (бот заблокирован и т.п.): {failed}"
    )


@router.message(Command("update_menu"))
async def cmd_update_menu(message: Message, bot: Bot):
    """Секретная команда: /update_menu
    Решает проблему "после обновления бота меню внизу не обновилось".

    Дело в том, что Telegram-клиент запоминает клавиатуру (reply-кнопки
    внизу экрана) с прошлого раза, когда бот её присылал, и НЕ обновляет
    её сам по себе, даже если в коде бота кнопки поменялись — новую
    клавиатуру клиент подхватит только когда бот в следующий раз пришлёт
    сообщение с reply_markup. Чистить историю чата для этого не нужно —
    это отдельное, необратимое действие, которое к тому же бот не может
    сделать за пользователя.

    Эта команда просто рассылает всем пользователям короткое сообщение
    с АКТУАЛЬНОЙ клавиатурой — после этого у всех она обновится сама,
    без необходимости просить всех вручную нажать /start."""
    if not _is_admin(message.from_user.id):
        return

    user_ids = db.get_all_user_ids()
    status_msg = await message.answer(f"🔄 Обновляю меню для {len(user_ids)} пользователей...")

    sent, failed = 0, 0
    for user_id in user_ids:
        try:
            await bot.send_message(
                user_id,
                "🔄 Меню бота обновлено — загляни, там могли появиться новые кнопки!",
                reply_markup=main_menu_keyboard()
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"🔄 Обновление меню завершено.\n"
        f"✅ Доставлено: {sent}\n"
        f"❌ Не доставлено (бот заблокирован и т.п.): {failed}"
    )


@router.message(Command("stats_global"))
async def cmd_global_stats(message: Message):
    """Секретная команда: общая статистика бота (только для админов)."""
    if not _is_admin(message.from_user.id):
        return

    s = db.get_global_stats()
    text = (
        f"📊 <b>Общая статистика бота</b>\n\n"
        f"👥 Игроков зарегистрировано: <b>{s['users_count']}</b>\n"
        f"💰 Суммарный баланс всех игроков: <b>{s['total_balance']}</b>\n"
        f"📈 Всего заработано за всё время: <b>{s['total_earned']}</b>\n"
        f"📉 Всего потрачено/проиграно: <b>{s['total_spent']}</b>\n\n"
        f"🎁 Кейсов открыто: <b>{s['cases_opened']}</b>\n"
        f"⚔️ Дуэлей сыграно: <b>{s['duels_played']}</b>\n"
        f"🛠 Апгрейдов удачных/сгоревших: <b>{s['upgrades_success']}</b> / "
        f"<b>{s['upgrades_failed']}</b>\n\n"
        f"📦 Предметов в инвентарях всех игроков: <b>{s['items_count']}</b>\n"
        f"💎 Их суммарная стоимость: <b>{s['items_total_value']}</b>\n\n"
        f"🎰 Сейчас в копилке джекпота: <b>{s['jackpot_amount']}</b> монет"
    )
    await message.answer(text)


def _users_list_text(rows, title: str) -> str:
    if not rows:
        return "Никого не нашлось."
    lines = [f"{title}\n"]
    for r in rows:
        name = f"@{r['username']}" if r["username"] else "(без ника)"
        lines.append(f"• <code>{r['user_id']}</code> — {name} — {r['balance']} монет")
    return "\n".join(lines)


def _players_keyboard(page: int, total: int, page_size: int):
    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_players:{page - 1}"))
    if (page + 1) * page_size < total:
        row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_players:{page + 1}"))
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


@router.message(Command("find"))
async def cmd_find(message: Message):
    """Секретная команда: /find <ник или user_id>
    Ищет игрока прямо в боте — по нику (можно частично, без "@") или по
    точному id — и показывает его id, ник и баланс. Снимает необходимость
    искать id игрока через сторонние боты перед тем, как посмотреть его
    /logs."""
    if not _is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Использование: <code>/find запрос</code>\n"
            "Примеры: <code>/find 123456789</code> или <code>/find ivan</code>\n\n"
            "Если не найдётся — попробуй <code>/players</code>, чтобы "
            "пролистать всех зарегистрированных игроков."
        )
        return

    rows = db.find_users(parts[1])
    await message.answer(_users_list_text(rows, f"🔎 <b>Результаты поиска «{html.escape(parts[1])}»</b>"))


@router.message(Command("players"))
async def cmd_players(message: Message):
    """Секретная команда: /players
    Постраничный список ВСЕХ зарегистрированных игроков (новые сверху) —
    id, ник, баланс. Пролистывается кнопками, если /find не нашёл нужного
    (например, игрок ещё не задавал себе ник в Telegram)."""
    if not _is_admin(message.from_user.id):
        return

    page_size = 20
    rows, total = db.get_users_page(offset=0, limit=page_size)
    text = _users_list_text(rows, f"👥 <b>Игроки</b> (всего: {total})")
    await message.answer(text, reply_markup=_players_keyboard(0, total, page_size))


@router.callback_query(F.data.startswith("admin_players:"))
async def paginate_players(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    page = int(callback.data.split(":")[1])
    page_size = 20
    rows, total = db.get_users_page(offset=page * page_size, limit=page_size)
    text = _users_list_text(rows, f"👥 <b>Игроки</b> (всего: {total})")
    await callback.message.edit_text(text, reply_markup=_players_keyboard(page, total, page_size))
    await callback.answer()


@router.message(Command("reset_drop_top"))
async def cmd_reset_drop_top(message: Message):
    """Секретная команда: /reset_drop_top
    Полностью очищает журнал дропов (drop_log), из которого строится топ
    "по дропу" — например, чтобы убрать оттуда старые записи от апгрейдов
    (попадали туда до фикса) или обнулить топ в начале нового периода.
    Необратимо, поэтому сначала спрашиваем подтверждение кнопкой."""
    if not _is_admin(message.from_user.id):
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚠️ Да, очистить топ по дропу", callback_data="admin_reset_drop_top:confirm"),
        InlineKeyboardButton(text="Отмена", callback_data="admin_reset_drop_top:cancel"),
    ]])
    await message.answer(
        "Это удалит ВСЮ историю дропов из кейсов (журнал, по которому "
        "строится топ «по дропу»). Действие необратимо. Продолжить?",
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("admin_reset_drop_top:"))
async def confirm_reset_drop_top(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer()
        return

    action = callback.data.split(":")[1]
    if action == "cancel":
        await callback.message.edit_text("Отменено, топ по дропу не тронут.")
        await callback.answer()
        return

    deleted = db.clear_drop_log()
    db.log_event(callback.from_user.id, "admin_reset_drop_top", details={"deleted": deleted})
    await callback.message.edit_text(f"✅ Топ по дропу очищен. Удалено записей: {deleted}.")
    await callback.answer()


@router.message(Command("logs"))
async def cmd_logs(message: Message):
    """Секретная команда: /logs <user_id> [кол-во]
    Показывает журнал конкретного игрока: не только изменения баланса, а
    вообще все значимые события (кейсы, апгрейды, дуэли, краш, слоты, бои
    Tower Defence, покупки и т.д.) — чтобы можно было разобрать спорную
    ситуацию, найти баг по цепочке событий или заметить нечестную игру."""
    if not _is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/logs user_id [кол-во]</code>\n"
            "Пример: <code>/logs 123456789 40</code> (по умолчанию 30, максимум 100)\n\n"
            "Смежные команды:\n"
            "<code>/logs_suspicious [кол-во]</code> — события, автоматически "
            "помеченные как подозрительные, по всем игрокам сразу\n"
            "<code>/logs_recent [кол-во]</code> — общая лента последних "
            "событий по всем игрокам"
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("user_id должен быть целым числом.")
        return

    limit = _parse_limit(parts, 2)
    rows = db.get_user_events(target_id, limit=limit)
    if not rows:
        await message.answer(f"Событий для игрока <code>{target_id}</code> пока нет.")
        return

    await message.answer(_events_reply(rows, f"📜 <b>Лог игрока {target_id}</b>"))


@router.message(Command("logs_suspicious"))
async def cmd_logs_suspicious(message: Message):
    """Секретная команда: /logs_suspicious [кол-во]
    События, которые бот САМ автоматически пометил как подозрительные —
    например, если из Web App пришла статистика боя, физически невозможная
    (попаданий больше выстрелов и т.п.). По всем игрокам сразу — удобно для
    регулярной проверки на нечестную игру без просмотра каждого игрока
    по отдельности."""
    if not _is_admin(message.from_user.id):
        return

    parts = message.text.split()
    limit = _parse_limit(parts, 1)
    rows = db.get_recent_events(limit=limit, suspicious_only=True)
    if not rows:
        await message.answer("⚠️ Подозрительных событий пока не найдено.")
        return

    await message.answer(_events_reply(rows, "⚠️ <b>Подозрительные события</b>", with_user_id=True))


@router.message(Command("logs_recent"))
async def cmd_logs_recent(message: Message):
    """Секретная команда: /logs_recent [кол-во]
    Общая лента последних событий по ВСЕМ игрокам сразу (в отличие от
    /logs — там журнал одного конкретного игрока) — удобно смотреть "живой
    пульс" бота или искать инцидент по примерному времени, если ID игрока
    заранее неизвестен."""
    if not _is_admin(message.from_user.id):
        return

    parts = message.text.split()
    limit = _parse_limit(parts, 1)
    rows = db.get_recent_events(limit=limit)
    if not rows:
        await message.answer("Событий пока нет.")
        return

    await message.answer(_events_reply(rows, "📜 <b>Общая лента событий</b>", with_user_id=True))
