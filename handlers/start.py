from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

import database as db

router = Router()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    # Остальные разделы (Баланс, Инвентарь, Кейсы, Апгрейд, Краш, Топ,
    # Профиль, Играть) доступны через Web App (Menu Button в BotFather),
    # эта reply-клавиатура ими не дублируется. "Работа" — исключение: она
    # реализована именно здесь (handlers/work.py), поэтому у неё есть
    # кнопка в чате. "Дуэль" вынесена отдельной строкой ниже.
    keyboard = [
        [KeyboardButton(text="💼 Работа"), KeyboardButton(text="🎰 Джекпот"), KeyboardButton(text="🎉 Бонус")],
        [KeyboardButton(text="⚔️ Дуэль")],
    ]

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


@router.message(CommandStart())
async def cmd_start(message: Message):
    db.get_or_create_user(
        message.from_user.id,
        message.from_user.username or message.from_user.full_name
    )
    await message.answer(
        "👋 Добро пожаловать в CS2 Case Bot!\n\n"
        "Здесь можно зарабатывать баланс, открывать кейсы, собирать инвентарь скинов "
        "и играть с другими участниками.\n\n"
        "Выбери действие на клавиатуре ниже 👇",
        reply_markup=main_menu_keyboard()
    )


@router.message(F.text == "💰 Баланс")
async def show_balance(message: Message):
    db.get_or_create_user(
        message.from_user.id,
        message.from_user.username or message.from_user.full_name
    )
    balance = db.get_balance(message.from_user.id)
    await message.answer(f"💰 Твой баланс: <b>{balance}</b> монет")


# Обработчик кнопки "📦 Инвентарь" теперь живёт в handlers/inventory.py —
# там же продажа предметов.
