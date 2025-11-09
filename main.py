import os
import logging
import re
import threading
from datetime import datetime, date, timedelta

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

from fastapi import FastAPI, HTTPException, Header
import uvicorn

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
API_KEY = os.getenv('API_KEY')
print("BOT_TOKEN:", os.getenv('BOT_TOKEN'))
print("DATABASE_URL:", os.getenv('DATABASE_URL'))
print("API_KEY:", os.getenv('API_KEY'))

if not all([BOT_TOKEN, DATABASE_URL, API_KEY]):
    raise ValueError("Missing environment variables")

# Database connection pool
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10)

# Create tables if not exist
with pool.connection() as conn:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        registered_name VARCHAR NOT NULL,
        username VARCHAR,
        registration_date TIMESTAMP WITH TIME ZONE NOT NULL
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS registrations (
        id SERIAL PRIMARY KEY,
        user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
        event_type VARCHAR NOT NULL,
        event_date DATE NOT NULL,
        UNIQUE (user_id, event_date)
    );
    """)
    conn.commit()

# States for ConversationHandler
REG_NAME = 0
CHOOSE_DATE, CHOOSE_TYPE = range(2)

# Validation regex for name: І. Прізвище (Ukrainian letters allowed)
NAME_REGEX = re.compile(r'^[А-ЯЇІЄҐ]\. [А-ЯЇІЄҐ][а-я їієґʼ\-]+$', re.IGNORECASE)

# Helper functions for DB
def insert_user(user_id: int, registered_name: str, username: str | None) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, registered_name, username, registration_date)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, registered_name, username, datetime.utcnow()),
        )
        conn.commit()

def get_user(user_id: int) -> tuple | None:
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT registered_name, username FROM users WHERE user_id = %s",
            (user_id,),
        )
        return cur.fetchone()

def insert_registration(user_id: int, event_type: str, event_date: date) -> bool:
    try:
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO registrations (user_id, event_type, event_date)
                VALUES (%s, %s, %s)
                """,
                (user_id, event_type, event_date),
            )
            conn.commit()
        return True
    except psycopg.errors.UniqueViolation:
        return False

def get_user_registrations(user_id: int) -> list:
    today = date.today()
    with pool.connection() as conn:
        cur = conn.execute(
            """
            SELECT id, event_type, event_date
            FROM registrations
            WHERE user_id = %s AND event_date >= %s
            ORDER BY event_date ASC
            """,
            (user_id, today),
        )
        return cur.fetchall()

def delete_registration(reg_id: int) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM registrations WHERE id = %s", (reg_id,))
        conn.commit()

def get_lists_for_date(target_date: date) -> dict:
    with pool.connection() as conn:
        cur = conn.execute(
            """
            SELECT r.event_type, u.registered_name, u.username
            FROM registrations r
            JOIN users u ON r.user_id = u.user_id
            WHERE r.event_date = %s
            ORDER BY r.event_type, u.registered_name
            """,
            (target_date,),
        )
        rows = cur.fetchall()

    lists = {"Звичайне": [], "Добове": []}
    for event_type, full_name, username in rows:
        lists[event_type].append({"full_name": full_name, "username": username})

    return {
        "request_date": target_date.isoformat(),
        "total_registrations": len(rows),
        "lists": lists,
    }

# Bot handlers
async def start(update: Update, context: CallbackContext) -> int | None:
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user:
        await show_main_menu(update, context)
        return None
    else:
        await update.message.reply_text(
            'Вітаю! Для використання бота пройдіть реєстрацію.\n'
            'Введіть ваші ініціали у форматі: І. Прізвище (наприклад, П. Порошенко)',
            reply_markup=ReplyKeyboardRemove(),
        )
        return REG_NAME

async def register_name(update: Update, context: CallbackContext) -> int | None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if NAME_REGEX.match(text):
        username = update.effective_user.username
        insert_user(user_id, text, username)
        await update.message.reply_text(f'Реєстрацію завершено! Ви зареєстровані як {text}.')
        await show_main_menu(update, context)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            'Невірний формат. Введіть у строгому форматі: І. Прізвище (наприклад, П. Порошенко).\n'
            'Спробуйте ще раз.'
        )
        return REG_NAME

async def show_main_menu(update: Update, context: CallbackContext) -> None:
    keyboard = [['Записатись на звільнення', 'Мої записи']]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text('Головне меню:', reply_markup=reply_markup)

async def handle_text(update: Update, context: CallbackContext) -> None:
    text = update.message.text
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await start(update, context)
        return

    if text == 'Записатись на звільнення':
        await start_registration(update, context)
    elif text == 'Мої записи':
        await show_registrations(update, context)
    else:
        await update.message.reply_text('Будь ласка, використовуйте кнопки для взаємодії.')

async def ignore_non_text(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text('Будь ласка, використовуйте кнопки для взаємодії. Або /start для меню.')

async def start_registration(update: Update, context: CallbackContext) -> int:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    context.user_data['dates'] = {
        'Сьогодні': today,
        'Завтра': tomorrow,
    }
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton('Сьогодні', callback_data='date:Сьогодні')],
         [InlineKeyboardButton('Завтра', callback_data='date:Завтра')]]
    )
    await update.message.reply_text('Оберіть дату:', reply_markup=keyboard)
    return CHOOSE_DATE

async def choose_date(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    date_str = query.data.split(':')[1]
    context.user_data['selected_date'] = context.user_data['dates'][date_str]
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton('Звичайне', callback_data='type:Звичайне')],
         [InlineKeyboardButton('Добове', callback_data='type:Добове')]]
    )
    await query.edit_message_text('Оберіть тип звільнення:', reply_markup=keyboard)
    return CHOOSE_TYPE

async def choose_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    event_type = query.data.split(':')[1]
    user_id = update.effective_user.id
    event_date = context.user_data['selected_date']
    success = insert_registration(user_id, event_type, event_date)
    if success:
        msg = f'Ви успішно записалися на {event_type} звільнення на {event_date.strftime("%Y-%m-%d")}.'
    else:
        msg = 'Ви вже записані на цю дату. Дублювання не дозволено.'
    await query.edit_message_text(msg)
    del context.user_data['selected_date']
    del context.user_data['dates']
    return ConversationHandler.END

async def show_registrations(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    regs = get_user_registrations(user_id)
    if not regs:
        await update.message.reply_text('У вас немає активних записів.')
        return

    for reg_id, event_type, event_date in regs:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton('Скасувати запис', callback_data=f'cancel:{reg_id}')]]
        )
        await update.message.reply_text(
            f'Дата: {event_date.strftime("%Y-%m-%d")}\nТип: {event_type}',
            reply_markup=keyboard,
        )

async def cancel_registration(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    reg_id = int(query.data.split(':')[1])
    delete_registration(reg_id)
    await query.edit_message_text('Запис скасовано.')

# Setup bot
def setup_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    reg_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
        },
        fallbacks=[CommandHandler('start', start)],
    )
    application.add_handler(reg_handler)

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
        states={
            CHOOSE_DATE: [CallbackQueryHandler(choose_date, pattern='^date:')],
            CHOOSE_TYPE: [CallbackQueryHandler(choose_type, pattern='^type:')],
        },
        fallbacks=[CommandHandler('start', start)],
        map_to_parent={
            ConversationHandler.END: ConversationHandler.END,
        },
    )
    application.add_handler(conv_handler)

    application.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, ignore_non_text))
    application.add_handler(CallbackQueryHandler(cancel_registration, pattern='^cancel:'))

    application.run_polling()

# FastAPI app
app = FastAPI()

@app.get("/api/lists/{date_str}")
async def get_lists(date_str: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    return get_lists_for_date(target_date)

# Run API and bot
if __name__ == '__main__':
    threading.Thread(target=setup_bot, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)

