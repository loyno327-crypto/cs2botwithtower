import asyncio
import logging
import random
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
from database import init_db
import database as db
from handlers import start, work, cases, inventory, upgrade, pvp, top, stats, jackpot, admin, crash
from webserver import start_webserver


async def bonus_notifier_task(bot: Bot):
    """Раз в BONUS_NOTIFY_CHECK_SECONDS проверяет, у кого истёк кулдаун
    ежедневного (теперь — ежечасного) бонуса, и присылает уведомление,
    что бонус готов. Уведомление шлётся только один раз на цикл кулдауна
    (флаг bonus_notified), чтобы не спамить."""
    cooldown = timedelta(hours=config.DAILY_BONUS_COOLDOWN_HOURS)
    while True:
        try:
            candidates = db.get_users_awaiting_bonus()
            now = datetime.now()
            for row in candidates:
                last_bonus = row["last_bonus_time"]
                if not last_bonus:
                    continue
                elapsed = now - datetime.fromisoformat(last_bonus)
                if elapsed >= cooldown:
                    user_id = row["user_id"]
                    try:
                        await bot.send_message(
                            user_id,
                            "🎉 Ежедневный бонус готов! Нажми «🎉 Бонус» в меню, чтобы забрать его."
                        )
                    except Exception:
                        pass
                    db.set_bonus_notified(user_id, 1)
        except Exception:
            logging.exception("Ошибка в bonus_notifier_task")

        await asyncio.sleep(config.BONUS_NOTIFY_CHECK_SECONDS)


async def jackpot_draw_task(bot: Bot):
    """Розыгрыш джекпота ПО РАСПИСАНИЮ — дважды в сутки, в моменты времени
    из config.JACKPOT_DRAW_TIMES (например, 12:00 и 00:00).

    В момент розыгрыша:
      - если в копилке накопилось МЕНЬШЕ config.JACKPOT_POT_PAYOUT монет —
        розыгрыш просто пропускается (копилка продолжает копиться дальше);
      - если накопилось достаточно — из копилки забирается ровно
        JACKPOT_POT_PAYOUT монет, они делятся поровну между
        JACKPOT_WINNERS_COUNT случайно выбранными игроками, а остаток
        копилки переносится на следующий розыгрыш.

    Раз в минуту (JACKPOT_CHECK_SECONDS) задача проверяет текущее время —
    как только оно совпало с одним из времён в расписании, розыгрыш
    выполняется один раз (чтобы не сработать повторно в течение той же
    минуты, каждый слот "дата+время" запоминается в drawn_slots)."""
    drawn_slots: set[tuple[str, str]] = set()

    while True:
        try:
            now = datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M")

            if time_str in config.JACKPOT_DRAW_TIMES:
                slot = (date_str, time_str)
                if slot not in drawn_slots:
                    drawn_slots.add(slot)
                    # чистим старые метки, чтобы множество не росло бесконечно
                    drawn_slots = {s for s in drawn_slots if s[0] == date_str}

                    pot = db.get_jackpot()
                    if pot and pot["amount"] >= config.JACKPOT_POT_PAYOUT:
                        all_user_ids = db.get_all_user_ids()

                        if len(all_user_ids) >= config.JACKPOT_WINNERS_COUNT:
                            winners = random.sample(all_user_ids, config.JACKPOT_WINNERS_COUNT)

                            for winner_id in winners:
                                db.add_balance(winner_id, config.JACKPOT_WINNER_AMOUNT)
                                db.increment_stat(winner_id, "jackpot_wins")
                            db.draw_jackpot_multi(winners, config.JACKPOT_WINNER_AMOUNT)

                            names = []
                            for winner_id in winners:
                                user = db.get_or_create_user(winner_id, None)
                                name = user["username"] if user["username"] else f"id{winner_id}"
                                names.append(f"@{name}" if user["username"] else name)
                            names_str = ", ".join(names)

                            announcement = (
                                f"🎰 <b>Джекпот разыгран!</b>\n\n"
                                f"Победители: {names_str}\n"
                                f"Каждый из них получил <b>{config.JACKPOT_WINNER_AMOUNT}</b> монет!\n\n"
                                f"Удачи в следующий раз! 🍀"
                            )
                            for user_id in all_user_ids:
                                try:
                                    await bot.send_message(user_id, announcement)
                                except Exception:
                                    pass
                                await asyncio.sleep(0.05)
                    # Если монет не хватило — молча пропускаем розыгрыш,
                    # без уведомлений, как и требовалось.
        except Exception:
            logging.exception("Ошибка в jackpot_draw_task")

        await asyncio.sleep(config.JACKPOT_CHECK_SECONDS)


async def main():
    logging.basicConfig(level=logging.INFO)

    if not config.BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Укажи переменную окружения BOT_TOKEN "
            "в настройках проекта на bothost.ru (Environment variables)."
        )

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    init_db()

    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(work.router)
    dp.include_router(cases.router)
    dp.include_router(inventory.router)
    dp.include_router(upgrade.router)
    dp.include_router(pvp.router)
    dp.include_router(crash.router)
    dp.include_router(top.router)
    dp.include_router(jackpot.router)
    dp.include_router(stats.router)

    asyncio.create_task(bonus_notifier_task(bot))
    asyncio.create_task(jackpot_draw_task(bot))
    asyncio.create_task(start_webserver())

    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот запущен. Нажми Ctrl+C, чтобы остановить.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
