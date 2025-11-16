import os
import logging
import calendar
from datetime import datetime, date, timedelta, timezone

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

from fastapi import FastAPI, Request, HTTPException, Header
import uvicorn

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Переменные окружения ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
API_KEY = os.getenv('API_KEY')
WEBHOOK_PATH = '/webhook'
DOMAIN = os.getenv('RENDER_EXTERNAL_HOSTNAME')
ADMIN_IDS_STR = os.getenv('ADMIN_IDS', '')
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]

if not all([BOT_TOKEN, DATABASE_URL, API_KEY, DOMAIN, ADMIN_IDS]):
    raise ValueError("Отсутствуют переменные окружения (BOT_TOKEN, DATABASE_URL, API_KEY, DOMAIN, ADMIN_IDS)")

WEBHOOK_URL = f"https://{DOMAIN}{WEBHOOK_PATH}"

# --- Пул соединений с базой данных ---
pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10)

# --- Создание таблиц в БД, если их нет ---
with pool.connection() as conn:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        registered_name VARCHAR NOT NULL,
        username VARCHAR,
        group_number VARCHAR,
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

# --- Единый набор состояний для ConversationHandler ---
(
    REG_NAME, REG_GROUP,                 # Состояния регистрации
    MAIN_MENU, CHOOSE_DATE, CHOOSE_TYPE, # Состояния основного меню и записи
    EDIT_GET_ID, EDIT_CHOOSE_FIELD, EDIT_GET_NEW_VALUE # Состояния админ. редактирования
) = range(8)

# --- Функции для работы с БД ---
def insert_user(user_id: int, registered_name: str, username: str | None, group_number: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, registered_name, username, group_number, registration_date)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                registered_name = EXCLUDED.registered_name,
                username = EXCLUDED.username,
                group_number = EXCLUDED.group_number;
            """,
            (user_id, registered_name, username, group_number, datetime.now(timezone.utc)),
        )

def get_user(user_id: int) -> dict | None:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()

def update_user_field(user_id: int, field: str, value: str) -> None:
    if field not in ['registered_name', 'group_number']: raise ValueError("Invalid field")
    with pool.connection() as conn:
        query = psycopg.sql.SQL("UPDATE users SET {field} = %s WHERE user_id = %s").format(
            field=psycopg.sql.Identifier(field)
        )
        conn.execute(query, (value, user_id))

def insert_registration(user_id: int, event_type: str, event_date: date) -> bool:
    if event_type not in ('Звичайне', 'Добове'):
        raise ValueError('Invalid event_type')
    try:
        with pool.connection() as conn:
            conn.execute("INSERT INTO registrations (user_id, event_type, event_date) VALUES (%s, %s, %s)", (user_id, event_type, event_date))
        return True
    except psycopg.errors.UniqueViolation:
        return False

def get_user_registrations(user_id: int) -> list:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM registrations WHERE user_id = %s AND event_date >= %s ORDER BY event_date ASC", (user_id, date.today()))
            return cur.fetchall()

def delete_registration(reg_id: int) -> None:
    with pool.connection() as conn: conn.execute("DELETE FROM registrations WHERE id = %s", (reg_id,))

def get_lists_for_date(target_date: date) -> dict:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT r.event_type, u.registered_name AS full_name, u.username, u.group_number
                FROM registrations r JOIN users u ON r.user_id = u.user_id
                WHERE r.event_date = %s ORDER BY u.group_number, u.registered_name
                """, (target_date,))
            rows = cur.fetchall()
    lists = {"Звичайне": [], "Добове": []}
    for row in rows: lists[row['event_type']].append(row)
    return {"request_date": target_date.isoformat(), "total_registrations": len(rows), "lists": lists}

def clear_future_registrations() -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM registrations WHERE event_date >= %s", (date.today(),))
            deleted_rows = cur.rowcount
    logger.info(f"Admin cleared {deleted_rows} future registrations.")
    return deleted_rows

def wipe_all_data() -> None:
    with pool.connection() as conn:
        conn.execute("TRUNCATE TABLE registrations, users RESTART IDENTITY;")
    logger.warning("Admin WIPED ALL DATA from users and registrations tables.")


# --- Вспомогательные функции для бота ---
def create_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    keyboard = []
    header = f"{calendar.month_name[month]} {year}"
    keyboard.append([InlineKeyboardButton(header, callback_data='ignore')])
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    keyboard.append([InlineKeyboardButton(day, callback_data='ignore') for day in days])
    
    month_calendar = calendar.monthcalendar(year, month)
    tomorrow = date.today() + timedelta(days=1)

    for week in month_calendar:
        row = []
        for day in week:
            if day == 0: row.append(InlineKeyboardButton(" ", callback_data='ignore'))
            else:
                current_date = date(year, month, day)
                if current_date < tomorrow: row.append(InlineKeyboardButton(f"~{day}~", callback_data='ignore'))
                else: row.append(InlineKeyboardButton(str(day), callback_data=f'day:{current_date.isoformat()}'))
        keyboard.append(row)
        
    prev_month_date = date(year, month, 1) - timedelta(days=1)
    next_month_date = date(year, month, 1) + timedelta(days=32)
    nav_row = [
        InlineKeyboardButton("<", callback_data=f'nav:{prev_month_date.year}:{prev_month_date.month}'),
        InlineKeyboardButton(">", callback_data=f'nav:{next_month_date.year}:{next_month_date.month}')
    ]
    keyboard.append(nav_row)
    return InlineKeyboardMarkup(keyboard)

async def safe_reply(update, context, text, reply_markup=None, edit=False):
    """Универсальная функция ответа, работает и для сообщений, и для колбэков."""
    if update.callback_query:
        try: await update.callback_query.answer()
        except Exception: pass
        if edit:
            try:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
                return
            except Exception: pass
        if update.callback_query.message:
            await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)

async def show_main_menu(update: Update, context: CallbackContext):
    """Показывает главное меню с кнопками."""
    keyboard = [['Записатись на звільнення', 'Мої записи']]
    await update.message.reply_text(
        'Головне меню:',
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

# --- Обработчики состояний ---

async def start_router(update: Update, context: CallbackContext) -> int:
    """Главная точка входа. Проверяет, зарегистрирован ли юзер, и направляет его."""
    user_id = update.effective_user.id
    context.user_data.clear()
    user = get_user(user_id)

    if user:
        await update.message.reply_text(
            f"Вітаю, {user['registered_name']}!\nОберіть дію:",
            reply_markup=ReplyKeyboardMarkup([['Записатись на звільнення', 'Мої записи']], resize_keyboard=True),
        )
        return MAIN_MENU
    else:
        await update.message.reply_text(
            """Вітаю! Для використання бота пройдіть реєстрацію.
Введіть ваше звання та прізвище з ініціалами (наприклад, ст. солдат К.Пижко)""",
            reply_markup=ReplyKeyboardRemove(),
        )
        return REG_NAME

async def register_name(update: Update, context: CallbackContext) -> int:
    context.user_data['registered_name'] = update.message.text.strip()
    await update.message.reply_text('Дякую! Тепер введіть номер вашої навчальної групи (наприклад, 311).')
    return REG_GROUP

async def register_group(update: Update, context: CallbackContext) -> int:
    group_number = update.message.text.strip()
    if not group_number.isdigit():
        await update.message.reply_text("Невірний формат. Номер групи має складатися лише з цифр. Спробуйте ще раз.")
        return REG_GROUP
    
    registered_name = context.user_data['registered_name']
    insert_user(update.effective_user.id, registered_name, update.effective_user.username, group_number)
    
    await update.message.reply_text(f'Реєстрацію завершено! Ви зареєстровані як {registered_name}, група {group_number}.')
    await show_main_menu(update, context)
    context.user_data.clear()
    return MAIN_MENU # Переходим в состояние главного меню

async def handle_menu_choice(update: Update, context: CallbackContext) -> int:
    """Обрабатывает нажатия кнопок в главном меню."""
    text = update.message.text.strip()
    if text == 'Записатись на звільнення':
        keyboard = [
            [InlineKeyboardButton('На завтра', callback_data=f'day:{(date.today() + timedelta(days=1)).isoformat()}')],
            [InlineKeyboardButton('Обрати іншу дату', callback_data='calendar')]
        ]
        await update.message.reply_text('Оберіть дату звільнення:', reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_DATE
    
    elif text == 'Мої записи':
        regs = get_user_registrations(update.effective_user.id)
        if not regs:
            await update.message.reply_text('У вас немає активних записів.')
        else:
            message_text = "Ваші активні записи:\n"
            for reg in regs:
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Скасувати запис', callback_data=f'cancel:{reg["id"]}')]])
                await update.message.reply_text(
                    f'📅 Дата: {reg["event_date"]:%d.%m.%Y}\n📋 Тип: {reg["event_type"]}',
                    reply_markup=keyboard
                )
        return MAIN_MENU # Остаемся в главном меню

async def date_callback_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == 'calendar':
        now = datetime.now()
        await query.edit_message_text("Оберіть дату:", reply_markup=create_calendar(now.year, now.month))
        return CHOOSE_DATE
    elif data.startswith('nav:'):
        _, year, month = data.split(':')
        await query.edit_message_text("Оберіть дату:", reply_markup=create_calendar(int(year), int(month)))
        return CHOOSE_DATE
    elif data.startswith('day:'):
        context.user_data['selected_date'] = date.fromisoformat(data.split(':')[1])
        keyboard = [
            [InlineKeyboardButton('Звичайне', callback_data='type:Звичайне')],
            [InlineKeyboardButton('Добове', callback_data='type:Добове')]
        ]
        await query.edit_message_text('Оберіть тип звільнення:', reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSE_TYPE

async def choose_type(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    event_type = query.data.split(':')[1]
    selected_date = context.user_data.get('selected_date')
    
    if not selected_date:
        await query.edit_message_text("Вибачте, сесія застаріла. Почніть знову з головного меню.")
        return MAIN_MENU
    
    success = insert_registration(update.effective_user.id, event_type, selected_date)
    msg = f'✅ Ви успішно записалися на {event_type} звільнення на {selected_date:%d.%m.%Y}.' if success else '⚠️ Ви вже записані на цю дату.'
    await query.edit_message_text(msg)
    
    context.user_data.clear()
    # После завершения действия неявно возвращаемся в главное меню (пользователь может нажать кнопку)
    return MAIN_MENU

async def edit_start(update: Update, context: CallbackContext) -> int:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("У вас немає прав для виконання цієї команди.")
        return ConversationHandler.END
    await update.message.reply_text("Введіть Telegram ID користувача, дані якого потрібно змінити.")
    return EDIT_GET_ID

async def edit_get_id(update: Update, context: CallbackContext) -> int:
    try:
        target_id = int(update.message.text)
    except ValueError:
        await update.message.reply_text("ID має бути числом. Спробуйте ще раз.")
        return EDIT_GET_ID
    
    user_data = get_user(target_id)
    if not user_data:
        await update.message.reply_text("Користувача з таким ID не знайдено.")
        await show_main_menu(update, context) # Возвращаем в меню
        return MAIN_MENU if get_user(update.effective_user.id) else ConversationHandler.END

    context.user_data['edit_user_id'] = target_id
    text = f"Дані користувача:\nІм'я: {user_data['registered_name']}\nГрупа: {user_data['group_number']}\n\nЩо бажаєте змінити?"
    keyboard = [
        [InlineKeyboardButton("Ім'я", callback_data='edit_field:registered_name')],
        [InlineKeyboardButton("Групу", callback_data='edit_field:group_number')]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return EDIT_CHOOSE_FIELD

async def edit_choose_field(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(':')[1]
    context.user_data['edit_field'] = field
    field_name_map = {'registered_name': "нове ім'я", 'group_number': 'новий номер групи'}
    await query.edit_message_text(f"Введіть {field_name_map[field]}:")
    return EDIT_GET_NEW_VALUE

async def edit_get_new_value(update: Update, context: CallbackContext) -> int:
    user_id = context.user_data.get('edit_user_id')
    field = context.user_data.get('edit_field')
    new_value = update.message.text.strip()
    
    if not user_id or not field:
        await update.message.reply_text("Сесія редагування втрачена. Почніть знову.")
        context.user_data.clear()
        await show_main_menu(update, context)
        return MAIN_MENU

    update_user_field(user_id, field, new_value)
    await update.message.reply_text(f"✅ Дані для користувача {user_id} успішно оновлено.")
    context.user_data.clear()
    await show_main_menu(update, context)
    return MAIN_MENU

async def cancel(update: Update, context: CallbackContext) -> int:
    """Отменяет текущий диалог и возвращает в главное меню."""
    await update.message.reply_text("Дію скасовано.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    # Возвращаем пользователя в соответствующее начальное состояние
    if get_user(update.effective_user.id):
        await show_main_menu(update, context)
        return MAIN_MENU
    return ConversationHandler.END


# --- Обработчики вне диалога ---

async def cancel_registration(update: Update, context: CallbackContext):
    """Удаляет конкретную запись по ID."""
    query = update.callback_query
    await query.answer()
    reg_id = int(query.data.split(':')[1])
    delete_registration(reg_id)
    await query.edit_message_text('✅ Запис скасовано.')

async def admin_panel(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("У вас немає прав для виконання цієї команди.")
        return
    keyboard = [
        [InlineKeyboardButton("Видалити всі майбутні записи", callback_data='admin:clear_regs')],
        [InlineKeyboardButton("⚠️ ОЧИСТИТИ ВСІ ДАНІ ⚠️", callback_data='admin:wipe_all')],
        [InlineKeyboardButton("Скасувати", callback_data='admin:cancel')]
    ]
    await update.message.reply_text("Панель адміністратора:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    action = query.data.split(':')[1]

    if action == 'clear_regs':
        count = clear_future_registrations()
        await query.edit_message_text(f"✅ Усі майбутні записи ({count} шт.) видалено.")
    elif action == 'wipe_all':
        wipe_all_data()
        await query.edit_message_text("✅🔴 УСІ дані (користувачі та записи) було повністю видалено з бази даних.")
    elif action == 'cancel':
        await query.edit_message_text("Дію скасовано.")

async def ignore_callback(update: Update, context: CallbackContext):
    """Отвечает на 'пустые' колбэки, чтобы убрать индикатор загрузки."""
    if update.callback_query:
        await update.callback_query.answer()


# --- Настройка FastAPI и вебхука ---
app = FastAPI()
application = ApplicationBuilder().token(BOT_TOKEN).build()

# --- ЕДИНЫЙ ОБРАБОТЧИК ДИАЛОГОВ ---
conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler('start', start_router),
        CommandHandler('edit', edit_start)
    ],
    states={
        # Флоу регистрации
        REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
        REG_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_group)],
        
        # Флоу главного меню
        MAIN_MENU: [
            MessageHandler(filters.Regex('^Записатись на звільнення$'), handle_menu_choice),
            MessageHandler(filters.Regex('^Мої записи$'), handle_menu_choice),
        ],
        CHOOSE_DATE: [CallbackQueryHandler(date_callback_handler, pattern='^(day:|nav:|calendar)')],
        CHOOSE_TYPE: [CallbackQueryHandler(choose_type, pattern='^type:')],

        # Флоу админского редактирования
        EDIT_GET_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_id)],
        EDIT_CHOOSE_FIELD: [CallbackQueryHandler(edit_choose_field, pattern='^edit_field:')],
        EDIT_GET_NEW_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_get_new_value)],
    },
    fallbacks=[CommandHandler('cancel', cancel), CommandHandler('start', start_router)],
)

# --- РЕГИСТРАЦИЯ ВСЕХ ОБРАБОТЧИКОВ ---
application.add_handler(conv_handler)
application.add_handler(CallbackQueryHandler(cancel_registration, pattern='^cancel:'))
application.add_handler(CommandHandler('admin', admin_panel))
application.add_handler(CallbackQueryHandler(admin_panel_callback, pattern='^admin:'))
application.add_handler(CallbackQueryHandler(ignore_callback, pattern='^ignore$'))


@app.post(WEBHOOK_PATH)
async def process_update(request: Request):
    update_data = await request.json()
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.get("/api/lists/{date_str}")
async def get_lists_api(date_str: str, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")
    return get_lists_for_date(target_date)

@app.on_event("startup")
async def startup():
    await application.initialize()
    await application.bot.set_webhook(url=WEBHOOK_URL)

@app.on_event("shutdown")
async def shutdown():
    await application.shutdown()
@app.get("/ping")
async def keep_alive():
    # Просто легкий ответ, чтобы Render понял, что мы живы
    return {"status": "I am alive"}
if __name__ == '__main__':
    # Эта часть для локального запуска, на Render она не будет выполняться
    PORT = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=PORT)
