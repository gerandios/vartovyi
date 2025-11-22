import os
import logging
import calendar
import re
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List

from fastapi import FastAPI, Request, HTTPException, Header, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    CallbackContext,
)

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

import uvicorn

# --- Настройка таймзони (Київ) ---
KYIV_TZ = ZoneInfo("Europe/Kiev")

# --- Настройка логування ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Змінні оточення ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
API_KEY = os.getenv('API_KEY')
DOMAIN = os.getenv('RENDER_EXTERNAL_HOSTNAME')
ADMIN_IDS_STR = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = [int(aid) for aid in ADMIN_IDS_STR.split(',') if aid.strip().isdigit()]

if not all([BOT_TOKEN, DATABASE_URL, API_KEY, DOMAIN]):
    logger.warning("⚠️ Увага: Деякі змінні оточення не задані!")

WEBHOOK_PATH = '/webhook'
WEBHOOK_URL = f"https://{DOMAIN}{WEBHOOK_PATH}"

# --- Пул з'єднань ---
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=True)

# --- МІГРАЦІЯ ---
def migrate_database():
    logger.info("Checking DB schema...")
    try:
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, rank VARCHAR, name VARCHAR, username VARCHAR, group_number VARCHAR, registration_date TIMESTAMP WITH TIME ZONE NOT NULL);")
                cur.execute("CREATE TABLE IF NOT EXISTS registrations (id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE, event_type VARCHAR NOT NULL, event_date DATE NOT NULL, reason VARCHAR, return_info VARCHAR, UNIQUE (user_id, event_date));")
                cur.execute("CREATE TABLE IF NOT EXISTS ranks (id SERIAL PRIMARY KEY, name VARCHAR UNIQUE NOT NULL);")
                
                default_ranks = ['солдат', 'ст. солдат', 'молодший сержант', 'сержант']
                for rank_name in default_ranks:
                    cur.execute("INSERT INTO ranks (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (rank_name,))
                
                conn.commit()
        logger.info("Database ready.")
    except Exception as e:
        logger.error(f"FATAL: Database migration failed: {e}")
        raise

migrate_database()

# --- СТАНИ ---
(
    REG_RANK, REG_SURNAME, REG_FIRSTNAME, REG_GROUP, 
    MAIN_MENU, 
    CHOOSE_DATE, CHOOSE_TYPE, CHOOSE_DOVOBE_REASON, CHOOSE_DOZVIL_TIME
) = range(9)


# --- БД ФУНКЦІЇ ---
def insert_user(user_id: int, rank: str, name: str, username: str | None, group_number: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, rank, name, username, group_number, registration_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                rank = EXCLUDED.rank,
                name = EXCLUDED.name,
                username = EXCLUDED.username,
                group_number = EXCLUDED.group_number;
            """,
            (user_id, rank, name, username, group_number, datetime.now(timezone.utc)),
        )

def get_user(user_id: int) -> dict | None:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()

def get_all_users() -> list:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT user_id, rank, name, group_number FROM users ORDER BY group_number, name")
            return cur.fetchall()

def delete_user_db(user_id: int) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))

def update_user_from_admin(user_id: int, rank: str, name: str, group_number: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE users SET rank = %s, name = %s, group_number = %s WHERE user_id = %s", (rank, name, group_number, user_id))

def insert_registration(user_id: int, event_type: str, event_date: date, reason: str | None, return_info: str | None) -> bool:
    try:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO registrations (user_id, event_type, event_date, reason, return_info) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, event_date) DO UPDATE SET event_type = EXCLUDED.event_type, reason = EXCLUDED.reason, return_info = EXCLUDED.return_info",
                (user_id, event_type, event_date, reason, return_info)
            )
        return True
    except psycopg.errors.UniqueViolation:
        return False

def get_user_registrations(user_id: int) -> list:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM registrations WHERE user_id = %s AND event_date >= %s ORDER BY event_date ASC", (user_id, date.today()))
            return cur.fetchall()

def delete_registration(reg_id: int) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM registrations WHERE id = %s", (reg_id,))

def get_lists_for_date(target_date: date) -> dict:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT r.event_type, CONCAT(u.rank, ' ', u.name) AS full_name, u.username, u.group_number,
                       r.reason, r.return_info
                FROM registrations r JOIN users u ON r.user_id = u.user_id
                WHERE r.event_date = %s ORDER BY u.group_number, u.name
                """, (target_date,)
            )
            rows = cur.fetchall()
    lists = {"Звичайне": [], "Добове": []}
    for row in rows:
        row_data = dict(row)
        lists[row['event_type']].append(row_data)
    return {"request_date": target_date.isoformat(), "total_registrations": len(rows), "lists": lists}

def clear_future_registrations() -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM registrations WHERE event_date >= %s", (date.today(),))
            return cur.rowcount

def wipe_all_data() -> None:
    with pool.connection() as conn:
        conn.execute("TRUNCATE TABLE registrations, users, ranks RESTART IDENTITY;")

def get_all_ranks() -> List[str]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM ranks ORDER BY name;")
            return [row[0] for row in cur.fetchall()]

def add_rank(rank_name: str):
    try:
        with pool.connection() as conn:
            conn.execute("INSERT INTO ranks (name) VALUES (%s);", (rank_name.lower(),))
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Rank already exists.")

def delete_rank(rank_name: str):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users WHERE rank = %s;", (rank_name,))
            if cur.fetchone()[0] > 0:
                raise HTTPException(status_code=409, detail="Rank is in use.")
            cur.execute("DELETE FROM ranks WHERE name = %s;", (rank_name,))


# --- UI ФУНКЦІЇ ---
def create_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    keyboard = []
    uk_month_names = ["", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]
    keyboard.append([InlineKeyboardButton(f"{uk_month_names[month]} {year}", callback_data='ignore')])
    keyboard.append([InlineKeyboardButton(day, callback_data='ignore') for day in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]])
    
    month_calendar = calendar.monthcalendar(year, month)
    now_kyiv = datetime.now(KYIV_TZ)
    today = now_kyiv.date()
    
    # Відображаємо всі дні, а перевірку доступності робимо при кліку
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data='ignore'))
            else:
                current_date = date(year, month, day)
                if current_date < today:
                    row.append(InlineKeyboardButton(f"~{day}~", callback_data='ignore'))
                else:
                    row.append(InlineKeyboardButton(str(day), callback_data=f'day:{current_date.isoformat()}'))
        keyboard.append(row)
    
    prev_d = date(year, month, 1) - timedelta(days=1)
    next_d = date(year, month, 1) + timedelta(days=32)
    keyboard.append([
        InlineKeyboardButton("<", callback_data=f'nav:{prev_d.year}:{prev_d.month}'),
        InlineKeyboardButton(">", callback_data=f'nav:{next_d.year}:{next_d.month}')
    ])
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: CallbackContext):
    keyboard = [['Записатись на звільнення', 'Мої записи']]
    
    # Текст з ПРАВИЛАМИ
    info_text = (
        "🏠 **Головне меню**\n\n"
        "📜 **ГРАФІК ПОДАЧІ ЗАЯВОК:**\n\n"
        "1️⃣ **На сьогодні:** до 16:00.\n"
        "2️⃣ **На Пт, Сб, Нд:** до 17:00 Четверга.\n\n"
        "⚠️ _Якщо ви намагаєтесь записатись на вихідні у четвер після 17:00 — система вас не пропустить._"
    )
    
    await update.message.reply_text(
        info_text, 
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode='Markdown'
    )

# --- ЛОГІКА РЕЄСТРАЦІЇ ---

async def start_router(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    context.user_data.clear()
    user = get_user(user_id)
    if user:
        info_text = (
            f"Вітаю, {user['rank']} {user['name']}!\n\n"
            "📜 **ГРАФІК ПОДАЧІ ЗАЯВОК:**\n"
            "1️⃣ **На сьогодні:** до 16:00.\n"
            "2️⃣ **На Пт, Сб, Нд:** до 17:00 Четверга."
        )
        await update.message.reply_text(
            info_text, 
            reply_markup=ReplyKeyboardMarkup([['Записатись на звільнення', 'Мої записи']], resize_keyboard=True),
            parse_mode='Markdown'
        )
        return MAIN_MENU
    else:
        ranks = get_all_ranks()
        keyboard = []
        row = []
        for r in ranks:
            row.append(r.capitalize())
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row: keyboard.append(row)
            
        await update.message.reply_text("Вітаю! Розпочнемо реєстрацію.\n1️⃣ **Крок 1 з 4:**\nОберіть ваше **звання**:", reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True), parse_mode='Markdown')
        return REG_RANK

async def register_rank(update: Update, context: CallbackContext) -> int:
    selected_rank = update.message.text.lower()
    if selected_rank not in [r.lower() for r in get_all_ranks()]:
        await update.message.reply_text("⚠️ Оберіть звання з меню.")
        return REG_RANK
    context.user_data['rank'] = selected_rank
    await update.message.reply_text("✅ Звання прийнято.\n\n2️⃣ **Крок 2 з 4:**\nВведіть ваше **ПРІЗВИЩЕ** (лише прізвище).\n📌 *Приклад:* Шевченко", reply_markup=ReplyKeyboardRemove(), parse_mode='Markdown')
    return REG_SURNAME

async def register_surname(update: Update, context: CallbackContext) -> int:
    raw_text = update.message.text.strip()
    if len(raw_text) < 2 or not re.match(r"^[a-zA-Zа-яА-ЯіІїЇєЄґҐ\-\']+$", raw_text):
        await update.message.reply_text("⚠️ Помилка. Введіть коректне прізвище (тільки літери).")
        return REG_SURNAME
    context.user_data['surname'] = raw_text.capitalize()
    await update.message.reply_text("✅ Прізвище прийнято.\n\n3️⃣ **Крок 3 з 4:**\nВведіть ваше **ІМ'Я** або **ІНІЦІАЛИ**.\n📌 *Приклад:* Тарас або Т.Г.", parse_mode='Markdown')
    return REG_FIRSTNAME

async def register_firstname(update: Update, context: CallbackContext) -> int:
    raw_text = update.message.text.strip()
    if len(raw_text) < 1 or (re.match(r"^[\d\s\W]+$", raw_text) and not re.search(r"[a-zA-Zа-яА-Я]", raw_text)):
        await update.message.reply_text("⚠️ Введіть коректне ім'я або ініціали.")
        return REG_FIRSTNAME

    surname = context.user_data['surname']
    
    # ФОРМАТ: "І. Прізвище"
    initial = raw_text[0].upper()
    full_name = f"{initial}. {surname}"
    
    context.user_data['name'] = full_name
    
    await update.message.reply_text(
        f"Ваше ім'я в системі буде: **{full_name}**\n\n"
        "4️⃣ **Крок 4 з 4:**\n"
        "Введіть номер вашої **ГРУПИ** (тільки цифри).",
        parse_mode='Markdown'
    )
    return REG_GROUP

async def register_group(update: Update, context: CallbackContext) -> int:
    group_number = update.message.text.strip()
    if not group_number.isdigit() or len(group_number) > 5:
        await update.message.reply_text("⛔️ Номер групи має складатися тільки з цифр.")
        return REG_GROUP

    rank = context.user_data['rank']
    name = context.user_data['name']
    insert_user(update.effective_user.id, rank, name, update.effective_user.username, group_number)
    
    await update.message.reply_text(f'✅ **РЕЄСТРАЦІЮ ЗАВЕРШЕНО!**\n👤 {rank.capitalize()} {name}\n🎓 Група: {group_number}', parse_mode='Markdown')
    await show_main_menu(update, context)
    context.user_data.clear()
    return MAIN_MENU

# --- МЕНЮ ---
async def handle_menu_choice(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if text == 'Записатись на звільнення':
        now_kyiv = datetime.now(KYIV_TZ)
        today = now_kyiv.date()
        tomorrow = today + timedelta(days=1)
        keyboard = []
        # Кнопки для зручності. Логіка перевірки часу тепер в callback_handler
        keyboard.append([InlineKeyboardButton('На сьогодні', callback_data=f'day:{today.isoformat()}')])
        keyboard.append([InlineKeyboardButton('На завтра', callback_data=f'day:{tomorrow.isoformat()}')])
        keyboard.append([InlineKeyboardButton('Обрати іншу дату', callback_data='calendar')])
        await update.message.reply_text('Оберіть дату:', reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_DATE
    elif text == 'Мої записи':
        regs = get_user_registrations(update.effective_user.id)
        if not regs:
            await update.message.reply_text('Записів немає.')
        else:
            for reg in regs:
                msg = f'📅 {reg["event_date"]:%d.%m.%Y} | {reg["event_type"]}'
                if reg["reason"]: msg += f'\n📝 {reg["reason"]}'
                if reg["return_info"]: msg += f'\n⏰ {reg["return_info"]}'
                await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('❌ Скасувати', callback_data=f'cancel:{reg["id"]}')]]))
        return MAIN_MENU
    return MAIN_MENU

# ----------------------------------------------------------------
# 🔥 ГОЛОВНА ЛОГІКА ПЕРЕВІРКИ ДАТИ І ЧАСУ
# ----------------------------------------------------------------
async def date_callback_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    # Не робимо query.answer() одразу, щоб мати змогу показати Alert
    
    data = query.data
    now = datetime.now(KYIV_TZ)

    # --- НАВИГАЦІЯ ПО КАЛЕНДАРЮ ---
    if data == 'calendar':
        await query.answer()
        await query.edit_message_text("Оберіть дату:", reply_markup=create_calendar(now.year, now.month))
        return CHOOSE_DATE
    elif data.startswith('nav:'):
        await query.answer()
        _, year, month = data.split(':')
        await query.edit_message_text("Оберіть дату:", reply_markup=create_calendar(int(year), int(month)))
        return CHOOSE_DATE
    
    # --- ВИБІР ДАТИ ---
    elif data.startswith('day:'):
        selected_date = date.fromisoformat(data.split(':')[1])
        
        # 1. НЕ МОЖНА В МИНУЛЕ
        if selected_date < now.date():
            await query.answer("⚠️ Не можна обрати минулу дату.", show_alert=True)
            return CHOOSE_DATE
            
        # 2. ПРАВИЛО "НА СЬОГОДНЯ"
        # Якщо обрана дата = сьогодні, перевіряємо 16:00
        if selected_date == now.date():
            if now.hour >= 16:
                 await query.answer(
                     "⛔️ ЗАПИС НА СЬОГОДНІ ЗАЧИНЕНО!\n\n"
                     "Подавати заявку «день у день» можна лише до 16:00.", 
                     show_alert=True
                 )
                 return CHOOSE_DATE

        # 3. ПРАВИЛО "НА ВИХІДНІ" (Пт, Сб, Нд)
        # Дедлайн: Четверг 17:00
        target_dow = selected_date.weekday() # 0=Пн, ..., 3=Чт, 4=Пт, 5=Сб, 6=Нд
        
        if target_dow in [4, 5, 6]: # Якщо обрали Пт, Сб або Нд
            # Знаходимо дату Четверга цього тижня
            # (Віднімаємо різницю днів, щоб потрапити в день №3 - Четверг)
            days_diff = target_dow - 3
            deadline_date = selected_date - timedelta(days=days_diff)
            
            # Встановлюємо дедлайн: Четверг 17:00:00
            deadline_dt = datetime(
                deadline_date.year, 
                deadline_date.month, 
                deadline_date.day, 
                17, 0, 0, 
                tzinfo=KYIV_TZ
            )
            
            # Якщо зараз часу більше, ніж дедлайн -> БЛОК
            if now > deadline_dt:
                error_text = (
                    "⛔️ ЗАПИС НА ВИХІДНІ ЗАЧИНЕНО!\n\n"
                    "Згідно правил, списки на Пт, Сб, Нд закриваються "
                    "автоматично у ЧЕТВЕР о 17:00.\n\n"
                    "Ви не встигли."
                )
                await query.answer(error_text, show_alert=True)
                return CHOOSE_DATE

        # --- ЯКЩО ВСЕ ОК ---
        await query.answer()
        context.user_data['selected_date'] = selected_date
        
        # Формування кнопок типу звільнення
        if target_dow == 5: # Субота
            keyboard = [[InlineKeyboardButton('Звичайне', callback_data='type:Звичайне'), InlineKeyboardButton('Добове (до 08:30)', callback_data='type:Добове:auto_saturday')]]
        else: 
            keyboard = [[InlineKeyboardButton('Звичайне', callback_data='type:Звичайне'), InlineKeyboardButton('Добове', callback_data='type:Добове')]]
        
        await query.edit_message_text(f"Дата: {selected_date:%d.%m.%Y}. Тип звільнення:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_TYPE

async def choose_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(':')
    context.user_data['event_type'] = parts[1]
    if parts[1] == 'Звичайне': return await save_registration(update, context, None, "до 21:30")
    if len(parts) > 2: return await save_registration(update, context, "рапорт", "до 08:30")
    await query.edit_message_text("Підстава:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Рапорт', callback_data='reason:рапорт')], [InlineKeyboardButton('Дозвіл Н.І.', callback_data='reason:дозвіл')]]))
    return CHOOSE_DOVOBE_REASON

async def choose_dovobe_reason(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    reason = "рапорт" if query.data.split(':')[1] == "рапорт" else "дозвіл Н.І."
    context.user_data['reason'] = reason
    if reason == "рапорт": return await save_registration(update, context, reason, "до 06:00")
    await query.edit_message_text("До котрої:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('До 06:00', callback_data='dozvil_time:06:00')], [InlineKeyboardButton('До 08:00', callback_data='dozvil_time:08:00')]]))
    return CHOOSE_DOZVIL_TIME

async def choose_dozvil_time(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    return await save_registration(update, context, context.user_data.get('reason'), f"до {query.data.split(':')[1]}")

async def save_registration(update: Update, context: CallbackContext, reason, return_info) -> int:
    insert_registration(update.effective_user.id, context.user_data['event_type'], context.user_data['selected_date'], reason, return_info)
    await update.callback_query.edit_message_text("✅ Запис збережено!")
    context.user_data.clear()
    return MAIN_MENU

async def cancel(update: Update, context: CallbackContext) -> int:
    if update.callback_query: await update.callback_query.edit_message_text("Скасовано.")
    else: await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    if get_user(update.effective_user.id): await show_main_menu(update, context)
    return MAIN_MENU if get_user(update.effective_user.id) else ConversationHandler.END

async def cancel_registration(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    delete_registration(int(query.data.split(':')[1]))
    await query.edit_message_text('✅ Запис видалено.')

# --- АДМІН ПАНЕЛЬ ---

async def admin_panel(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    keyboard = [
        [InlineKeyboardButton("👥 Керування користувачами", callback_data='admin:users_list')],
        [InlineKeyboardButton("🗑 Видалити майбутні записи", callback_data='admin:clear_regs')],
        [InlineKeyboardButton("⚠️ ОЧИСТИТИ ВСЕ (WIPE) ⚠️", callback_data='admin:wipe_all')],
        [InlineKeyboardButton("Скасувати", callback_data='admin:cancel')]
    ]
    await update.message.reply_text("Адмін-панель:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔️ Доступ заборонено.")
        return

    data = query.data
    
    if data == 'admin:main':
        keyboard = [
            [InlineKeyboardButton("👥 Керування користувачами", callback_data='admin:users_list')],
            [InlineKeyboardButton("🗑 Видалити майбутні записи", callback_data='admin:clear_regs')],
            [InlineKeyboardButton("⚠️ ОЧИСТИТИ ВСЕ (WIPE) ⚠️", callback_data='admin:wipe_all')],
            [InlineKeyboardButton("Скасувати", callback_data='admin:cancel')]
        ]
        await query.edit_message_text("Адмін-панель:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == 'admin:users_list':
        users = get_all_users()
        keyboard = []
        if not users:
            await query.edit_message_text("Список користувачів порожній.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin:main')]]))
            return
        for u in users:
            btn_text = f"{u['group_number']} | {u['rank']} {u['name']}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin:u_act:{u['user_id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='admin:main')])
        await query.edit_message_text("Оберіть користувача для редагування:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith('admin:u_act:'):
        user_id = int(data.split(':')[2])
        user = get_user(user_id)
        if not user:
            await query.edit_message_text("Користувача не знайдено (можливо, вже видалений).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 До списку", callback_data='admin:users_list')]]))
            return
        text = (
            f"👤 **Користувач:**\n"
            f"Звання: {user['rank']}\n"
            f"Ім'я: {user['name']}\n"
            f"Група: {user['group_number']}\n"
            f"Telegram ID: `{user['user_id']}`"
        )
        keyboard = [
            [InlineKeyboardButton("❌ ВИДАЛИТИ З БАЗИ", callback_data=f"admin:u_del:{user_id}")],
            [InlineKeyboardButton("✏️ Редагувати (заглушка)", callback_data=f"admin:u_edit:{user_id}")],
            [InlineKeyboardButton("🔙 До списку", callback_data='admin:users_list')]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data.startswith('admin:u_del:'):
        user_id = int(data.split(':')[2])
        delete_user_db(user_id)
        await query.answer("Користувача видалено!", show_alert=True)
        query.data = 'admin:users_list'
        await admin_panel_callback(update, context)

    elif data.startswith('admin:u_edit:'):
        await query.answer("⚠️ Функція редагування через бот тимчасово недоступна.\nВидаліть користувача та скажіть йому зареєструватися наново, або використайте API.", show_alert=True)

    elif data == 'admin:clear_regs':
        count = clear_future_registrations()
        await query.edit_message_text(f"✅ Видалено {count} записів.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin:main')]]))
    elif data == 'admin:wipe_all':
        wipe_all_data()
        await query.edit_message_text("✅🔴 БАЗА ДАНИХ ОЧИЩЕНА ПОВНІСТЮ.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data='admin:main')]]))
    elif data == 'admin:cancel':
        await query.edit_message_text("Адмін-панель закрито.")

async def ignore_callback(update: Update, context: CallbackContext):
    if update.callback_query: await update.callback_query.answer()

# --- FastAPI ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
application = ApplicationBuilder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CommandHandler('start', start_router), MessageHandler(filters.TEXT & ~filters.COMMAND, start_router)],
    states={
        REG_RANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_rank)],
        REG_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname)],
        REG_FIRSTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_firstname)],
        REG_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_group)],
        MAIN_MENU: [MessageHandler(filters.Regex('^Записатись на звільнення$'), handle_menu_choice), MessageHandler(filters.Regex('^Мої записи$'), handle_menu_choice)],
        CHOOSE_DATE: [CallbackQueryHandler(date_callback_handler, pattern='^(day:|nav:|calendar)')],
        CHOOSE_TYPE: [CallbackQueryHandler(choose_type, pattern='^type:')],
        CHOOSE_DOVOBE_REASON: [CallbackQueryHandler(choose_dovobe_reason, pattern='^reason:')],
        CHOOSE_DOZVIL_TIME: [CallbackQueryHandler(choose_dozvil_time, pattern='^dozvil_time:')],
    },
    fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start_router)],
)

application.add_handler(conv_handler)
application.add_handler(CallbackQueryHandler(cancel_registration, pattern='^cancel:'))
application.add_handler(CommandHandler('admin', admin_panel))
application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern='^admin:'))
application.add_handler(CallbackQueryHandler(ignore_callback, pattern='^ignore'))

class UserUpdate(BaseModel):
    rank: str
    name: str
    group_number: str
class RankCreate(BaseModel):
    name: str

@app.post(WEBHOOK_PATH)
async def process_update(request: Request):
    await application.process_update(Update.de_json(await request.json(), application.bot))
    return {"ok": True}

@app.get("/api/lists/{date_str}")
async def get_lists_api(date_str: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(403)
    return get_lists_for_date(date.fromisoformat(date_str))

@app.get("/api/users")
async def get_users_list_api(x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(403)
    return get_all_users()

@app.put("/api/users/{user_id}")
async def update_user_api(user_id: int, user_data: UserUpdate, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(403)
    update_user_from_admin(user_id, user_data.rank, user_data.name, user_data.group_number)
    return {"status": "success"}

@app.get("/api/ranks")
async def get_ranks_api(x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(403)
    return get_all_ranks()

@app.post("/api/ranks")
async def create_rank_api(rank_data: RankCreate, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(403)
    add_rank(rank_data.name.strip())
    return {"status": "success"}

@app.delete("/api/ranks/{rank_name}")
async def delete_rank_api(rank_name: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(403)
    delete_rank(rank_name)
    return {"status": "success"}

@app.get("/constructor", response_class=HTMLResponse)
async def get_constructor_page():
    try:
        with open("ai_studio_code (23).html", "r", encoding="utf-8") as f: return HTMLResponse(content=f.read())
    except FileNotFoundError: raise HTTPException(404)

@app.get("/health")
async def health_check(): return Response(status_code=200)

@app.on_event("startup")
async def startup():
    await application.initialize()
    await application.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

@app.on_event("shutdown")
async def shutdown():
    pool.close()
    await application.shutdown()

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
