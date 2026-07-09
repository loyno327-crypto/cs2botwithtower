"""
Апгрейд предметов: можно выбрать от 1 до config.MAX_UPGRADE_ITEMS предметов
из инвентаря (список предметов разбит на страницы по config.ITEMS_PER_PAGE
штук — выбор предметов сохраняется при переключении между страницами), их
стоимость суммируется, и дальше — как раньше: с шансом из
config.UPGRADE_MULTIPLIERS суммарная цена увеличивается в это число раз,
и все выбранные предметы объединяются в один новый предмет с этой ценой.
Если апгрейд не удался — все выбранные предметы сгорают (удаляются).

Матожидание каждого множителя специально меньше 1 — это комиссия дома,
как в реальных апгрейдерах скинов. Стоимость сгоревших предметов уходит
в общий джекпот (см. handlers/jackpot.py).
"""

import random

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import config
from handlers import cases

router = Router()

# user_id -> {"selected": [id, ...], "page": int}
upgrade_state: dict[int, dict] = {}


def _fmt_mult(mult) -> str:
    # Красиво показываем "x1.2" и "x2" (без лишнего ".0")
    return f"x{mult:g}"


def _upgradeable_items(user_id: int):
    """Инвентарь без защищённых предметов — защищённые (is_protected = 1)
    нельзя использовать в апгрейде вообще, поэтому их не показываем
    в списке выбора."""
    return [i for i in db.get_inventory(user_id) if not i["is_protected"]]


def _page_slice(items, page: int):
    per_page = config.ITEMS_PER_PAGE
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    return items[start:start + per_page], page, total_pages


def pick_keyboard(items, selected: list[int], page: int) -> InlineKeyboardMarkup:
    page_items, page, total_pages = _page_slice(items, page)

    buttons = []
    for item in page_items:
        mark = "✅ " if item["id"] in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{item['item_name']} — {item['item_price']} монет",
            callback_data=f"upg_toggle:{item['id']}:{page}"
        )])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"upg_page:{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="upg_noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"upg_page:{page + 1}"))
        buttons.append(nav_row)

    if selected:
        total = sum(1 for i in items if i["id"] in selected)
        buttons.append([InlineKeyboardButton(
            text=f"➡️ Продолжить ({total} шт. выбрано)",
            callback_data="upg_continue"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="upg_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def multiplier_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{_fmt_mult(mult)}  (шанс {int(chance * 100)}%)",
            callback_data=f"upg_go:{mult}"
        )]
        for mult, chance in sorted(config.UPGRADE_MULTIPLIERS.items())
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="upg_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _pick_text(items, selected: list[int], page: int) -> str:
    _, page, total_pages = _page_slice(items, page)
    text = (
        f"🛠 Выбери от 1 до {config.MAX_UPGRADE_ITEMS} предметов, которые хочешь "
        f"объединить и попробовать улучшить (нажимай, чтобы отметить):"
    )
    if total_pages > 1:
        text += f"\n\nСтраница {page + 1} из {total_pages}"
    return text


@router.message(F.text == "🛠 Апгрейд")
async def start_upgrade(message: Message):
    user_id = message.from_user.id
    items = _upgradeable_items(user_id)
    if not items:
        await message.answer("📦 Инвентарь пуст — сначала открой кейс, апгрейдить нечего.")
        return

    upgrade_state[user_id] = {"selected": [], "page": 0}
    await message.answer(
        _pick_text(items, [], 0),
        reply_markup=pick_keyboard(items, [], 0)
    )


@router.callback_query(F.data == "upg_noop")
async def upg_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("upg_page:"))
async def change_page(callback: CallbackQuery):
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])

    state = upgrade_state.setdefault(user_id, {"selected": [], "page": 0})
    state["page"] = page

    items = _upgradeable_items(user_id)
    await callback.message.edit_text(
        _pick_text(items, state["selected"], page),
        reply_markup=pick_keyboard(items, state["selected"], page)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("upg_toggle:"))
async def toggle_item(callback: CallbackQuery):
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    item_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0

    item = db.get_item_by_id(item_id, user_id)
    if not item:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    if item["is_protected"]:
        await callback.answer("🔒 Предмет защищён — сначала сними защиту в Инвентаре.", show_alert=True)
        return

    state = upgrade_state.setdefault(user_id, {"selected": [], "page": page})
    selected = state["selected"]
    if item_id in selected:
        selected.remove(item_id)
    else:
        if len(selected) >= config.MAX_UPGRADE_ITEMS:
            await callback.answer(
                f"Нельзя выбрать больше {config.MAX_UPGRADE_ITEMS} предметов за раз.",
                show_alert=True
            )
            return
        selected.append(item_id)
    state["page"] = page

    items = _upgradeable_items(user_id)
    await callback.message.edit_text(
        _pick_text(items, selected, page),
        reply_markup=pick_keyboard(items, selected, page)
    )
    await callback.answer()


@router.callback_query(F.data == "upg_continue")
async def continue_upgrade(callback: CallbackQuery):
    user_id = callback.from_user.id
    selected = upgrade_state.get(user_id, {}).get("selected", [])
    if not selected:
        await callback.answer("Сначала выбери хотя бы один предмет.", show_alert=True)
        return

    items = [db.get_item_by_id(item_id, user_id) for item_id in selected]
    items = [i for i in items if i is not None and not i["is_protected"]]
    if not items:
        await callback.answer("Выбранные предметы больше недоступны.", show_alert=True)
        return

    total = sum(i["item_price"] for i in items)
    names = ", ".join(i["item_name"] for i in items)

    await callback.message.edit_text(
        f"🛠 Выбрано: <b>{names}</b>\n"
        f"Суммарная цена: <b>{total}</b> монет\n\n"
        f"Выбери множитель. Если апгрейд не удастся — все выбранные предметы сгорят.",
        reply_markup=multiplier_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "upg_cancel")
async def cancel_upgrade(callback: CallbackQuery):
    upgrade_state.pop(callback.from_user.id, None)
    await callback.message.edit_text("Отменено. Ничего не потеряно 👍")
    await callback.answer()


@router.callback_query(F.data.startswith("upg_go:"))
async def do_upgrade(callback: CallbackQuery):
    user_id = callback.from_user.id
    mult = float(callback.data.split(":")[1])
    if mult == int(mult):
        mult = int(mult)

    selected = upgrade_state.get(user_id, {}).get("selected", [])
    if not selected:
        await callback.answer("Список предметов пуст — начни заново.", show_alert=True)
        return

    items = [db.get_item_by_id(item_id, user_id) for item_id in selected]
    items = [i for i in items if i is not None and not i["is_protected"]]
    if not items:
        await callback.answer("Выбранные предметы больше недоступны.", show_alert=True)
        return

    chance = config.UPGRADE_MULTIPLIERS.get(mult)
    if chance is None:
        await callback.answer("Некорректный множитель.", show_alert=True)
        return

    total_price = sum(i["item_price"] for i in items)
    success = random.random() < chance

    if success:
        new_price = int(total_price * mult)
        if len(items) == 1:
            new_name = items[0]["item_name"]
            new_rarity = items[0]["item_rarity"]
        else:
            best = max(items, key=lambda i: i["item_price"])
            new_name = f"Улучшенный набор ({len(items)} предм.)"
            new_rarity = best["item_rarity"]

        for i in items:
            db.remove_item(i["id"], user_id)
        db.add_item(user_id, new_name, new_rarity, new_price)
        db.increment_stat(user_id, "upgrades_success")
        xp_result = db.add_xp(user_id, config.XP_UPGRADE_SUCCESS)

        text = (
            f"🎉 Апгрейд удался!\n<b>{new_name}</b> теперь стоит "
            f"<b>{new_price}</b> монет (было {total_price})."
        )
        text += cases.level_up_notice(xp_result)

        await callback.message.edit_text(text)
    else:
        for i in items:
            db.remove_item(i["id"], user_id)
        db.increment_stat(user_id, "upgrades_failed")
        db.add_to_jackpot(total_price)  # сгоревшие монеты уходят в джекпот

        await callback.message.edit_text(
            f"🔥 Не повезло, всё сгорело. −{total_price} монет в джекпот."
        )

    upgrade_state.pop(user_id, None)
    await callback.answer()
