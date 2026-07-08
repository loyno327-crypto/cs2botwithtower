from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message

import asyncio

import config
import database as db
from handlers.start import main_menu_keyboard

router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


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
    db.add_balance(target_id, amount)
    new_balance = db.get_balance(target_id)

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
