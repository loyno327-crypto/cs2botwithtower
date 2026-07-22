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
    protected_count = sum(1 for item in items if item["is_protected"])

    lines = ["📦 <b>Твой инвентарь:</b>\n"]
    for item in page_items:
        emoji = RARITY_EMOJI.get(item["item_rarity"], "◽")
        lock = "🔒 " if item["is_protected"] else ""
        lines.append(
            f"{lock}{emoji} {item['item_name']} ({item['item_rarity']}) — {item['item_price']} монет"
        )

    lines.append(f"\n💰 Общая стоимость инвентаря: <b>{total_value}</b> монет")
    lines.append(f"Продажа возвращает {int(config.SELL_PERCENT * 100)}% от стоимости предмета.")
    if protected_count:
        lines.append(f"🔒 Защищено предметов: {protected_count} (не продаются и не участвуют в апгрейде).")
    if total_pages > 1:
        lines.append(f"\nСтраница {page + 1} из {total_pages}")
    return "\n".join(lines)


def inventory_keyboard(items, page_items, page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons = []
    for item in page_items:
        protected = bool(item["is_protected"])
        lock_text = "🔓 Снять защиту" if protected else "🔒 Защитить"
        buttons.append([InlineKeyboardButton(
            text=f"{lock_text}: {item['item_name']}",
            callback_data=f"toggle_protect:{item['id']}:{page}"
        )])
        if not protected:
            buttons.append([InlineKeyboardButton(
                text=f"💸 Продать {item['item_name']} — {sell_price(item['item_price'])} монет",
                callback_data=f"sell_item:{item['id']}:{page}"
            )])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"inv_page:{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="inv_noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"inv_page:{page + 1}"))
        buttons.append(nav_row)

    if any(not item["is_protected"] for item in items):
        buttons.append([InlineKeyboardButton(text="🗑 Продать всё (кроме защищённых)", callback_data="sell_all")])

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


@router.callback_query(F.data.startswith("toggle_protect:"))
async def toggle_protect(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    item_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    item = db.get_item_by_id(item_id, user_id)
    if not item:
        await callback.answer("Предмет не найден.", show_alert=True)
        return

    new_state = not bool(item["is_protected"])
    db.set_item_protected(item_id, user_id, new_state)
    await callback.answer("🔒 Предмет защищён." if new_state else "🔓 Защита снята.")

    items, text, keyboard = await _render_inventory(user_id, page)
    await callback.message.edit_text(text, reply_markup=keyboard)


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

    if item["is_protected"]:
        await callback.answer("🔒 Предмет защищён — сначала сними защиту.", show_alert=True)
        return

    price = sell_price(item["item_price"])
    db.remove_item(item_id, user_id)
    db.add_balance(user_id, price, reason="item_sell")
    db.log_event(user_id, "item_sell", details={
        "item_name": item["item_name"], "item_rarity": item["item_rarity"],
        "original_price": item["item_price"], "sold_for": price,
    })

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
    sellable = [i for i in items if not i["is_protected"]]

    if not sellable:
        await callback.answer("Нет предметов для продажи (всё защищено или инвентарь пуст).", show_alert=True)
        return

    total_price = sum(sell_price(item["item_price"]) for item in sellable)
    count = len(sellable)

    db.remove_all_items(user_id)  # защищённые предметы не удаляются
    db.add_balance(user_id, total_price, reason="item_sell_all")
    db.log_event(user_id, "item_sell_all", details={"count": count, "total_price": total_price})

    remaining = len(items) - count
    text = f"🗑 Продано {count} шт. предметов на общую сумму <b>{total_price}</b> монет.\n\n"
    if remaining:
        text += f"🔒 {remaining} защищённых предметов оставлены в инвентаре."
    else:
        text += "📦 Твой инвентарь пуст. Открой кейс, чтобы получить первые предметы!"

    await callback.message.edit_text(text)
    await callback.answer(f"✅ Продано всё за {total_price} монет!")
