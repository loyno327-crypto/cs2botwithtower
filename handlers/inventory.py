"""
Логика инвентаря: показать список предметов пользователя (с постраничной
навигацией — по config.ITEMS_PER_PAGE предметов на странице), продать
любой отдельный предмет обратно за монеты, либо продать сразу весь
инвентарь одной кнопкой.

Продажа отдаёт SELL_PERCENT (см. config.py) от цены предмета —
это специально меньше 100%, чтобы не было выгодно бесконечно
крутить кейсы и сразу продавать выпавшее без потерь.
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import config

router = Router()

RARITY_EMOJI = {
    "Милитари": "🔵",
    "Запрещённое": "🟪",
    "Засекреченное": "🌸",
    "Тайное": "🔴",
    "Редкий спецпредмет": "🟡",
}


def sell_price(item_price: int) -> int:
    """Сколько монет вернётся за предмет при продаже."""
    return max(1, int(item_price * config.SELL_PERCENT))


def _page_slice(items, page: int):
    per_page = config.ITEMS_PER_PAGE
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    return items[start:start + per_page], page, total_pages


def inventory_text(items, page_items, page: int, total_pages: int) -> str:
    total_value = sum(item["item_price"] for item in items)

    lines = ["📦 <b>Твой инвентарь:</b>\n"]
    for item in page_items:
        emoji = RARITY_EMOJI.get(item["item_rarity"], "◽")
        lines.append(
            f"{emoji} {item['item_name']} ({item['item_rarity']}) — {item['item_price']} монет"
        )

    lines.append(f"\n💰 Общая стоимость инвентаря: <b>{total_value}</b> монет")
    lines.append(f"Продажа возвращает {int(config.SELL_PERCENT * 100)}% от стоимости предмета.")
    if total_pages > 1:
        lines.append(f"\nСтраница {page + 1} из {total_pages}")
    return "\n".join(lines)


def inventory_keyboard(items, page_items, page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=f"💸 Продать {item['item_name']} — {sell_price(item['item_price'])} монет",
                callback_data=f"sell_item:{item['id']}:{page}"
            )
        ]
        for item in page_items
    ]

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"inv_page:{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="inv_noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"inv_page:{page + 1}"))
        buttons.append(nav_row)

    if items:
        buttons.append([InlineKeyboardButton(text="🗑 Продать всё", callback_data="sell_all")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_inventory(user_id: int, page: int = 0):
    items = db.get_inventory(user_id)
    page_items, page, total_pages = _page_slice(items, page)
    text = inventory_text(items, page_items, page, total_pages)
    keyboard = inventory_keyboard(items, page_items, page, total_pages)
    return items, text, keyboard


@router.message(F.text == "📦 Инвентарь")
async def show_inventory(message: Message):
    user_id = message.from_user.id
    items, text, keyboard = await _render_inventory(user_id, 0)

    if not items:
        await message.answer("📦 Твой инвентарь пуст. Открой кейс, чтобы получить первые предметы!")
        return

    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "inv_noop")
async def inv_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("inv_page:"))
async def change_page(callback: CallbackQuery):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])

    items, text, keyboard = await _render_inventory(user_id, page)
    if not items:
        await callback.message.edit_text(
            "📦 Твой инвентарь пуст. Открой кейс, чтобы получить первые предметы!"
        )
        await callback.answer()
        return

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("sell_item:"))
async def sell_item(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    item_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    item = db.get_item_by_id(item_id, user_id)
    if not item:
        await callback.answer("Предмет не найден — возможно, уже продан.", show_alert=True)
        return

    price = sell_price(item["item_price"])
    db.remove_item(item_id, user_id)
    db.add_balance(user_id, price)

    await callback.answer(f"✅ Продано за {price} монет!")

    items, text, keyboard = await _render_inventory(user_id, page)
    if not items:
        await callback.message.edit_text(
            "📦 Твой инвентарь пуст. Открой кейс, чтобы получить первые предметы!"
        )
        return

    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data == "sell_all")
async def sell_all(callback: CallbackQuery):
    user_id = callback.from_user.id
    items = db.get_inventory(user_id)

    if not items:
        await callback.answer("Инвентарь уже пуст.", show_alert=True)
        return

    total_price = sum(sell_price(item["item_price"]) for item in items)
    count = len(items)

    db.remove_all_items(user_id)
    db.add_balance(user_id, total_price)

    await callback.message.edit_text(
        f"🗑 Продано {count} шт. предметов на общую сумму <b>{total_price}</b> монет.\n\n"
        f"📦 Твой инвентарь пуст. Открой кейс, чтобы получить первые предметы!"
    )
    await callback.answer(f"✅ Продано всё за {total_price} монет!")
