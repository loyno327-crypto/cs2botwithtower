"""
PvP-дуэли на монеты по поиску соперника: игрок выбирает ставку, ЗАТЕМ сразу
выбирает ход камень/ножницы/бумага — и только после этого начинается поиск
соперника с такой же ставкой.

Такой порядок (сначала ход, потом поиск) важен: раз ход уже выбран заранее,
как только находится соперник — матч разрешается МГНОВЕННО и синхронно,
прямо внутри одного обработчика. Не нужно ждать, пока второй игрок тоже
что-то нажмёт где-то в другом месте — а именно с этим было связано
зависание/баг в старой версии (там оба игрока должны были успеть выбрать
ход уже ПОСЛЕ того, как нашли друг друга, и это состояние хранилось в
оперативной памяти процесса, из-за чего дуэль могла "зависнуть").

Весь ход дуэли хранится в самой таблице duels (колонка choice), поэтому
никакого хрупкого состояния в памяти между сообщениями пользователей нет.

Комиссия дома с каждой дуэли уходит в общий джекпот (handlers/jackpot.py).
"""

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import config
from handlers import cases

router = Router()

CHOICES = {
    "rock": "🪨 Камень",
    "scissors": "✂️ Ножницы",
    "paper": "📄 Бумага",
}

# Что бьёт что: ключ побеждает значение
BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


def bet_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"{bet} монет", callback_data=f"duel_bet:{bet}")]
        for bet in config.DUEL_BET_OPTIONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def rps_pick_keyboard(bet: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"rps_pick:{bet}:{choice}")]
        for choice, label in CHOICES.items()
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="duel_pick_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cancel_keyboard(duel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить поиск", callback_data=f"duel_cancel:{duel_id}")]
    ])


def display_name(user_id: int, username: str) -> str:
    return f"@{username}" if username else f"id{user_id}"


@router.message(F.text == "⚔️ Дуэль")
async def start_duel_menu(message: Message):
    user_id = message.from_user.id
    existing = db.get_waiting_duel_by_user(user_id)
    if existing:
        await message.answer(
            f"⏳ Ты уже ищешь соперника на ставку {existing['bet']} монет.",
            reply_markup=cancel_keyboard(existing["id"])
        )
        return

    await message.answer(
        "⚔️ Выбери ставку:",
        reply_markup=bet_keyboard()
    )


@router.callback_query(F.data.startswith("duel_bet:"))
async def choose_bet(callback: CallbackQuery):
    user_id = callback.from_user.id
    bet = int(callback.data.split(":")[1])

    if db.get_waiting_duel_by_user(user_id):
        await callback.answer("Ты уже в очереди на дуэль.", show_alert=True)
        return

    balance = db.get_balance(user_id)
    if balance < bet:
        await callback.answer(f"Недостаточно монет! Нужно {bet}, у тебя {balance}.", show_alert=True)
        return

    await callback.message.edit_text(
        f"⚔️ Ставка: <b>{bet}</b> монет.\n\n"
        f"Выбери камень, ножницы или бумагу — соперника начнём искать сразу "
        f"после этого:",
        reply_markup=rps_pick_keyboard(bet)
    )
    await callback.answer()


@router.callback_query(F.data == "duel_pick_cancel")
async def cancel_pick(callback: CallbackQuery):
    await callback.message.edit_text("Отменено. Ставка не списывалась 👍")
    await callback.answer()


@router.callback_query(F.data.startswith("rps_pick:"))
async def rps_pick(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.full_name
    _, bet_str, choice = callback.data.split(":")
    bet = int(bet_str)

    if db.get_waiting_duel_by_user(user_id):
        await callback.answer("Ты уже в очереди на дуэль.", show_alert=True)
        return

    balance = db.get_balance(user_id)
    if balance < bet:
        await callback.answer(f"Недостаточно монет! Нужно {bet}, у тебя {balance}.", show_alert=True)
        return

    opponent_duel = db.find_waiting_opponent(bet, user_id)

    if not opponent_duel:
        # Соперник не найден — замораживаем ставку и встаём в очередь
        # с уже выбранным ходом.
        db.add_balance(user_id, -bet)
        duel_id = db.create_duel(user_id, bet, username, choice)
        await callback.message.edit_text(
            f"⏳ Ищем соперника на ставку <b>{bet}</b> монет...\n"
            f"Твой ход: {CHOICES[choice]} (уже выбран, менять не нужно).\n"
            f"Как только кто-то поставит столько же — дуэль решится автоматически.",
            reply_markup=cancel_keyboard(duel_id)
        )
        await callback.answer()
        return

    # Соперник найден — оба хода уже известны, разрешаем матч мгновенно.
    db.add_balance(user_id, -bet)
    db.delete_duel(opponent_duel["id"])

    opponent_id = opponent_duel["user_id"]
    opponent_username = opponent_duel["username"]
    opponent_choice = opponent_duel["choice"]

    if choice == opponent_choice:
        # Ничья — раунд не засчитывается, обе ставки просто возвращаются.
        # Никакой повторной постановки в очередь не делаем специально: это
        # была бы такая же хрупкая "подвешенная" логика, из-за которой
        # раньше дуэли зависали. Проще и надёжнее — вернуть монеты и дать
        # игрокам самим нажать "Дуэль" ещё раз, если хотят реванш.
        db.add_balance(user_id, bet)
        db.add_balance(opponent_id, bet)

        tie_text = (
            f"⚖️ Ничья! Оба выбрали {CHOICES[choice]}.\n"
            f"Ставка возвращена. Нажми «⚔️ Дуэль», чтобы попробовать снова."
        )
        await callback.message.edit_text(tie_text)
        try:
            await bot.send_message(opponent_id, tie_text)
        except Exception:
            pass
        await callback.answer("Ничья! Ставка возвращена.")
        return

    if BEATS[choice] == opponent_choice:
        winner_id, winner_username, winner_choice = user_id, username, choice
        loser_id, loser_username, loser_choice = opponent_id, opponent_username, opponent_choice
    else:
        winner_id, winner_username, winner_choice = opponent_id, opponent_username, opponent_choice
        loser_id, loser_username, loser_choice = user_id, username, choice

    pot = bet * 2
    fee = pot * config.DUEL_HOUSE_FEE_PERCENT // 100
    payout = pot - fee

    db.add_balance(winner_id, payout)
    db.increment_stat(winner_id, "duels_played")
    db.increment_stat(winner_id, "duels_won")
    db.increment_stat(loser_id, "duels_played")
    db.add_to_jackpot(fee)  # комиссия дома уходит в общий джекпот
    xp_result = db.add_xp(winner_id, config.XP_DUEL_WIN)

    winner_text = (
        f"⚔️ Дуэль на {bet} монет завершена!\n\n"
        f"Ты играл с {display_name(loser_id, loser_username)}\n"
        f"Твой ход: {CHOICES[winner_choice]} vs {CHOICES[loser_choice]}\n\n"
        f"🏆 Ты победил и заработал <b>{payout}</b> монет "
        f"(комиссия дома: {fee}, ушла в джекпот)."
    )
    winner_text += cases.level_up_notice(xp_result)
    loser_text = (
        f"⚔️ Дуэль на {bet} монет завершена!\n\n"
        f"Ты играл с {display_name(winner_id, winner_username)}\n"
        f"Твой ход: {CHOICES[loser_choice]} vs {CHOICES[winner_choice]}\n\n"
        f"😔 В этот раз не повезло. Соперник забрал банк."
    )

    # Сообщение тому, кто только что нажал кнопку — редактируем прямо в диалоге.
    await callback.message.edit_text(winner_text if user_id == winner_id else loser_text)
    # Сообщение сопернику — он ждал в очереди, шлём новым сообщением.
    try:
        await bot.send_message(
            opponent_id,
            winner_text if opponent_id == winner_id else loser_text
        )
    except Exception:
        pass

    await callback.answer("Дуэль завершена!")


@router.callback_query(F.data.startswith("duel_cancel:"))
async def cancel_duel(callback: CallbackQuery):
    user_id = callback.from_user.id
    duel_id = int(callback.data.split(":")[1])
    duel = db.get_duel(duel_id)

    if not duel or duel["user_id"] != user_id or duel["status"] != "waiting":
        await callback.answer(
            "Эту дуэль уже нельзя отменить — возможно, соперник уже найден.",
            show_alert=True
        )
        return

    db.delete_duel(duel_id)
    db.add_balance(user_id, duel["bet"])
    await callback.message.edit_text("❌ Поиск соперника отменён, ставка возвращена.")
    await callback.answer()
