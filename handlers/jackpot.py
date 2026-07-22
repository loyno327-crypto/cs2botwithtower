"""
Джекпот — розыгрыш "проигранных" монет.
В копилку падает комиссия дома с дуэлей и стоимость сгоревших при
апгрейде предметов. Дважды в сутки, по расписанию (см. фоновую задачу
jackpot_draw_task в bot.py), бот случайно выбирает config.JACKPOT_WINNERS_COUNT
игроков среди всех зарегистрированных и делит между ними ровно
config.JACKPOT_POT_PAYOUT монет из копилки. Если к моменту розыгрыша
нужная сумма ещё не накопилась — розыгрыш в этот раз пропускается.
"""

from aiogram import Router, F
from aiogram.types import Message

import database as db
import config

router = Router()


@router.message(F.text == "🎰 Джекпот")
async def show_jackpot(message: Message):
    db.get_or_create_user(
        message.from_user.id,
        message.from_user.username or message.from_user.full_name
    )

    pot = db.get_jackpot()
    amount = pot["amount"] if pot else 0
    remaining = max(config.JACKPOT_POT_PAYOUT - amount, 0)
    draw_times = " и ".join(config.JACKPOT_DRAW_TIMES)

    lines = [
        "🎰 <b>Джекпот</b>\n",
        f"Накоплено: <b>{amount}</b> монет",
        f"Копилка пополняется комиссией с дуэлей и стоимостью сгоревших "
        f"при апгрейде предметов.",
        f"Розыгрыш проходит <b>дважды в сутки</b> — в {draw_times}. Если к этому "
        f"моменту в копилке накопилось хотя бы {config.JACKPOT_POT_PAYOUT} монет, "
        f"бот случайно выбирает {config.JACKPOT_WINNERS_COUNT} игроков и делит между "
        f"ними {config.JACKPOT_POT_PAYOUT} монет — по {config.JACKPOT_WINNER_AMOUNT} "
        f"монет каждому. Если нужная сумма ещё не накопилась — розыгрыш в этот раз "
        f"пропускается, и копилка продолжает копиться дальше. "
        f"Участвовать отдельно не нужно, розыгрыш проходит сам.",
    ]
    if remaining > 0:
        lines.append(f"\nДо суммы следующего розыгрыша не хватает: <b>{remaining}</b> монет.")
    else:
        lines.append(f"\n✅ Суммы уже достаточно — розыгрыш состоится в ближайшее время ({draw_times}).")

    if pot and pot["last_winner_ids"]:
        winner_ids = pot["last_winner_ids"].split(",")
        names = ", ".join(f"id{wid}" for wid in winner_ids)
        lines.append(
            f"\n🏆 Прошлый розыгрыш: {names} "
            f"(по +{pot['last_winner_amount']} монет каждому)"
        )

    await message.answer("\n".join(lines))
