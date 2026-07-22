"""
Функция "Работа": пользователь нажимает кнопку, получает вопрос с вариантами
ответа. Правильный ответ — начисляется случайная награда. Есть небольшой
кулдаун, чтобы нельзя было спамить кнопку.
"""

import json
import os
import random
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import config
from handlers import cases

router = Router()

def _find_data_file(filename: str) -> str:
    """Ищет json-файл с данными (questions.json / cases.json) рядом с ботом.

    ВАЖНО: на bothost.ru папка /app/data — это примонтированный постоянный
    диск, который в момент запуска контейнера полностью перекрывает то, что
    было в этой папке внутри репозитория (файлы из data/ "пропадают" при
    старте). Поэтому статичные json-файлы с данными должны лежать в static/,
    а data/ используется только для database.db (см. config.DB_PATH).
    Порядок поиска: static -> data (на случай других хостингов без такого
    ограничения) -> assets, чтобы работать в любой конфигурации репозитория.
    """
    base = os.path.join(os.path.dirname(__file__), "..")
    for folder in ("static", "assets", "data"):
        candidate = os.path.join(base, folder, filename)
        if os.path.exists(candidate):
            return candidate
    # Если не нашли — вернём путь в static/, чтобы дальнейшая ошибка была понятной
    return os.path.join(base, "static", filename)


QUESTIONS_PATH = _find_data_file("questions.json")

with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)

# Храним "активный" вопрос каждого пользователя в памяти (пока бот работает).
# Ключ — user_id, значение — правильный индекс ответа и размер награды.
active_questions: dict[int, dict] = {}


@router.message(F.text == "💼 Работа")
async def start_work(message: Message):
    user_id = message.from_user.id
    db.get_or_create_user(
        user_id,
        message.from_user.username or message.from_user.full_name
    )

    last_work = db.get_last_work_time(user_id)
    if last_work:
        elapsed = (datetime.now() - datetime.fromisoformat(last_work)).total_seconds()
        if elapsed < config.WORK_COOLDOWN_SECONDS:
            wait = int(config.WORK_COOLDOWN_SECONDS - elapsed)
            await message.answer(f"⏳ Отдохни немного! Попробуй снова через {wait} сек.")
            return

    question_data = random.choice(QUESTIONS)
    reward = random.randint(config.WORK_REWARD_MIN, config.WORK_REWARD_MAX)

    active_questions[user_id] = {
        "correct": question_data["correct"],
        "reward": reward
    }

    buttons = [
        [InlineKeyboardButton(text=option, callback_data=f"work_answer:{i}")]
        for i, option in enumerate(question_data["options"])
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        f"🧠 <b>Вопрос:</b>\n{question_data['question']}",
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("work_answer:"))
async def process_answer(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = active_questions.get(user_id)

    if not data:
        await callback.answer("Вопрос уже неактивен, нажми «Работа» ещё раз.", show_alert=True)
        return

    chosen_index = int(callback.data.split(":")[1])
    db.set_last_work_time(user_id, datetime.now().isoformat())

    if chosen_index == data["correct"]:
        db.add_balance(user_id, data["reward"], reason="work_correct")
        db.increment_stat(user_id, "work_correct")
        result = db.add_xp(user_id, config.XP_WORK)
        db.log_event(user_id, "work_answer", details={"result": "correct", "reward": data["reward"]})

        text = f"✅ Правильно! Ты заработал <b>{data['reward']}</b> монет."
        text += cases.level_up_notice(result)
        await callback.message.edit_text(text)
    else:
        db.increment_stat(user_id, "work_wrong")
        db.log_event(user_id, "work_answer", details={"result": "wrong"})
        await callback.message.edit_text("❌ Неправильно. В этот раз без награды. Попробуй снова через некоторое время!")

    del active_questions[user_id]
    await callback.answer()
