"""
Модуль работы с базой данных.
Используем SQLite — это просто файл на компьютере, ничего дополнительно
устанавливать не нужно (модуль sqlite3 встроен в Python).
"""

import sqlite3
import shutil
import os
from datetime import datetime
import config


def _migrate_legacy_db_location():
    """Раньше database.db лежал прямо рядом с кодом бота. Теперь (см.
    config.DB_PATH) он хранится на постоянном диске (/app/data на
    bothost.ru), чтобы переживать передеплой. Если старый файл с данными
    ещё существует по старому пути, а по новому его нет — копируем его,
    чтобы не потерять баланс и инвентарь игроков. Копируем, а не переносим,
    чтобы старый файл остался как запасная копия."""
    legacy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")
    target_path = os.path.abspath(config.DB_PATH)

    target_dir = os.path.dirname(target_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    if legacy_path != target_path and os.path.exists(legacy_path) and not os.path.exists(target_path):
        shutil.copy2(legacy_path, target_path)


def get_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при старте бота."""
    _migrate_legacy_db_location()

    conn = get_connection()
    cur = conn.cursor()

    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            last_work_time TEXT
        )
    """)

    # Таблица инвентаря (пригодится на следующих шагах — кейсы, апгрейд и т.д.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            item_name TEXT,
            item_rarity TEXT,
            item_price INTEGER,
            obtained_at TEXT
        )
    """)

    # Журнал дропов — ОТДЕЛЬНАЯ таблица, куда пишется каждый выпавший
    # предмет и которая никогда не чистится и не изменяется (в отличие от
    # inventory, откуда предметы удаляются при продаже/апгрейде/сгорании).
    # Нужна для топа "по дропу за всё время" — чтобы проданный или
    # апгрейднутый предмет не пропадал из истории лучших выпадений.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drop_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            item_name TEXT,
            item_rarity TEXT,
            item_price INTEGER,
            obtained_at TEXT
        )
    """)

    # Таблица дуэлей (PvP). status: 'waiting' — ждёт соперника, после матча
    # запись сразу удаляется (см. handlers/pvp.py), так что 'waiting' —
    # единственный статус, который реально хранится сколько-то времени.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bet INTEGER,
            status TEXT DEFAULT 'waiting',
            created_at TEXT
        )
    """)

    # Копилка джекпота — розыгрыш "проигранных" монет (комиссия с дуэлей +
    # сгоревшие при апгрейде предметы). Всегда одна строка с id = 1.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS jackpot (
            id INTEGER PRIMARY KEY,
            amount INTEGER DEFAULT 0,
            last_draw_at TEXT,
            last_winner_id INTEGER,
            last_winner_amount INTEGER DEFAULT 0
        )
    """)

    conn.commit()

    cur.execute("SELECT id FROM jackpot WHERE id = 1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO jackpot (id, amount, last_draw_at, last_winner_id, last_winner_amount) "
            "VALUES (1, 0, ?, NULL, 0)",
            (datetime.now().isoformat(),)
        )
        conn.commit()

    # Миграция: часть колонок могла отсутствовать в базе, созданной раньше
    # (например, до появления бонуса или статистики). ALTER TABLE ADD COLUMN
    # в SQLite не поддерживает "IF NOT EXISTS", поэтому просто ловим ошибку,
    # если колонка уже есть.
    new_columns = [
        ("last_bonus_time", "TEXT"),
        ("work_correct", "INTEGER DEFAULT 0"),
        ("work_wrong", "INTEGER DEFAULT 0"),
        ("cases_opened", "INTEGER DEFAULT 0"),
        ("duels_played", "INTEGER DEFAULT 0"),
        ("duels_won", "INTEGER DEFAULT 0"),
        ("upgrades_success", "INTEGER DEFAULT 0"),
        ("upgrades_failed", "INTEGER DEFAULT 0"),
        ("total_earned", "INTEGER DEFAULT 0"),
        ("total_spent", "INTEGER DEFAULT 0"),
        ("bonus_notified", "INTEGER DEFAULT 0"),
        ("jackpot_wins", "INTEGER DEFAULT 0"),
        ("xp", "INTEGER DEFAULT 0"),
        ("level", "INTEGER DEFAULT 1"),
        # Статистика Tower Defence Web App (Этап 3) — отдельные колонки
        # с префиксом td_, чтобы не путать с текстовыми режимами бота.
        ("td_battles_played", "INTEGER DEFAULT 0"),
        ("td_wins", "INTEGER DEFAULT 0"),
        ("td_best_wave", "INTEGER DEFAULT 0"),
        # Этап 4 — боевая механика (точность/хедшоты/броня): накопительная
        # статистика стрельбы за всё время, нужна для расчёта общей точности
        # в профиле и для будущих ачивок ("100 хедшотов" и т.п.).
        ("td_shots_fired", "INTEGER DEFAULT 0"),
        ("td_hits", "INTEGER DEFAULT 0"),
        ("td_headshots", "INTEGER DEFAULT 0"),
        ("td_damage_dealt", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_type in new_columns:
        try:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Миграция таблицы duels: имя соперника (кто с кем играл) и выбранный
    # ход камень/ножницы/бумага. Ход выбирается ДО поиска соперника — это
    # позволяет разрешать матч мгновенно, синхронно, в одном обработчике,
    # без хрупкого ожидания действия второго игрока в оперативной памяти.
    for col_name, col_type in [("username", "TEXT"), ("choice", "TEXT")]:
        try:
            cur.execute(f"ALTER TABLE duels ADD COLUMN {col_name} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Миграция таблицы jackpot: раньше был один победитель (last_winner_id),
    # теперь джекпот делится между несколькими игроками сразу — храним их
    # id через запятую в last_winner_ids, не трогая старую колонку.
    try:
        cur.execute("ALTER TABLE jackpot ADD COLUMN last_winner_ids TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Разовый бэкфилл: если drop_log ещё пуст, а в inventory уже есть
    # предметы (бот обновился с более старой версии) — копируем их в
    # журнал один раз, чтобы существующие игроки не потеряли историю
    # своих лучших дропов из-за появления новой таблицы.
    cur.execute("SELECT COUNT(*) AS c FROM drop_log")
    if cur.fetchone()["c"] == 0:
        cur.execute("SELECT COUNT(*) AS c FROM inventory")
        if cur.fetchone()["c"] > 0:
            cur.execute("""
                INSERT INTO drop_log (user_id, item_name, item_rarity, item_price, obtained_at)
                SELECT user_id, item_name, item_rarity, item_price, obtained_at FROM inventory
            """)
            conn.commit()

    conn.close()


# ---------- Пользователи и баланс ----------

def get_or_create_user(user_id: int, username: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cur.fetchone()

    if user is None:
        cur.execute(
            "INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)",
            (user_id, username, config.START_BALANCE)
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cur.fetchone()

    conn.close()
    return user


def get_balance(user_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["balance"] if row else 0


def add_balance(user_id: int, amount: int):
    """Изменяет баланс пользователя и попутно ведёт учёт того, сколько всего
    монет было получено (amount > 0) или потрачено/проиграно (amount < 0) —
    это используется в профиле игрока."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    if amount > 0:
        cur.execute("UPDATE users SET total_earned = total_earned + ? WHERE user_id = ?", (amount, user_id))
    elif amount < 0:
        cur.execute("UPDATE users SET total_spent = total_spent + ? WHERE user_id = ?", (-amount, user_id))
    conn.commit()
    conn.close()


def get_last_work_time(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT last_work_time FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["last_work_time"] if row and row["last_work_time"] else None


def set_last_work_time(user_id: int, time_str: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_work_time = ? WHERE user_id = ?", (time_str, user_id))
    conn.commit()
    conn.close()


def get_last_bonus_time(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT last_bonus_time FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["last_bonus_time"] if row and row["last_bonus_time"] else None


def set_last_bonus_time(user_id: int, time_str: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_bonus_time = ? WHERE user_id = ?", (time_str, user_id))
    conn.commit()
    conn.close()


def set_bonus_notified(user_id: int, value: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET bonus_notified = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()


def get_users_awaiting_bonus():
    """Возвращает всех пользователей, которые уже когда-то получали бонус
    (last_bonus_time задан) и ещё не получили уведомление о готовности
    следующего. Фильтрация по времени делается в Python — вызывающий код
    (фоновая задача) сам решает, прошёл ли кулдаун."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, last_bonus_time FROM users "
        "WHERE last_bonus_time IS NOT NULL AND (bonus_notified IS NULL OR bonus_notified = 0)"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_user_ids():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    conn.close()
    return [row["user_id"] for row in rows]


def get_top_users(limit: int = 10):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_top_users_by_level(limit: int = 10):
    """Топ игроков по уровню. При равном уровне выше тот, у кого больше
    накопленного опыта на текущем уровне (xp) — так топ не "залипает"
    на месте между уровнями."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, username, level, xp FROM users ORDER BY level DESC, xp DESC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_top_drops(limit: int = 10):
    """Топ по дропу: для КАЖДОГО игрока берём его самый дорогой предмет,
    когда-либо выпавший из кейсов за ВСЁ время (в том числе уже проданные,
    апгрейднутые или сгоревшие предметы), и ранжируем игроков по этому
    значению. Читаем из drop_log — постоянного журнала, а не из inventory,
    откуда предметы могут быть удалены."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT users.user_id, users.username,
               drop_log.item_name, drop_log.item_rarity, drop_log.item_price
        FROM drop_log
        JOIN users ON users.user_id = drop_log.user_id
        JOIN (
            SELECT user_id, MAX(item_price) AS best_price
            FROM drop_log
            GROUP BY user_id
        ) best ON best.user_id = drop_log.user_id AND best.best_price = drop_log.item_price
        GROUP BY users.user_id
        ORDER BY drop_log.item_price DESC
        LIMIT ?
        """,
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------- Инвентарь ----------

def add_item(user_id: int, item_name: str, item_rarity: str, item_price: int) -> int:
    """Добавляет предмет в инвентарь пользователя, а также пишет тот же
    дроп в drop_log — постоянный журнал, который не трогается при продаже,
    апгрейде или сгорании предмета (нужен для топа "по дропу за всё время").
    Возвращает id предмета в инвентаре."""
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute(
        "INSERT INTO inventory (user_id, item_name, item_rarity, item_price, obtained_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, item_name, item_rarity, item_price, now)
    )
    item_id = cur.lastrowid
    cur.execute(
        "INSERT INTO drop_log (user_id, item_name, item_rarity, item_price, obtained_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, item_name, item_rarity, item_price, now)
    )
    conn.commit()
    conn.close()
    return item_id


def get_inventory(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM inventory WHERE user_id = ? ORDER BY item_price DESC",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_item_by_id(item_id: int, user_id: int):
    """Достаёт предмет, только если он принадлежит этому пользователю (защита от подмены id)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM inventory WHERE id = ? AND user_id = ?",
        (item_id, user_id)
    )
    row = cur.fetchone()
    conn.close()
    return row


def remove_item(item_id: int, user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM inventory WHERE id = ? AND user_id = ?",
        (item_id, user_id)
    )
    conn.commit()
    conn.close()


def remove_all_items(user_id: int):
    """Удаляет сразу весь инвентарь пользователя (используется кнопкой
    "Продать всё"), одним запросом вместо цикла по отдельным предметам."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM inventory WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def update_item_price(item_id: int, user_id: int, new_price: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE inventory SET item_price = ? WHERE id = ? AND user_id = ?",
        (new_price, item_id, user_id)
    )
    conn.commit()
    conn.close()


# ---------- PvP-дуэли ----------

def create_duel(user_id: int, bet: int, username: str = None, choice: str = None) -> int:
    """Ставит пользователя в очередь ожидания соперника. Возвращает id дуэли.
    username сохраняется, чтобы потом показать сопернику, "с кем он играл".
    choice — уже выбранный ход камень/ножницы/бумага, чтобы матч можно было
    разрешить мгновенно, как только найдётся соперник."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO duels (user_id, bet, status, created_at, username, choice) "
        "VALUES (?, ?, 'waiting', ?, ?, ?)",
        (user_id, bet, datetime.now().isoformat(), username, choice)
    )
    conn.commit()
    duel_id = cur.lastrowid
    conn.close()
    return duel_id


def find_waiting_opponent(bet: int, exclude_user_id: int):
    """Ищет самую старую дуэль в очереди с такой же ставкой от другого игрока."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM duels WHERE bet = ? AND status = 'waiting' AND user_id != ? "
        "ORDER BY created_at ASC LIMIT 1",
        (bet, exclude_user_id)
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_waiting_duel_by_user(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM duels WHERE user_id = ? AND status = 'waiting'", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_duel(duel_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM duels WHERE id = ?", (duel_id,))
    row = cur.fetchone()
    conn.close()
    return row


def delete_duel(duel_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM duels WHERE id = ?", (duel_id,))
    conn.commit()
    conn.close()


# ---------- Статистика ----------

# Список разрешён явно и не принимает значения снаружи (например, от
# callback_data), чтобы имя колонки нельзя было подставить через SQL-инъекцию.
STAT_COLUMNS = {
    "work_correct", "work_wrong", "cases_opened",
    "duels_played", "duels_won", "upgrades_success", "upgrades_failed",
    "jackpot_wins", "td_battles_played", "td_wins",
    "td_shots_fired", "td_hits", "td_headshots", "td_damage_dealt",
}


def increment_stat(user_id: int, column: str, amount: int = 1):
    """Увеличивает одну из статистических колонок пользователя на amount."""
    if column not in STAT_COLUMNS:
        raise ValueError(f"Недопустимая колонка статистики: {column}")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE users SET {column} = {column} + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def get_user_full_stats(user_id: int):
    """Возвращает строку users вместе с агрегатами по инвентарю:
    количество предметов, суммарная стоимость и самый дорогой предмет."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(item_price), 0) AS total, "
        "COALESCE(MAX(item_price), 0) AS best_price "
        "FROM inventory WHERE user_id = ?",
        (user_id,)
    )
    inv = cur.fetchone()

    best_item_name = None
    if inv["best_price"] > 0:
        cur.execute(
            "SELECT item_name FROM inventory WHERE user_id = ? AND item_price = ? LIMIT 1",
            (user_id, inv["best_price"])
        )
        row = cur.fetchone()
        best_item_name = row["item_name"] if row else None

    conn.close()
    return {
        "user": user,
        "items_count": inv["cnt"],
        "items_total_value": inv["total"],
        "best_item_name": best_item_name,
        "best_item_price": inv["best_price"],
    }


# ---------- Tower Defence Web App (Этап 3) ----------

def get_best_wave(user_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT td_best_wave FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["td_best_wave"] if row and row["td_best_wave"] is not None else 0


def record_td_battle_result(
    user_id: int, wave_reached: int, won: bool, reward_coins: int, reward_xp: int,
    shots_fired: int = 0, hits: int = 0, headshots: int = 0, damage_dealt: int = 0,
):
    """Фиксирует итог одного боя Tower Defence:
    - увеличивает счётчик сыгранных боёв (и побед, если бой выигран);
    - обновляет рекорд по волнам, только если новый результат выше;
    - начисляет монеты (через add_balance — total_earned обновится сам)
      и опыт (через add_xp — обработает и повышение уровня);
    - копит статистику стрельбы за всё время (Этап 4: точность/хедшоты/
      урон) — используется для процента точности в профиле.
    Возвращает актуальный рекорд волны и результат начисления опыта."""
    increment_stat(user_id, "td_battles_played")
    if won:
        increment_stat(user_id, "td_wins")
    if shots_fired:
        increment_stat(user_id, "td_shots_fired", shots_fired)
    if hits:
        increment_stat(user_id, "td_hits", hits)
    if headshots:
        increment_stat(user_id, "td_headshots", headshots)
    if damage_dealt:
        increment_stat(user_id, "td_damage_dealt", damage_dealt)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT td_best_wave FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    current_best = row["td_best_wave"] if row and row["td_best_wave"] is not None else 0
    new_best = max(current_best, wave_reached)
    if new_best != current_best:
        cur.execute("UPDATE users SET td_best_wave = ? WHERE user_id = ?", (new_best, user_id))
        conn.commit()
    conn.close()

    if reward_coins:
        add_balance(user_id, reward_coins)
    xp_result = add_xp(user_id, reward_xp) if reward_xp else None

    return {
        "best_wave": new_best,
        "xp_result": xp_result,
    }


# ---------- Джекпот ----------

def get_jackpot():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jackpot WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return row


def add_to_jackpot(amount: int):
    """Добавляет 'проигранные' монеты в копилку джекпота (комиссия с дуэлей,
    сгоревшие при апгрейде предметы)."""
    if amount <= 0:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE jackpot SET amount = amount + ? WHERE id = 1", (amount,))
    conn.commit()
    conn.close()


def touch_jackpot_draw_time():
    """Сдвигает время последнего розыгрыша, не трогая данные о победителе —
    используется, когда пришло время розыгрыша, но копилка пуста."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE jackpot SET last_draw_at = ? WHERE id = 1", (datetime.now().isoformat(),))
    conn.commit()
    conn.close()


def draw_jackpot(winner_id: int, amount: int):
    """Фиксирует итоги розыгрыша: обнуляет копилку и запоминает победителя."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE jackpot SET amount = 0, last_draw_at = ?, last_winner_id = ?, "
        "last_winner_amount = ? WHERE id = 1",
        (datetime.now().isoformat(), winner_id, amount)
    )
    conn.commit()
    conn.close()


def draw_jackpot_multi(winner_ids: list, amount_each: int):
    """Фиксирует итоги розыгрыша джекпота сразу между несколькими игроками:
    списывает с копилки amount_each * len(winner_ids) (остаток, если он был,
    переносится на следующий розыгрыш) и запоминает id всех победителей."""
    ids_str = ",".join(str(uid) for uid in winner_ids)
    total = amount_each * len(winner_ids)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE jackpot SET amount = amount - ?, last_draw_at = ?, "
        "last_winner_ids = ?, last_winner_amount = ? WHERE id = 1",
        (total, datetime.now().isoformat(), ids_str, amount_each)
    )
    conn.commit()
    conn.close()


# ---------- Уровни и опыт ----------

def xp_needed_for_level(level: int) -> int:
    """Сколько XP нужно, чтобы перейти с уровня `level` на следующий."""
    return config.LEVEL_XP_BASE + (level - 1) * config.LEVEL_XP_STEP


def get_level_info(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT xp, level FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    xp = row["xp"] if row else 0
    level = row["level"] if row else 1
    return {
        "xp": xp,
        "level": level,
        "xp_needed": xp_needed_for_level(level),
    }


def add_xp(user_id: int, amount: int):
    """Начисляет опыт и обрабатывает повышение уровня (может быть сразу
    несколько уровней за раз, если начислили много опыта). За каждый новый
    уровень игрок получает бонус монетами (см. config.LEVEL_UP_BONUS_PER_LEVEL).
    Возвращает информацию о результате, включая флаг leveled_up."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT xp, level FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    xp = (row["xp"] if row else 0) + amount
    level = row["level"] if row else 1

    levels_gained = 0
    bonus_awarded = 0
    while xp >= xp_needed_for_level(level):
        xp -= xp_needed_for_level(level)
        level += 1
        levels_gained += 1
        bonus_awarded += level * config.LEVEL_UP_BONUS_PER_LEVEL

    cur.execute("UPDATE users SET xp = ?, level = ? WHERE user_id = ?", (xp, level, user_id))
    if bonus_awarded > 0:
        cur.execute(
            "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?",
            (bonus_awarded, bonus_awarded, user_id)
        )
    conn.commit()
    conn.close()

    return {
        "leveled_up": levels_gained > 0,
        "levels_gained": levels_gained,
        "new_level": level,
        "xp": xp,
        "xp_needed": xp_needed_for_level(level),
        "bonus_awarded": bonus_awarded,
    }


# ---------- Общая статистика бота (для админов) ----------

def get_global_stats():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS users_count, "
        "COALESCE(SUM(balance), 0) AS total_balance, "
        "COALESCE(SUM(total_earned), 0) AS total_earned, "
        "COALESCE(SUM(total_spent), 0) AS total_spent, "
        "COALESCE(SUM(duels_played), 0) AS duels_played, "
        "COALESCE(SUM(cases_opened), 0) AS cases_opened, "
        "COALESCE(SUM(upgrades_success), 0) AS upgrades_success, "
        "COALESCE(SUM(upgrades_failed), 0) AS upgrades_failed "
        "FROM users"
    )
    users_row = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*) AS items_count, COALESCE(SUM(item_price), 0) AS items_total_value "
        "FROM inventory"
    )
    inv_row = cur.fetchone()

    cur.execute("SELECT amount FROM jackpot WHERE id = 1")
    jackpot_row = cur.fetchone()

    conn.close()
    return {
        "users_count": users_row["users_count"],
        "total_balance": users_row["total_balance"],
        "total_earned": users_row["total_earned"],
        "total_spent": users_row["total_spent"],
        "duels_played": users_row["duels_played"],
        "cases_opened": users_row["cases_opened"],
        "upgrades_success": users_row["upgrades_success"],
        "upgrades_failed": users_row["upgrades_failed"],
        "items_count": inv_row["items_count"],
        "items_total_value": inv_row["items_total_value"],
        "jackpot_amount": jackpot_row["amount"] if jackpot_row else 0,
    }
