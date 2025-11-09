import os
import logging
import re
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

from fastapi import FastAPI, Request, HTTPException, Header
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
WEBHOOK_PATH = '/webhook'
DOMAIN = os.getenv('RENDER_EXTERNAL_HOSTNAME')

if not all([BOT_TOKEN, DATABASE_URL, API_KEY, DOMAIN]):
    raise ValueError("Missing environment variables")

WEBHOOK_URL = f"https://{DOMAIN}{WEBHOOK_PATH}"

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

# Validation regex for name
NAME_REGEX = re.compile(r'^[А-ЯЇІЄҐ]\. [А-ЯЇІЄҐ][а-яїієґʼ\-]+$', re.IGNORECASE)

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
    logger.info(f"User {user_id} registered as {registered_name}")

def get_user(user_id: int) -> tuple | None:
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT registered_name, username FROM users WHERE user_id = %s",
            (user_id,),
        )
        result = cur.fetchone()
        logger.info(f"Getting user {user_id}: {result}")
        return result

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
        logger.info(f"Registration added for user {user_id}: {event_type} on {event_date}")
        return True
    except psycopg.errors.UniqueViolation:
        logger.warning(f"Duplicate registration for user {user_id} on {event_date}")
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
        result = cur.fetchall()
        logger.info(f"Getting registrations for user {user_id}: {result}")
        return result

def delete_registration(reg_id: int) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM registrations WHERE id = %s", (reg_id,))
        conn.commit()
    logger.info(f"Registration {reg_id} deleted")

def get_lists_for_date(target_date: date) -> dict:
    with pool.connection() as conn:
        # Встановлюємо, щоб курсор повертав рядки у вигляді словників, а не кортежів
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # === КЛЮЧОВЕ ВИПРАВЛЕННЯ: ===
            # 1. Використовуємо SQL-псевдонім "AS", щоб примусово назвати стовпець "full_name".
            # 2. Тепер результат буде гарантовано мати правильні ключі.
            cur.execute(
                """
                SELECT 
                    r.event_type, 
                    u.registered_name AS full_name, 
                    u.username
                FROM registrations r
                JOIN users u ON r.user_id = u.user_id
                WHERE r.event_date = %s
                ORDER BY r.event_type, u.registered_name
                """,
                (target_date,),
            )
            rows = cur.fetchall()

    lists = {"Звичайне": [], "Добове": []}
    # Тепер ми ітеруємо по словниках, де кожен ключ відповідає назві стовпця
    for row in rows:
        event_type = row['event_type']
        # Ми просто копіюємо весь словник, не турбуючись про окремі поля
        lists[event_type].append({
            "full_name": row['full_name'],
            "username": row['username']
        })

    return {
        "request_date": target_date.isoformat(),
        "total_registrations": len(rows),
        "lists": lists,
    }

# Bot handlers
async def start(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user:
        await show_main_menu(update, context)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            'Вітаю! Для використання бота пройдіть реєстрацію.\n'
            'Введіть ваші ініціали у форматі: І. Прізвище (наприклад, П. Порошенко)',
            reply_markup=ReplyKeyboardRemove(),
        )
        return REG_NAME

async def register_name(update: Update, context: CallbackContext) -> int:
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

async def handle_menu(update: Update, context: CallbackContext) -> int:
    text = update.message.text
    user_id = update.effective_user.id
    
    logger.info(f"User {user_id} selected: {text}")
    
    user = get_user(user_id)
    if not user:
        await update.message.reply_text('Спочатку зареєструйтеся за допомогою /start')
        return ConversationHandler.END

    if text == 'Записатись на звільнення':
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
        
    elif text == 'Мої записи':
        regs = get_user_registrations(user_id)
        if not regs:
            await update.message.reply_text('У вас немає активних записів.')
        else:
            for reg_id, event_type, event_date in regs:
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton('Скасувати запис', callback_data=f'cancel:{reg_id}')]]
                )
                await update.message.reply_text(
                    f'Дата: {event_date.strftime("%Y-%m-%d")}\nТип: {event_type}',
                    reply_markup=keyboard,
                )
        return ConversationHandler.END
    else:
        await update.message.reply_text('Будь ласка, використовуйте кнопки для взаємодії.')
        return ConversationHandler.END

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
    context.user_data.pop('selected_date', None)
    context.user_data.pop('dates', None)
    return ConversationHandler.END

async def cancel_registration(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    reg_id = int(query.data.split(':')[1])
    delete_registration(reg_id)
    await query.edit_message_text('Запис скасовано.')

# FastAPI app
app = FastAPI()

# Telegram bot application
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Registration conversation handler
reg_handler = ConversationHandler(
    entry_points=[CommandHandler('start', start)],
    states={
        REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
    },
    fallbacks=[CommandHandler('start', start)],
)

# Main menu conversation handler
menu_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex('^(Записатись на звільнення|Мої записи)$'), handle_menu)],
    states={
        CHOOSE_DATE: [CallbackQueryHandler(choose_date, pattern='^date:')],
        CHOOSE_TYPE: [CallbackQueryHandler(choose_type, pattern='^type:')],
    },
    fallbacks=[CommandHandler('start', start)],
)

# Add handlers in correct order
application.add_handler(reg_handler)
application.add_handler(menu_handler)
application.add_handler(CallbackQueryHandler(cancel_registration, pattern='^cancel:'))

# Webhook endpoint
@app.post(WEBHOOK_PATH)
async def process_update(request: Request):
    req = await request.json()
    update = Update.de_json(req, application.bot)
    await application.process_update(update)
    return {"ok": True}

# API endpoint
@app.get("/api/lists/{date_str}")
async def get_lists(date_str: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    return get_lists_for_date(target_date)

# Set webhook on startup
@app.on_event("startup")
async def startup():
    await application.initialize()
    await application.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

@app.on_event("shutdown")
async def shutdown():
    await application.shutdown()

# Run app
if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

