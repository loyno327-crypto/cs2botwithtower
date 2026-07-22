"""
Краш-игра: игрок делает ставку, множитель начинает расти от x1.00 и
обновляется прямо в сообщении каждые config.CRASH_TICK_SECONDS. В любой
момент можно нажать "💸 Забрать" — тогда выигрыш = ставка * множитель на
момент нажатия. Если не успел забрать до случайной "точки краша" — ставка
сгорает полностью.

Точка краша генерируется по классической формуле краш-игр с "зашитой"
комиссией дома (config.CRASH_HOUSE_EDGE) — так множитель x2-x3 выпадает
регулярно, а высокие множители (x10+) редко, как в реальных краш-играх.

Состояние активной игры хранится в памяти (active_games), так как это
просто "жив ли ещё раунд для этого игрока прямо сейчас" — переживать
перезапуск бота ему не нужно.
"""

import asyncio
import random

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import config
from handlers import cases

router = Router()

# user_id -> {"bet": int, "multiplier": float, "crash_point": float, "done": bool,
#             "chat_id": int, "message_id": int}
active_games: dict[int, dict] = {}


def bet_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"{bet} монет", callback_data=f"crash_bet:{bet}")]
        for bet in config.CRASH_BET_OPTIONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cashout_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Забрать", callback_data="crash_cashout")]
    ])


def generate_crash_point() -> float:
    """Классическая формула генерации точки краша с комиссией дома.
    Небольшой шанс мгновенного краша на x1.00 — как в реальных краш-играх."""
    r = random.random()
    if r < 0.03:
        return 1.00
    point = config.CRASH_HOUSE_EDGE / (1 - r)
    return round(min(point, config.CRASH_MAX_MULTIPLIER), 2)


@router.message(F.text == "🚀 Краш")
async def start_crash_menu(message: Message):
    user_id = message.from_user.id
    db.get_or_create_user(
        user_id, message.from_user.username or message.from_user.full_name
    )

    if user_id in active_games and not active_games[user_id].get("done"):
        await message.answer("⏳ У тебя уже есть активная игра — сначала заверши её.")
        return

    await message.answer(
        "🚀 <b>Краш</b>\n\n"
        "Выбери ставку. Множитель начнёт расти с x1.00 — жми «💸 Забрать» "
        "в любой момент, чтобы получить ставку × текущий множитель. "
        "Не успеешь — ставка сгорит.",
        reply_markup=bet_keyboard()
    )


@router.callback_query(F.data.startswith("crash_bet:"))
async def choose_bet(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    bet = int(callback.data.split(":")[1])

    if user_id in active_games and not active_games[user_id].get("done"):
        await callback.answer("У тебя уже есть активная игра.", show_alert=True)
        return

    balance = db.get_balance(user_id)
    if balance < bet:
        await callback.answer(f"Недостаточно монет! Нужно {bet}, у тебя {balance}.", show_alert=True)
        return

    db.add_balance(user_id, -bet, reason="crash_bet")

    crash_point = generate_crash_point()
    state = {
        "bet": bet,
        "multiplier": 1.0,
        "crash_point": crash_point,
        "done": False,
        "chat_id": callback.message.chat.id,
        "message_id": callback.message.message_id,
    }
    active_games[user_id] = state
    # crash_point пишем в лог сразу (игроку он не показывается) — если
    # потом возникнет спор "нечестно сгорело слишком быстро", по логу видно
    # реальную сгенерированную точку краха, а не то, что запомнил игрок.
    db.log_event(user_id, "crash_start", details={"bet": bet, "crash_point": crash_point})

    await callback.message.edit_text(
        f"🚀 Ставка <b>{bet}</b> монет сделана!\n"
        f"Множитель: <b>x1.00</b>\n"
        f"Возможный выигрыш: {bet} монет",
        reply_markup=cashout_keyboard()
    )
    await callback.answer()

    asyncio.create_task(_run_crash_loop(bot, user_id, state))


async def _run_crash_loop(bot: Bot, user_id: int, state: dict):
    while True:
        await asyncio.sleep(config.CRASH_TICK_SECONDS)

        if state.get("done"):
            return

        state["multiplier"] = round(state["multiplier"] * (1 + config.CRASH_GROWTH_PER_TICK), 2)

        if state["multiplier"] >= state["crash_point"]:
            state["done"] = True
            db.log_event(user_id, "crash_bust", details={
                "bet": state["bet"], "crash_point": state["crash_point"],
            })
            try:
                await bot.edit_message_text(
                    chat_id=state["chat_id"],
                    message_id=state["message_id"],
                    text=(
                        f"💥 Крах на <b>x{state['crash_point']:.2f}</b>!\n"
                        f"Ты не успел забрать — ставка <b>{state['bet']}</b> монет сгорела."
                    )
                )
            except Exception:
                pass
            active_games.pop(user_id, None)
            return

        potential = int(state["bet"] * state["multiplier"])
        try:
            await bot.edit_message_text(
                chat_id=state["chat_id"],
                message_id=state["message_id"],
                text=(
                    f"🚀 Множитель растёт: <b>x{state['multiplier']:.2f}</b>\n"
                    f"Возможный выигрыш: {potential} монет"
                ),
                reply_markup=cashout_keyboard()
            )
        except Exception:
            pass


@router.callback_query(F.data == "crash_cashout")
async def cashout(callback: CallbackQuery):
    user_id = callback.from_user.id
    state = active_games.get(user_id)

    if not state or state.get("done"):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    state["done"] = True
    multiplier = state["multiplier"]
    bet = state["bet"]
    payout = int(bet * multiplier)

    db.add_balance(user_id, payout, reason="crash_cashout")
    result = db.add_xp(user_id, config.XP_CRASH_WIN)
    db.log_event(user_id, "crash_cashout", details={
        "bet": bet, "multiplier": multiplier, "payout": payout, "crash_point": state["crash_point"],
    })

    text = (
        f"💸 Забрал на <b>x{multiplier:.2f}</b>!\n"
        f"Выигрыш: <b>{payout}</b> монет (ставка была {bet})."
    )
    text += cases.level_up_notice(result)

    await callback.message.edit_text(text)
    active_games.pop(user_id, None)
    await callback.answer("Забрал выигрыш!")
