from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

import config
import database as db

router = Router()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = []

    # "Играть" — первой строкой, одна кнопка по центру. Показываем только
    # когда задан WEBAPP_URL (см. config.py) — так на этапе разработки,
    # пока адрес ещё не настроен, бот не ломается попыткой открыть пустую
    # ссылку.
    if config.WEBAPP_URL:
        keyboard.append([
            KeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=config.WEBAPP_URL))
        ])

    keyboard += [
        [KeyboardButton(text="⚔️ Дуэль"), KeyboardButton(text="🎰 Джекпот"), KeyboardButton(text="🎉 Бонус")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="💼 Работа")],
        [KeyboardButton(text="📦 Инвентарь"), KeyboardButton(text="🎁 Кейсы")],
        [KeyboardButton(text="🛠 Апгрейд"), KeyboardButton(text="🚀 Краш")],
        [KeyboardButton(text="🏆 Топ"), KeyboardButton(text="👤 Профиль")],
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
