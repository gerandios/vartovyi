import os
import logging
import calendar
import re  # <--- ДОДАНО: Необхідно для валідації
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo  # <--- ДОДАНО: Для роботи з Київським часом
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

# --- Настройка таймзони ---
# Це гарантує, що всі перевірки часу (16:00, 17:00) працюють по Києву, а не по Лондону
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
# Захист від помилки, якщо ADMIN_IDS порожній
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
                
                # Дефолтні звання
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
    REG_RANK, REG_SURNAME, REG_FIRSTNAME, REG_GROUP,  # Етапи реєстрації
    MAIN_MENU, 
    CHOOSE_DATE, CHOOSE_TYPE, CHOOSE_DOVOBE_REASON, CHOOSE_DOZVIL_TIME
) = range(9)


# --- БД ФУНКЦІЇ (Синхронні) ---
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
            # concat rank + name
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
    
    # Отримуємо час по Києву!
    now_kyiv = datetime.now(KYIV_TZ)
    today = now_kyiv.date()
    current_hour = now_kyiv.hour
    
    if current_hour < 16:
        min_available_date = today
    else:
        min_available_date = today + timedelta(days=1)
    
    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data='ignore'))
            else:
                current_date = date(year, month, day)
                if current_date < min_available_date:
                    row.append(InlineKeyboardButton(f"~{day}~", callback_data='ignore'))
                else:
                    row.append(InlineKeyboardButton(str(day), callback_data=f'day:{current_date.isoformat()}'))
        keyboard.append(row)
        
    # Навігація
    prev_d = date(year, month, 1) - timedelta(days=1)
    next_d = date(year, month, 1) + timedelta(days=32)
    keyboard.append([
        InlineKeyboardButton("<", callback_data=f'nav:{prev_d.year}:{prev_d.month}'),
        InlineKeyboardButton(">", callback_data=f'nav:{next_d.year}:{next_d.month}')
    ])
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: CallbackContext):
    keyboard = [['Записатись на звільнення', 'Мої записи']]
    await update.message.reply_text('Головне меню:', reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

# --- ЛОГІКА БОТА: СТАРТ ТА РЕЄСТРАЦІЯ ---

async def start_router(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    context.user_data.clear()
    user = get_user(user_id)
    
    if user:
        await update.message.reply_text(
            f"Вітаю, {user['rank']} {user['name']}!\nОберіть дію:",
            reply_markup=ReplyKeyboardMarkup([['Записатись на звільнення', 'Мої записи']], resize_keyboard=True),
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
        if row:
            keyboard.append(row)
            
        await update.message.reply_text(
            "Вітаю! Розпочнемо реєстрацію.\n\n"
            "1️⃣ **Крок 1 з 4:**\n"
            "Оберіть ваше **звання** за допомогою кнопок меню:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
            parse_mode='Markdown'
        )
        return REG_RANK

# 1. Валідація звання
async def register_rank(update: Update, context: CallbackContext) -> int:
    selected_rank = update.message.text.lower()
    available_ranks = [r.lower() for r in get_all_ranks()]
    
    if selected_rank not in available_ranks:
        await update.message.reply_text("⚠️ Будь ласка, оберіть звання, натиснувши на кнопку внизу.")
        return REG_RANK

    context.user_data['rank'] = selected_rank
    
    await update.message.reply_text(
        "✅ Звання прийнято.\n\n"
        "2️⃣ **Крок 2 з 4:**\n"
        "Введіть ваше **ПРІЗВИЩЕ** (лише прізвище).\n"
        "📌 *Приклад:* Шевченко",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode='Markdown'
    )
    return REG_SURNAME

# 2. Валідація прізвища
async def register_surname(update: Update, context: CallbackContext) -> int:
    raw_text = update.message.text.strip()
    
    if len(raw_text) < 2:
        await update.message.reply_text("⚠️ Прізвище занадто коротке. Спробуйте ще раз.")
        return REG_SURNAME

    # Дозволяємо літери, дефіс, апостроф. Забороняємо цифри та спецсимволи.
    if not re.match(r"^[a-zA-Zа-яА-ЯіІїЇєЄґҐ\-\']+$", raw_text):
        await update.message.reply_text("⚠️ Прізвище повинно містити **тільки літери**. Без цифр, смайлів та пробілів.\nСпробуйте ще раз.", parse_mode='Markdown')
        return REG_SURNAME

    context.user_data['surname'] = raw_text.capitalize()
    
    await update.message.reply_text(
        "✅ Прізвище прийнято.\n\n"
        "3️⃣ **Крок 3 з 4:**\n"
        "Введіть ваше **ІМ'Я** або **ІНІЦІАЛИ**.\n"
        "📌 *Приклад:* Тарас або Т.Г.",
        parse_mode='Markdown'
    )
    return REG_FIRSTNAME

# 3. Валідація імені
async def register_firstname(update: Update, context: CallbackContext) -> int:
    raw_text = update.message.text.strip()
    
    if len(raw_text) < 1 or len(raw_text) > 30:
        await update.message.reply_text("⚠️ Некоректна довжина. Введіть нормально (Ім'я або Ініціали).")
        return REG_FIRSTNAME
    
    # Якщо тільки цифри/символи
    if re.match(r"^[\d\s\W]+$", raw_text) and not re.search(r"[a-zA-Zа-яА-Я]", raw_text):
         await update.message.reply_text("⚠️ Ім'я не може складатися тільки з цифр або символів.")
         return REG_FIRSTNAME

    surname = context.user_data['surname']
    full_name = f"{surname} {raw_text.title()}"
    context.user_data['name'] = full_name
    
    await update.message.reply_text(
        "✅ Прийнято.\n\n"
        "4️⃣ **Крок 4 з 4:**\n"
        "Введіть номер вашої **ГРУПИ**.\n"
        "⚠️ **ТІЛЬКИ ЦИФРИ** (наприклад: 311)",
        parse_mode='Markdown'
    )
    return REG_GROUP

# 4. Валідація групи
async def register_group(update: Update, context: CallbackContext) -> int:
    group_number = update.message.text.strip()
    
    if not group_number.isdigit():
        await update.message.reply_text("⛔️ Помилка! Номер групи має складатися **тільки з цифр**.\nВведіть ще раз:", parse_mode='Markdown')
        return REG_GROUP
    
    if len(group_number) > 5:
        await update.message.reply_text("⛔️ Занадто довгий номер групи.")
        return REG_GROUP

    rank = context.user_data['rank']
    name = context.user_data['name']
    
    insert_user(update.effective_user.id, rank, name, update.effective_user.username, group_number)
    
    await update.message.reply_text(
        f'✅ **РЕЄСТРАЦІЮ ЗАВЕРШЕНО!**\n\n'
        f'👤 **Дані:** {rank.capitalize()} {name}\n'
        f'🎓 **Група:** {group_number}\n\n'
        f'Тепер ви можете користуватися меню.',
        parse_mode='Markdown'
    )
    await show_main_menu(update, context)
    context.user_data.clear()
    return MAIN_MENU


# --- ЛОГІКА БОТА: МЕНЮ І ЗАПИС ---

async def handle_menu_choice(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if text == 'Записатись на звільнення':
        now_kyiv = datetime.now(KYIV_TZ) # ВИПРАВЛЕНО ЧАС
        current_hour = now_kyiv.hour
        today = now_kyiv.date()
        tomorrow = today + timedelta(days=1)
        
        keyboard = []
        
        if current_hour < 16:
            keyboard.append([InlineKeyboardButton('На сьогодні', callback_data=f'day:{today.isoformat()}')])
        
        keyboard.append([InlineKeyboardButton('На завтра', callback_data=f'day:{tomorrow.isoformat()}')])
        keyboard.append([InlineKeyboardButton('Обрати іншу дату', callback_data='calendar')])
        
        await update.message.reply_text('Оберіть дату звільнення:', reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_DATE
    
    elif text == 'Мої записи':
        regs = get_user_registrations(update.effective_user.id)
        if not regs:
            await update.message.reply_text('У вас немає активних записів.')
        else:
            await update.message.reply_text("Ваші активні записи:")
            for reg in regs:
                reason_text = f'\n📝 Підстава: {reg["reason"]}' if reg["reason"] else ""
                return_text = f'\n⏰ Повернення: {reg["return_info"]}' if reg["return_info"] else ""
                msg = f'📅 Дата: {reg["event_date"]:%d.%m.%Y}\n📋 Тип: {reg["event_type"]}{reason_text}{return_text}'
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Скасувати запис', callback_data=f'cancel:{reg["id"]}')]])
                await update.message.reply_text(msg, reply_markup=keyboard)
        return MAIN_MENU
    return MAIN_MENU

async def date_callback_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'calendar':
        now = datetime.now(KYIV_TZ)
        await query.edit_message_text("Оберіть дату:", reply_markup=create_calendar(now.year, now.month))
        return CHOOSE_DATE
    elif data.startswith('nav:'):
        _, year, month = data.split(':')
        await query.edit_message_text("Оберіть дату:", reply_markup=create_calendar(int(year), int(month)))
        return CHOOSE_DATE
    elif data.startswith('day:'):
        selected_date = date.fromisoformat(data.split(':')[1])
        
        now = datetime.now(KYIV_TZ) # ВИПРАВЛЕНО
        today = now.date()
        current_hour = now.hour
        
        if selected_date == today and current_hour >= 16:
            await query.edit_message_text("⚠️ Час для запису на сьогодні минув (після 16:00).\nБудь ласка, оберіть іншу дату.")
            return CHOOSE_DATE
        
        context.user_data['selected_date'] = selected_date
        day_of_week = selected_date.weekday()
        text = f"Обрана дата: {selected_date:%d.%m.%Y}. Оберіть тип звільнення:"
        
        # Логіка вихідних
        if 0 <= day_of_week <= 4: # Пн-Пт
            keyboard = [[InlineKeyboardButton('Звичайне (до 21:30)', callback_data='type:Звичайне')],
                        [InlineKeyboardButton('Добове', callback_data='type:Добове')]]
        elif day_of_week == 5: # Субота
            text = f"Обрана дата: {selected_date:%d.%m.%Y} (Субота).\nВихід о 17:00. Оберіть тип:"
            keyboard = [[InlineKeyboardButton('Звичайне (до 21:30)', callback_data='type:Звичайне')],
                        [InlineKeyboardButton('Добове (до 08:30)', callback_data='type:Добове:auto_saturday')]]
        else: # Неділя
            text = f"Обрана дата: {selected_date:%d.%m.%Y} (Неділя).\nВихід о 09:00. Оберіть тип:"
            keyboard = [[InlineKeyboardButton('Звичайне (до 21:30)', callback_data='type:Звичайне')],
                        [InlineKeyboardButton('Добове', callback_data='type:Добове')]]
                        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_TYPE

async def choose_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(':')
    event_type = parts[1]
    context.user_data['event_type'] = event_type
    if event_type == 'Звичайне':
        return await save_registration(update, context, reason=None, return_info="до 21:30")
    if len(parts) > 2 and parts[2] == 'auto_saturday':
        return await save_registration(update, context, reason="рапорт", return_info="до 08:30")
    if event_type == 'Добове':
        keyboard = [[InlineKeyboardButton('Рапорт', callback_data='reason:рапорт')],
                    [InlineKeyboardButton('Маю дозвіл Н.І.', callback_data='reason:дозвіл')]]
        await query.edit_message_text("Вкажіть підставу для добового звільнення:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_DOVOBE_REASON
    return MAIN_MENU

async def choose_dovobe_reason(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    reason_code = query.data.split(':')[1]
    reason_text = "рапорт" if reason_code == "рапорт" else "дозвіл Н.І."
    context.user_data['reason'] = reason_text
    if reason_code == 'рапорт':
        return await save_registration(update, context, reason=reason_text, return_info="до 06:00")
    if reason_code == 'дозвіл':
        keyboard = [[InlineKeyboardButton('До 06:00', callback_data='dozvil_time:06:00')],
                    [InlineKeyboardButton('До 08:00', callback_data='dozvil_time:08:00')]]
        await query.edit_message_text("Вкажіть, до котрої години ви маєте дозвіл:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_DOZVIL_TIME

async def choose_dozvil_time(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    return_time = query.data.split(':')[1]
    return_info = f"до {return_time}"
    return await save_registration(update, context, reason=context.user_data.get('reason'), return_info=return_info)

async def save_registration(update: Update, context: CallbackContext, reason: str | None, return_info: str) -> int:
    user_id = update.effective_user.id
    selected_date = context.user_data.get('selected_date')
    event_type = context.user_data.get('event_type')
    query = update.callback_query
    
    if not all([selected_date, event_type]):
        await query.edit_message_text("❌ Помилка сесії. Почніть знову.")
        context.user_data.clear()
        return MAIN_MENU
        
    insert_registration(user_id, event_type, selected_date, reason, return_info)
    msg = f"✅ Запис оновлено!\n📅 Дата: {selected_date:%d.%m.%Y}\n📋 Тип: {event_type}\n"
    if reason: msg += f"📝 Підстава: {reason}\n"
    msg += f"⏰ Повернення: {return_info}"
    await query.edit_message_text(msg)
    context.user_data.clear()
    return MAIN_MENU

async def cancel(update: Update, context: CallbackContext) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Дію скасовано.")
    elif update.message:
        await update.message.reply_text("Дію скасовано.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    # Повертаємо меню, якщо юзер вже зареєстрований
    if get_user(update.effective_user.id):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Головне меню:",
            reply_markup=ReplyKeyboardMarkup([['Записатись на звільнення', 'Мої записи']], resize_keyboard=True)
        )
        return MAIN_MENU
    return ConversationHandler.END

async def cancel_registration(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    reg_id = int(query.data.split(':')[1])
    delete_registration(reg_id)
    await query.edit_message_text('✅ Запис скасовано.')

async def admin_panel(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS: return
    keyboard = [[InlineKeyboardButton("Видалити всі майбутні записи", callback_data='admin:clear_regs')], [InlineKeyboardButton("⚠️ ОЧИСТИТИ ВСІ ДАНІ ⚠️", callback_data='admin:wipe_all')], [InlineKeyboardButton("Скасувати", callback_data='admin:cancel')]]
    await update.message.reply_text("Панель адміністратора:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    action = query.data.split(':')[1]
    if action == 'clear_regs':
        count = clear_future_registrations()
        await query.edit_message_text(f"✅ Видалено {count} записів.")
    elif action == 'wipe_all':
        wipe_all_data()
        await query.edit_message_text("✅🔴 БАЗА ДАНИХ ОЧИЩЕНА.")
    elif action == 'cancel':
        await query.edit_message_text("Скасовано.")

async def ignore_callback(update: Update, context: CallbackContext):
    if update.callback_query: await update.callback_query.answer()

# --- FastAPI & Application ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

application = ApplicationBuilder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler('start', start_router),
        MessageHandler(filters.TEXT & ~filters.COMMAND, start_router)
    ],
    states={
        REG_RANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_rank)],
        REG_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname)],
        REG_FIRSTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_firstname)],
        REG_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_group)],
        
        MAIN_MENU: [
            MessageHandler(filters.Regex('^Записатись на звільнення$'), handle_menu_choice),
            MessageHandler(filters.Regex('^Мої записи$'), handle_menu_choice),
        ],
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

# --- API Routes ---
class UserUpdate(BaseModel):
    rank: str
    name: str
    group_number: str

class RankCreate(BaseModel):
    name: str

@app.post(WEBHOOK_PATH)
async def process_update(request: Request):
    update_data = await request.json()
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.get("/api/lists/{date_str}")
async def get_lists_api(date_str: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(status_code=403, detail="Forbidden")
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")
    return get_lists_for_date(target_date)

@app.get("/api/users")
async def get_users_list_api(x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(status_code=403, detail="Forbidden")
    return get_all_users()

@app.put("/api/users/{user_id}")
async def update_user_api(user_id: int, user_data: UserUpdate, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(status_code=403, detail="Forbidden")
    update_user_from_admin(user_id, user_data.rank, user_data.name, user_data.group_number)
    return {"status": "success"}

@app.get("/api/ranks")
async def get_ranks_api(x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(status_code=403, detail="Forbidden")
    return get_all_ranks()

@app.post("/api/ranks")
async def create_rank_api(rank_data: RankCreate, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(status_code=403, detail="Forbidden")
    if len(rank_data.name.strip()) < 2: raise HTTPException(status_code=400)
    add_rank(rank_data.name.strip())
    return {"status": "success"}

@app.delete("/api/ranks/{rank_name}")
async def delete_rank_api(rank_name: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(status_code=403, detail="Forbidden")
    delete_rank(rank_name)
    return {"status": "success"}

@app.get("/constructor", response_class=HTMLResponse)
async def get_constructor_page():
    try:
        with open("ai_studio_code (23).html", "r", encoding="utf-8") as f: return HTMLResponse(content=f.read())
    except FileNotFoundError: raise HTTPException(status_code=404)

@app.get("/health")
async def health_check(): return Response(status_code=200)

# Старт/Стоп
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
    PORT = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
