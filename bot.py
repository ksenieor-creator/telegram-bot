# coding: utf-8
"""Telegram-бот для расчёта стоимости выездов сварщиков.
Полная версия: пользовательские меню + админ-панель (интерактивное добавление/удаление выездов).
Интеграция всех функций админ-панели в интерактивное меню.
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta, date
import pytz
import holidays
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# ====== НАСТРОЙКИ ======
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN env var is required")
ADMIN_ID = 1006274417
DATA_FILE = "data.json"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
RU_HOLIDAYS = holidays.Russia()

# Таймаут неактивного расчёта (минуты)
CALC_TIMEOUT_MINUTES = 15

# Состояния для ConversationHandler админа
(SELECT_CUSTOMER, SELECT_ACTION, SELECT_DATE, SELECT_KIND, SELECT_DURATION, 
 SELECT_TARIFF_TYPE, CONFIRM_VISIT, CREATE_CUSTOMER, FIND_CUSTOMER, 
 LINK_USER, UNLINK_USER, ADD_SUM, SET_SUM) = range(13)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ====== ТАРИФЫ ======
TARIFFS_DISCOUNT = {
    "free": {"4": 20000, "8": 23000, "night_4": 27000, "night_8": 30000},
    "exact": {"4": 22000, "8": 25000, "night_4": 27000, "night_8": 30000},
    "urgent_tomorrow": {"4": 25000, "8": 27000, "night_4": 27000, "night_8": 30000},
    "urgent_today": {"4": 27000, "8": 30000, "night_4": 27000, "night_8": 30000},
    "holiday": {"4": 35000, "8": 35000, "night_4": 35000, "night_8": 35000},
}
TARIFFS_STANDARD = {
    "free": {"4": 22000, "8": 25000, "night_4": 35000, "night_8": 40000},
    "exact": {"4": 25000, "8": 30000, "night_4": 35000, "night_8": 40000},
    "urgent_tomorrow": {"4": 30000, "8": 35000, "night_4": 35000, "night_8": 40000},
    "urgent_today": {"4": 35000, "8": 40000, "night_4": 35000, "night_8": 40000},
    "holiday": {"4": 40000, "8": 45000, "night_4": 40000, "night_8": 45000},
}

# ====== ХРАНЕНИЕ ======
data = {"customers": {}, "last_reset": None}

# ====== УТИЛИТЫ ======
def now_msk() -> datetime:
    return datetime.now(MOSCOW_TZ)

def load_data():
    global data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cleanup_data()
        except Exception:
            logging.exception("Ошибка чтения data.json")

def cleanup_data():
    customers_to_remove = []
    for cid, cust in list(data["customers"].items()):
        if not cust.get("ids", []) and cid.isdigit():
            customers_to_remove.append(cid)
    for cid in customers_to_remove:
        del data["customers"][cid]
        logging.info(f"Удален некорректный клиент: {cid}")
    if customers_to_remove:
        save_data()

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("Ошибка записи data.json")

def generate_customer_id():
    """Генерирует уникальный ID для заказчика"""
    if not data["customers"]:
        return "1"
    max_id = max(int(cid) for cid in data["customers"].keys() if cid.isdigit())
    return str(max_id + 1)

def ensure_customer(name):
    """Создает заказчика с автоматическим ID"""
    cid = generate_customer_id()
    if cid not in data["customers"]:
        data["customers"][cid] = {
            "name": name,
            "ids": [],
            "projects_sum": 0,
            "discount": False,
            "visits": []
        }
    return cid

def find_customer_by_userid(uid: int):
    for cid, cust in data["customers"].items():
        if str(uid) in cust.get("ids", []):
            return cid, cust
    return None, None

def recalc_discount(cust):
    total_visits = len(cust.get("visits", []))
    if total_visits >= 4 or cust.get("projects_sum", 0) >= 60000:
        cust["discount"] = True
    else:
        cust["discount"] = False

def is_holiday(date_obj: date) -> bool:
    return date_obj in RU_HOLIDAYS

def classify_kind(selected_date: date) -> str:
    today = now_msk().date()
    now = now_msk()
    if is_holiday(selected_date):
        return "holiday"
    if selected_date == today:
        return "urgent_today"
    if selected_date == today + timedelta(days=1) and now.hour >= 17:
        return "urgent_tomorrow"
    return "exact"

def calc_price(kind: str, duration: str, discount: bool):
    prices = TARIFFS_DISCOUNT if discount else TARIFFS_STANDARD
    return prices[kind][duration]

def fmt_rub(n):
    return f"{int(n):,} ₽".replace(",", " ")

def format_visit_short(visit, index):
    """Краткое описание выезда для кнопки"""
    date_str = datetime.strptime(visit["date"], "%Y-%m-%d").strftime("%d.%m") if visit["date"] != "free" else "Своб."
    kind_icons = {
        "exact": "📅",
        "urgent_tomorrow": "⏰",
        "urgent_today": "⏰",
        "holiday": "🎉",
        "free": "🆓"
    }
    duration_icons = {
        "4": "4☀",
        "8": "8☀", 
        "night_4": "4🌙",
        "night_8": "8🌙"
    }
    icon = kind_icons.get(visit["kind"], "📌")
    duration_icon = duration_icons.get(visit["duration"], visit["duration"])
    return f"{index}. {date_str} {icon} {duration_icon} {fmt_rub(visit['price'])}"

# ====== ФУНКЦИЯ ДЛЯ УДАЛЕНИЯ СООБЩЕНИЙ С РАСЧЁТАМИ ======
async def delete_calculation_messages(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int = None):
    """Удаляет сообщения с расчётами пользователя"""
    try:
        if chat_id is None:
            chat_id = user_id
        
        # Получаем ID сообщений, которые нужно удалить
        messages_to_delete = []
        
        # Используем user_data для хранения ID последних сообщений с расчётами
        if 'last_calc_message_ids' in context.user_data:
            for msg_id in context.user_data['last_calc_message_ids']:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as e:
                    # Сообщение может быть уже удалено или недоступно
                    logging.debug(f"Не удалось удалить сообщение {msg_id}: {e}")
            
            # Очищаем список
            context.user_data['last_calc_message_ids'] = []
        
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")

def add_message_to_tracking(context: ContextTypes.DEFAULT_TYPE, message_id: int, max_tracked: int = 10):
    """Добавляет ID сообщения для отслеживания"""
    if 'last_calc_message_ids' not in context.user_data:
        context.user_data['last_calc_message_ids'] = []
    
    # Добавляем новый ID
    context.user_data['last_calc_message_ids'].append(message_id)
    
    # Ограничиваем количество отслеживаемых сообщений
    if len(context.user_data['last_calc_message_ids']) > max_tracked:
        # Оставляем только последние N сообщений
        context.user_data['last_calc_message_ids'] = context.user_data['last_calc_message_ids'][-max_tracked:]



# ====== СЛУЖЕБНОЕ СООБЩЕНИЕ ДЛЯ НЕПРИВЯЗАННЫХ ПОЛЬЗОВАТЕЛЕЙ ======
def _pending_welcome_store(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Хранилище message_id служебных сообщений 'Скоро будет предоставлен доступ...'.
    Ключ: user_id (int), значение: список message_id (list[int]).
    """
    return context.application.bot_data.setdefault("pending_welcome_msgs", {})

def add_pending_welcome_message(context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int, max_tracked: int = 10) -> None:
    store = _pending_welcome_store(context)
    ids = store.get(user_id)
    if not isinstance(ids, list):
        ids = []
    ids.append(int(message_id))
    # оставляем последние max_tracked
    if len(ids) > max_tracked:
        ids = ids[-max_tracked:]
    store[user_id] = ids

async def delete_pending_welcome_messages(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Удаляет ВСЕ служебные сообщения 'Скоро будет предоставлен доступ...' у пользователя."""
    try:
        store = _pending_welcome_store(context)
        ids = store.pop(user_id, None)
        if not ids:
            return
        if not isinstance(ids, list):
            ids = [ids]
        for mid in ids:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=int(mid))
            except Exception as e:
                logging.debug(f"Не удалось удалить служебное сообщение {mid} у {user_id}: {e}")
    except Exception as e:
        logging.error(f"Ошибка при удалении служебных сообщений у {user_id}: {e}")

# ====== СЕССИЯ РАСЧЁТА (1 активный расчёт + автосброс) ======
def _calc_store(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Глобальное хранилище активных расчётов (на уровне приложения)."""
    return context.application.bot_data.setdefault("calc_store", {})

def _calc_job_name(user_id: int) -> str:
    return f"calc_timeout_{user_id}"

def cancel_calc_timeout(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Отменяет запланированный автосброс расчёта для пользователя."""
    try:
        jq = context.application.job_queue
        for job in jq.get_jobs_by_name(_calc_job_name(user_id)):
            job.schedule_removal()
    except Exception:
        logging.exception("Ошибка отмены таймера расчёта")

def reset_calc_session(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Полностью сбрасывает активный расчёт пользователя (сессию + таймер)."""
    cancel_calc_timeout(context, user_id)
    store = _calc_store(context)
    store.pop(user_id, None)

async def _calc_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: сбросить расчёт по таймауту."""
    data = context.job.data or {}
    user_id = data.get("user_id")
    if not user_id:
        return

    store = _calc_store(context)
    # Если расчёта уже нет — ничего не делаем
    if user_id not in store:
        return

    # Сбрасываем
    store.pop(user_id, None)

    try:
        # Удаляем служебное сообщение "Скоро будет предоставлен доступ..." (если оно было)
        try:
            await delete_pending_welcome_messages(context, user_id)
        except Exception as e:
            logging.error(f"Не удалось удалить приветственное сообщение у {user_id}: {e}")

        await context.bot.send_message(
            chat_id=user_id,
            text="⏳ Расчёт сброшен из-за неактивности. Нажмите «🧮 Новый расчёт», чтобы начать заново.",
            reply_markup=kb_main_menu()
        )
    except Exception:
        # Пользователь мог заблокировать бота или не иметь открытого диалога
        logging.exception("Не удалось отправить уведомление об автосбросе")

def start_or_restart_calc_session(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    """Создаёт/пересоздаёт сессию расчёта и ставит таймер автосброса. Возвращает session_id."""
    # Сбрасываем предыдущий таймер/сессию
    reset_calc_session(context, user_id)

    session_id = str(int(time.time() * 1000))  # достаточно уникально для 1 пользователя
    store = _calc_store(context)
    store[user_id] = {"session": session_id, "ts": time.time()}

    # Ставим новый таймер
    seconds = CALC_TIMEOUT_MINUTES * 60
    try:
        context.application.job_queue.run_once(
            _calc_timeout_job,
            when=seconds,
            name=_calc_job_name(user_id),
            data={"user_id": user_id},
            chat_id=user_id
        )
    except Exception:
        logging.exception("Ошибка постановки таймера расчёта")

    return session_id

def touch_calc_session(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Обновляет активность и пересоздаёт таймер (скользящий таймаут)."""
    store = _calc_store(context)
    sess = store.get(user_id)
    if not sess:
        return
    sess["ts"] = time.time()
    # Перезапускаем таймер
    try:
        cancel_calc_timeout(context, user_id)
        seconds = CALC_TIMEOUT_MINUTES * 60
        context.application.job_queue.run_once(
            _calc_timeout_job,
            when=seconds,
            name=_calc_job_name(user_id),
            data={"user_id": user_id},
            chat_id=user_id
        )
    except Exception:
        logging.exception("Ошибка обновления таймера расчёта")

def get_active_calc_session(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str | None:
    store = _calc_store(context)
    sess = store.get(user_id)
    return sess.get("session") if sess else None

def is_session_valid(context: ContextTypes.DEFAULT_TYPE, user_id: int, session_id: str) -> bool:
    return session_id and (get_active_calc_session(context, user_id) == session_id)

# ====== КЛАВИАТУРЫ ======
def kb_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧮 Новый расчёт", callback_data="menu:calc")],
        [InlineKeyboardButton("📊 Ваш тариф", callback_data="menu:status")],
        [InlineKeyboardButton("🚗 Выезды", callback_data="menu:visits")]
    ])

def kb_after_calc_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧮 Новый расчёт", callback_data="menu:calc")],
        [InlineKeyboardButton("⬅ Главное меню", callback_data="menu:start")]
    ])

def kb_visits_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Главное меню", callback_data="menu:start")],
        [InlineKeyboardButton("🧮 Новый расчёт", callback_data="menu:calc")]
    ])

WEEKDAYS_RU = {0: "пн", 1: "вт", 2: "ср", 3: "чт", 4: "пт", 5: "сб", 6: "вс"}

def kb_dates_menu(session_id: str):
    today = now_msk().date()
    rows = []
    rows.append([
        InlineKeyboardButton("📅 Сегодня", callback_data=f"date:{session_id}:{today.isoformat()}"),
        InlineKeyboardButton("📅 Завтра", callback_data=f"date:{session_id}:{(today + timedelta(days=1)).isoformat()}")
    ])
    row = []
    for i in range(2, 12):
        d = today + timedelta(days=i)
        label = f"{d.day:02d} ({WEEKDAYS_RU[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"date:{session_id}:{d.isoformat()}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅ Главное меню", callback_data="menu:start")])
    return InlineKeyboardMarkup(rows)

def kb_duration_menu(date_str: str, kind: str, session_id: str):
    rows = [
        [
            InlineKeyboardButton("☀ 4 часа", callback_data=f"time:{session_id}:{date_str}:{kind}:4"),
            InlineKeyboardButton("☀ 8 часов", callback_data=f"time:{session_id}:{date_str}:{kind}:8"),
        ],
        [
            InlineKeyboardButton("🌙 4 часа", callback_data=f"time:{session_id}:{date_str}:{kind}:night_4"),
            InlineKeyboardButton("🌙 8 часов", callback_data=f"time:{session_id}:{date_str}:{kind}:night_8"),
        ],
        [InlineKeyboardButton("⬅ Главное меню", callback_data="menu:start")]
    ]
    return InlineKeyboardMarkup(rows)

def kb_admin_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")]
    ])

def kb_admin_customers():
    rows = []
    for cid, cust in data["customers"].items():
        visits_count = len(cust.get("visits", []))
        users_count = len(cust.get("ids", []))
        button_text = f"{cust['name']} (🚗{visits_count} 👥{users_count})"
        rows.append([InlineKeyboardButton(button_text, callback_data=f"admin_customer:{cid}")])
    
    rows.append([InlineKeyboardButton("➕ Создать нового заказчика", callback_data="admin_create_customer")])
    rows.append([InlineKeyboardButton("🔍 Найти заказчика по пользователю", callback_data="admin_find_customer")])
    rows.append([InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")])
    return InlineKeyboardMarkup(rows)

def kb_admin_actions(cid):
    """Клавиатура промежуточного меню после выбора клиента"""
    customer = data["customers"][cid]
    status = "✅" if customer.get("discount") else "❌"
    total_visits = len(customer.get("visits", []))
    users_count = len(customer.get("ids", []))
    projects_sum = customer.get("projects_sum", 0)
    
    rows = [
        [InlineKeyboardButton(f"📊 Тариф: {status} Льгота", callback_data=f"admin_action:tariff:{cid}")],
        [InlineKeyboardButton(f"🚗 Выезды: {total_visits}", callback_data=f"admin_action:visits:{cid}")],
        [InlineKeyboardButton(f"👥 Пользователи: {users_count}", callback_data=f"admin_action:users:{cid}")],
        [InlineKeyboardButton(f"💰 Проекты: {fmt_rub(projects_sum)}", callback_data=f"admin_action:projects:{cid}")],
        [InlineKeyboardButton("📅 Добавить выезд", callback_data=f"admin_action:add_visit:{cid}")],
        [InlineKeyboardButton("🗑 Удалить заказчика", callback_data=f"admin_action:remove:{cid}")],
        [InlineKeyboardButton("🧹 Очистить выезды", callback_data=f"admin_action:clear_visits:{cid}")],
        [InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")]
    ]
    return InlineKeyboardMarkup(rows)

def kb_admin_visits_management(cid, visits):
    """Клавиатура для управления выездами: рядом с каждым выездом есть кнопка удаления"""
    rows = []
    # Для каждого выезда добавляем строку с информацией о выезде и кнопкой удаления справа
    for i, visit in enumerate(visits):
        # Создаем кнопку с кратким описанием выезда
        visit_info = format_visit_short(visit, i+1)
        # Ограничиваем длину текста кнопки
        if len(visit_info) > 30:
            visit_info = visit_info[:27] + "..."
        
        rows.append([
            InlineKeyboardButton(visit_info, callback_data=f"admin_visit_info:{cid}:{i}"),
            InlineKeyboardButton("🗑", callback_data=f"admin_delete_visit:{cid}:{i}")
        ])
    
    # Кнопка удалить все
    if visits:
        rows.append([InlineKeyboardButton("🔥 Удалить все выезды", callback_data=f"admin_delete_all:{cid}")])
    rows.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_action:back:{cid}")])
    return InlineKeyboardMarkup(rows)

def kb_admin_back(cid):
    """Клавиатура с кнопкой Назад"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Назад", callback_data=f"admin_action:back:{cid}")]
    ])

def kb_admin_user_management(cid):
    customer = data["customers"][cid]
    users = customer.get("ids", [])
    
    rows = []
    for uid in users:
        rows.append([
            InlineKeyboardButton(f"👤 {uid}", callback_data=f"admin_user_info:{cid}:{uid}"),
            InlineKeyboardButton("❌ Отвязать", callback_data=f"admin_unlink_specific:{cid}:{uid}")
        ])
    
    rows.append([InlineKeyboardButton("➕ Привязать пользователя", callback_data=f"admin_link_user:{cid}")])
    rows.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_action:back:{cid}")])
    return InlineKeyboardMarkup(rows)

def kb_admin_projects_management(cid):
    customer = data["customers"][cid]
    current_sum = customer.get("projects_sum", 0)
    
    rows = [
        [InlineKeyboardButton("➕ Добавить 10,000 ₽", callback_data=f"admin_add_amount:{cid}:10000")],
        [InlineKeyboardButton("➕ Добавить 25,000 ₽", callback_data=f"admin_add_amount:{cid}:25000")],
        [InlineKeyboardButton("➕ Добавить 50,000 ₽", callback_data=f"admin_add_amount:{cid}:50000")],
        [InlineKeyboardButton("💵 Установить точную сумму", callback_data=f"admin_set_exact:{cid}")],
        [InlineKeyboardButton("🔄 Обнулить сумму", callback_data=f"admin_reset_sum:{cid}")],
        [InlineKeyboardButton("⬅ Назад", callback_data=f"admin_action:back:{cid}")]
    ]
    return InlineKeyboardMarkup(rows)

def kb_admin_dates(cid):
    today = now_msk().date()
    rows = []
    # Добавляем сегодняшнюю дату
    rows.append([
        InlineKeyboardButton("📅 Сегодня", callback_data=f"admin_date:{today.isoformat()}")
    ])
    # Добавляем 10 предыдущих дат
    row = []
    for i in range(1, 11):
        d = today - timedelta(days=i)
        label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS_RU[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"admin_date:{d.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    # Кнопки Назад и Отменить
    rows.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"admin_date:back:{cid}"),
        InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")
    ])
    return InlineKeyboardMarkup(rows)

def kb_admin_kind():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 К точному времени", callback_data="admin_kind:exact")],
        [InlineKeyboardButton("⏰ Срочный (на завтра)", callback_data="admin_kind:urgent_tomorrow")],
        [InlineKeyboardButton("⏰ Срочный (сегодня)", callback_data="admin_kind:urgent_today")],
        [InlineKeyboardButton("🎉 Праздничный", callback_data="admin_kind:holiday")],
        [InlineKeyboardButton("🆓 Свободный график", callback_data="admin_kind:free")],
        [
            InlineKeyboardButton("⬅ Назад", callback_data="admin_kind:back"),
            InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")
        ]
    ])

def kb_admin_duration():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀ 4 часа", callback_data="admin_duration:4")],
        [InlineKeyboardButton("☀ 8 часов", callback_data="admin_duration:8")],
        [InlineKeyboardButton("🌙 4 часа", callback_data="admin_duration:night_4")],
        [InlineKeyboardButton("🌙 8 часов", callback_data="admin_duration:night_8")],
        [
            InlineKeyboardButton("⬅ Назад", callback_data="admin_duration:back"),
            InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")
        ]
    ])

def kb_admin_tariff_type():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Льготный", callback_data="admin_tariff:discount")],
        [InlineKeyboardButton("💰 Стандартный", callback_data="admin_tariff:standard")],
        [
            InlineKeyboardButton("⬅ Назад", callback_data="admin_tariff:back"),
            InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")
        ]
    ])

def kb_admin_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="admin_confirm:yes")],
        [
            InlineKeyboardButton("⬅ Назад", callback_data="admin_confirm:back"),
            InlineKeyboardButton("❌ Отменить", callback_data="admin_cancel")
        ]
    ])

def kb_admin_quick_customers(user_id):
    """Клавиатура для быстрой привязки пользователя к заказчику.
    ВАЖНО: кнопки заказчиков сразу привязывают user_id к выбранному заказчику.
    Также доступны создание нового заказчика и поиск.
    """
    rows = []
    for cid, cust in data["customers"].items():
        visits_count = len(cust.get("visits", []))
        users_count = len(cust.get("ids", []))
        button_text = f"{cust['name']} (🚗{visits_count} 👥{users_count})"
        rows.append([InlineKeyboardButton(button_text, callback_data=f"admin_quick_link:{cid}:{user_id}")])

    rows.append([InlineKeyboardButton("➕ Создать нового заказчика", callback_data="admin_create_customer")])
    rows.append([InlineKeyboardButton("🔍 Найти заказчика по пользователю", callback_data="admin_find_customer")])
    rows.append([InlineKeyboardButton("❌ Отменить", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


async def notify_user_registered(context: ContextTypes.DEFAULT_TYPE, user_id: int, customer_name: str) -> None:
    """Отправляет пользователю подтверждение регистрации и сразу открывает панель расчёта."""
    try:
        # Удаляем все старые служебные сообщения для непривязанных пользователей (если были)
        await delete_pending_welcome_messages(context, user_id)
        await context.bot.send_message(
            user_id,
            f"✅ Вы успешно зарегистрированы как представитель заказчика: {customer_name}.",
            reply_markup=kb_main_menu()  # ИЗМЕНЕНИЕ: главное меню вместо расчёта
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

# ====== ХЕНДЛЕРЫ ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ ПРИ ВХОДЕ
    try:
        await delete_calculation_messages(context, user_id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений при старте: {e}")
    
    cid, cust = find_customer_by_userid(user_id)
    
    if not cust:
        # Пользователь не привязан - отправляем сообщение администратору
        admin_message = (
            f"🔔 Новый пользователь!\n"
            f"ID: {user_id}\n"
            f"Имя: {update.effective_user.first_name or 'Не указано'}\n"
            f"Фамилия: {update.effective_user.last_name or 'Не указана'}\n"
            f"Username: @{update.effective_user.username or 'Не указан'}\n"
            f"Время: {now_msk().strftime('%d.%m.%Y %H:%M')}"
        )
        
        # Отправляем сообщение админу
        try:
            await context.bot.send_message(
                ADMIN_ID,
                admin_message,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍💼 Панель администратора", callback_data=f"admin_panel_link:{user_id}")]
                ])
            )
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения админу: {e}")
        
        # Сообщение для пользователя
        msg = await update.message.reply_text(
            "👋 Вас приветствует бот «Выезды ИП Смирнов».\n"
            "Скоро вам будет предоставлен доступ к функциям бота."
        )
        # Запоминаем ID сообщения, чтобы удалить его после привязки пользователя к заказчику
        add_pending_welcome_message(context, user_id, msg.message_id)
        return
    
    # Пользователь привязан - показываем основное меню
    # На всякий случай удаляем служебные сообщения (если пользователь раньше был 'не привязан')
    await delete_pending_welcome_messages(context, user_id)
    reset_calc_session(context, user_id)
    await update.message.reply_text(
        f"Добро пожаловать! Вы работаете с заказчиком: {cust['name']}",
        reply_markup=kb_main_menu()
    )
    context.user_data["welcomed"] = True

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ДЛЯ ВСЕХ КНОПОК menu:"""
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    _, cust = find_customer_by_userid(user_id)
    
    if not cust:
        await q.edit_message_text("❌ Вы не привязаны к заказчику. Обратитесь к администратору.")
        return
    
    if q.data == "menu:start":
        # Возврат в главное меню.
        # 1) Завершаем активную сессию расчёта (таймер/состояние), но НЕ трогаем данные заказчика.
        reset_calc_session(context, user_id)

        # 2) Удаляем сообщения "процесс расчёта" (например, "Выберите дату"),
        #    но текущее сообщение (по которому кликнули) не удаляем — мы его перезапишем меню.
        try:
            chat_id = q.message.chat_id
            current_mid = q.message.message_id
            ids = list(context.user_data.get('last_calc_message_ids', []))
            for mid in ids:
                if mid == current_mid:
                    continue
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception as e:
                    logging.debug(f"Не удалось удалить сообщение {mid}: {e}")

            # Очищаем трекинг — мы сейчас превратим текущее сообщение в главное меню
            context.user_data['last_calc_message_ids'] = []
        except Exception as e:
            logging.error(f"Ошибка при очистке сообщений расчёта: {e}")

        # 3) Перезаписываем текущее сообщение в "главное меню" с нужным текстом
        await q.edit_message_text(
            f"Вы работаете с заказчиком: {cust['name']}",
            reply_markup=kb_main_menu()
        )

    elif q.data == "menu:calc":
        # ВСЕГДА запускаем новый расчёт
        reset_calc_session(context, user_id)

        # Удаляем предыдущие сообщения "процесса расчёта", но НЕ удаляем текущее сообщение,
        # по которому пользователь кликнул (его мы перезапишем на "Выберите дату").
        try:
            chat_id = q.message.chat_id
            current_mid = q.message.message_id
            ids = list(context.user_data.get('last_calc_message_ids', []))
            for mid in ids:
                if mid == current_mid:
                    continue
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception as e:
                    logging.debug(f"Не удалось удалить сообщение {mid}: {e}")

            # Сбрасываем трекинг — дальше заново начнём отслеживать сообщение с выбором даты
            context.user_data['last_calc_message_ids'] = []
        except Exception as e:
            logging.error(f"Ошибка при очистке сообщений расчёта: {e}")

        session_id = start_or_restart_calc_session(context, user_id)
        message = await q.edit_message_text("Выберите дату:", reply_markup=kb_dates_menu(session_id))

        # ОТСЛЕЖИВАЕМ ЭТО СООБЩЕНИЕ ДЛЯ БУДУЩЕГО УДАЛЕНИЯ
        add_message_to_tracking(context, message.message_id)
    
    elif q.data == "menu:status":
        reset_calc_session(context, user_id)
        status = "Да ✅" if cust.get("discount") else "Нет ❌"
        total_visits = len(cust.get("visits", []))
        text = (f"📊 Ваш тариф ({cust['name']})\n"
                f"— Выездов: {total_visits}\n"
                f"— Льготный тариф: {status}\n"
                f"— Сумма проектов: {fmt_rub(cust.get('projects_sum',0))}")
        await q.edit_message_text(text, reply_markup=kb_main_menu())
    
    elif q.data == "menu:visits":
        reset_calc_session(context, user_id)
        
        visits = cust.get("visits", [])
        if not visits:
            await q.edit_message_text("🚗 У вас пока нет записей о выездах.", reply_markup=kb_main_menu())
            return
        
        text = f"🚗 Выезды сварщиков для {cust['name']}:\n\n"
        
        for i, visit in enumerate(visits, 1):
            date_str = datetime.strptime(visit["date"], "%Y-%m-%d").strftime("%d.%m.%Y") if visit["date"] != "free" else "Свободная дата"
            kind_str = {
                "exact": "📅 К точному времени",
                "urgent_tomorrow": "⏰ Срочный (на завтра)",
                "urgent_today": "⏰ Срочный (сегодня)",
                "holiday": "🎉 Праздничный",
                "free": "🆓 Свободный график"
            }.get(visit["kind"], visit["kind"])
            duration_str = {
                "4": "4 часа ☀",
                "8": "8 часов ☀",
                "night_4": "4 часа 🌙 (ночной тариф)",
                "night_8": "8 часов 🌙 (ночной тариф)"
            }.get(visit["duration"], visit["duration"])
            tariff_str = "Льготный" if visit.get("tariff_type") == "discount" else "Стандартный"
            
            text += (f"{i}. 📅 {date_str}\n"
                    f"   📌 {kind_str}\n"
                    f"   ⏳ {duration_str}\n"
                    f"   💰 {fmt_rub(visit['price'])}\n"
                    f"   📊 Тариф: {tariff_str}\n"
                    f"   ——————————————\n")
        
        await q.edit_message_text(text, reply_markup=kb_visits_menu())


async def on_date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    parts = q.data.split(":")
    # ожидаем: date:<session_id>:<date_iso|free>
    if len(parts) < 3:
        await q.edit_message_text("❌ Некорректные данные. Нажмите «🧮 Новый расчёт».", reply_markup=kb_main_menu())
        return

    session_id = parts[1]
    date_str = parts[2]

    if not is_session_valid(context, q.from_user.id, session_id):
        # Нажали кнопку из старого сообщения/старого расчёта
        await q.answer("Этот расчёт устарел. Нажмите «🧮 Новый расчёт».", show_alert=True)
        try:
            await q.edit_message_text("♻️ Этот расчёт уже неактуален.\n\nНажмите «🧮 Новый расчёт».", reply_markup=kb_main_menu())
        except Exception:
            pass
        return

    # Обновляем таймер неактивности
    touch_calc_session(context, q.from_user.id)

    if date_str == "free":
        kind = "free"
        text = "🆓 Свободный график\n\n🌙 Ночной тариф действует с 21:00 до 09:00\n\nВыберите длительность и тип тарифа:"
        message = await q.edit_message_text(text, reply_markup=kb_duration_menu("free", kind, session_id))
        
        # ОТСЛЕЖИВАЕМ ЭТО СООБЩЕНИЕ
        add_message_to_tracking(context, message.message_id)
        return

    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    kind = classify_kind(d)
    TYPES = {
        "exact": "📅 К точному времени",
        "urgent_tomorrow": "⏰ Срочный (на завтра)",
        "urgent_today": "⏰ Срочный (сегодня)",
        "holiday": "🎉 Праздничный",
        "free": "🆓 Свободный график"
    }
    text = (f"📅 Дата: {d.strftime('%d.%m.%Y')}\n"            f"📌 Тип выезда: {TYPES[kind]}\n\n"            "🌙 Ночной тариф действует с 21:00 до 09:00\n\n"            "Выберите длительность и тип тарифа:")
    message = await q.edit_message_text(text, reply_markup=kb_duration_menu(d.isoformat(), kind, session_id))
    
    # ОТСЛЕЖИВАЕМ ЭТО СООБЩЕНИЕ
    add_message_to_tracking(context, message.message_id)


async def on_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    parts = q.data.split(":")
    # ожидаем: time:<session_id>:<date_iso|free>:<kind>:<duration>
    if len(parts) < 5:
        await q.edit_message_text("❌ Некорректные данные. Нажмите «🧮 Новый расчёт».", reply_markup=kb_main_menu())
        return

    session_id = parts[1]
    date_str, kind, duration = parts[2], parts[3], parts[4]

    if not is_session_valid(context, q.from_user.id, session_id):
        await q.answer("Этот расчёт устарел. Нажмите «🧮 Новый расчёт».", show_alert=True)
        try:
            await q.edit_message_text("♻️ Этот расчёт уже неактуален.\n\nНажмите «🧮 Новый расчёт».", reply_markup=kb_main_menu())
        except Exception:
            pass
        return

    # Обновляем таймер (на всякий случай)
    touch_calc_session(context, q.from_user.id)

    cid, cust = find_customer_by_userid(q.from_user.id)
    if not cust:
        reset_calc_session(context, q.from_user.id)
        await q.edit_message_text("❌ Вы не привязаны к заказчику. Обратитесь к администратору.")
        return

    discount = cust.get("discount", False)
    price = calc_price(kind, duration, discount)

    sel_date = now_msk().date() if date_str == "free" else datetime.strptime(date_str, "%Y-%m-%d").date()

    TYPES = {
        "exact": "📅 К точному времени",
        "urgent_tomorrow": "⏰ Срочный (на завтра)",
        "urgent_today": "⏰ Срочный (сегодня)",
        "holiday": "🎉 Праздничный",
        "free": "🆓 Свободный график"
    }

    DURATIONS = {
        "4": "4 часа ☀",
        "8": "8 часов ☀",
        "night_4": "4 часа 🌙 (ночной тариф)",
        "night_8": "8 часов 🌙 (ночной тариф)"
    }

    text = (f"📌 Заказчик: {cust['name']}\n"            f"📅 Дата: {sel_date.strftime('%d.%m.%Y')}\n"            f"📌 Тип выезда: {TYPES[kind]}\n"            f"⏳ Длительность: {DURATIONS[duration]}\n"            f"💰 Стоимость: {fmt_rub(price)}")

    message = await q.edit_message_text(text, reply_markup=kb_after_calc_menu())
    
    # ОТСЛЕЖИВАЕМ ЭТО СООБЩЕНИЕ
    add_message_to_tracking(context, message.message_id)

    # ✅ Завершаем активный расчёт (один активный расчёт)
    reset_calc_session(context, q.from_user.id)

# ====== БЫСТРЫЕ ДЕЙСТВИЯ АДМИНИСТРАТОРА ======
# ====== ВХОД В АДМИН-ПАНЕЛЬ (ТОЛЬКО КНОПКА) ======
async def admin_open_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает интерактивную админ-панель по кнопке."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END

    # если пришли с admin_panel_link:<user_id>, сохраняем и открываем панель
    if query.data.startswith("admin_panel_link:"):
        user_id = query.data.split(":", 1)[1]
        context.user_data["pending_link_user_id"] = user_id

    return await cmd_admin(update, context)



async def admin_quick_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрая привязка пользователя к заказчику"""
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split(":")
    cid = parts[1]
    user_id = parts[2]
    
    if cid not in data["customers"]:
        await query.edit_message_text("❌ Заказчик не найден.")
        return
    
    # Отвязываем от других заказчиков
    for other_cid, other_cust in data["customers"].items():
        if user_id in other_cust.get("ids", []):
            other_cust["ids"].remove(user_id)
    
    # Привязываем к выбранному
    customer = data["customers"][cid]
    if user_id not in customer["ids"]:
        customer["ids"].append(user_id)
        save_data()
        
        # Уведомляем пользователя
        await notify_user_registered(context, int(user_id), customer['name'])
        
        await query.edit_message_text(
            f"✅ Пользователь {user_id} привязан к заказчику {customer['name']}\n\n"
            f"Пользователь получил уведомление о регистрации."
        )
    else:
        await query.edit_message_text(
            f"ℹ Пользователь {user_id} уже привязан к заказчику {customer['name']}"
        )

# ====== АДМИН КОМАНДЫ ======
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает интерактивную панель администратора (быстро и без 'залипаний')."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    # Если команда пришла текстом (/admin, /addvist) — это обычный вход в админку
    if not update.callback_query:
        context.user_data.pop("pending_link_user_id", None)

    pending_uid = context.user_data.get("pending_link_user_id")

    # Сбрасываем незавершённые шаги добавления выезда/ввода текста
    context.user_data.pop("admin_visit", None)

    if pending_uid:
        text = f"👥 Выберите заказчика для привязки пользователя {pending_uid}:"
        markup = kb_admin_quick_customers(pending_uid)
    else:
        text = "👑 Админ-панель: выберите действие / заказчика:"
        markup = kb_admin_customers()

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await query.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)

    return SELECT_CUSTOMER


# ====== ДОБАВЛЯЕМ НЕДОСТАЮЩИЕ ФУНКЦИИ ======
async def cmd_addvisit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления выезда (админ)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    if not data["customers"]:
        await update.message.reply_text("❌ Нет заказчиков. Сначала создайте заказчика.")
        return
    
    context.user_data['admin_visit'] = {}
    await update.message.reply_text("👥 Выберите заказчика:", reply_markup=kb_admin_customers())
    return SELECT_CUSTOMER

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для ручной регистрации пользователя администратором"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 2:
            await update.message.reply_text(
                "Использование: /register <id_пользователя> <id_заказчика>\n\n"
                "Пример: /register 123456789 1"
            )
            return
        
        user_id = context.args[0]
        cid = context.args[1]
        
        if cid not in data["customers"]:
            await update.message.reply_text(f"❌ Заказчик с ID {cid} не найден.")
            return
        
        # Отвязываем от других заказчиков
        for other_cid, other_cust in data["customers"].items():
            if user_id in other_cust.get("ids", []):
                other_cust["ids"].remove(user_id)
        
        # Привязываем к выбранному
        customer = data["customers"][cid]
        if user_id not in customer["ids"]:
            customer["ids"].append(user_id)
            save_data()
            
            # Уведомляем пользователя
            await notify_user_registered(context, int(user_id), customer['name'])
            await update.message.reply_text(
                f"✅ Пользователь {user_id} привязан к заказчику {customer['name']}\n\n"
                f"Пользователь получил уведомление."
            )
        else:
            await update.message.reply_text(
                f"ℹ Пользователь {user_id} уже привязан к заказчику {customer['name']}"
            )
            
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание заказчика только по имени (ID генерируется автоматически)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) < 1:
            await update.message.reply_text("Использование: /create <имя>")
            return
            
        name = " ".join(context.args)
        cid = ensure_customer(name)
        save_data()
        await update.message.reply_text(f"✅ Создан заказчик: {name} (ID: {cid})")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /create <имя>")

async def admin_select_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END

    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Добавление выезда отменено.")
        return ConversationHandler.END
    
    # Обработка создания заказчика
    if query.data == "admin_create_customer":
        await query.edit_message_text(
            "👤 Создание нового заказчика:\n\n"
            "Введите название заказчика:",
            reply_markup=kb_admin_cancel()
        )
        return CREATE_CUSTOMER
    
    # Обработка поиска заказчика
    if query.data == "admin_find_customer":
        await query.edit_message_text(
            "🔍 Поиск заказчика по ID пользователя:\n\n"
            "Введите ID пользователя:",
            reply_markup=kb_admin_cancel()
        )
        return FIND_CUSTOMER
    
    # Обработка выбора существующего заказчика
    cid = query.data.split(":")[1]
    context.user_data['admin_visit'] = {}
    context.user_data['admin_visit']['cid'] = cid
    context.user_data['admin_visit']['customer_name'] = data["customers"][cid]["name"]
    
    await query.edit_message_text(
        f"👥 Заказчик: {data['customers'][cid]['name']}\n\nВыберите действие:",
        reply_markup=kb_admin_actions(cid)
    )
    return SELECT_ACTION

async def admin_create_customer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        try:
            name = update.message.text.strip()

            if not name:
                await update.message.reply_text("❌ Название заказчика не может быть пустым.")
                return CREATE_CUSTOMER

            cid = ensure_customer(name)
            save_data()

            pending_uid = context.user_data.get("pending_link_user_id")
            if pending_uid:
                await update.message.reply_text(
                    f"✅ Создан заказчик: {name} (ID: {cid})\n\n"
                    f"Теперь выберите заказчика для привязки пользователя {pending_uid}:",
                    reply_markup=kb_admin_quick_customers(pending_uid)
                )
            else:
                await update.message.reply_text(
                    f"✅ Создан заказчик: {name} (ID: {cid})\n\n"
                    "Теперь выберите заказчика:",
                    reply_markup=kb_admin_customers()
                )
            return SELECT_CUSTOMER

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return CREATE_CUSTOMER


async def admin_find_customer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        try:
            uid = update.message.text.strip()

            found_cid = None
            for cid, cust in data["customers"].items():
                if uid in cust.get("ids", []):
                    found_cid = cid
                    break

            if not found_cid:
                await update.message.reply_text(
                    f"❌ Пользователь {uid} не привязан ни к одному заказчику.\n\n"
                    "Попробуйте другой ID или выберите заказчика вручную:",
                    reply_markup=kb_admin_customers()
                )
                return SELECT_CUSTOMER

            customer = data["customers"][found_cid]
            context.user_data['admin_visit'] = {}
            context.user_data['admin_visit']['cid'] = found_cid
            context.user_data['admin_visit']['customer_name'] = customer["name"]

            await update.message.reply_text(
                f"✅ Найден заказчик: {customer['name']} (ID: {found_cid})\n\n"
                "Выберите действие:",
                reply_markup=kb_admin_actions(found_cid)
            )
            return SELECT_ACTION

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return FIND_CUSTOMER


async def admin_select_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Добавление выезда отменено.")
        return ConversationHandler.END
    
    # Обработка кнопок удаления выездов
    if query.data.startswith("admin_delete_"):
        parts = query.data.split(":")
        action = parts[0]
        cid = parts[1]
        
        customer = data["customers"][cid]
        visits = customer.get("visits", [])
        
        if action == "admin_delete_visit":
            visit_index = int(parts[2])
            if 0 <= visit_index < len(visits):
                deleted_visit = visits.pop(visit_index)
                recalc_discount(customer)
                save_data()
                
                date_str = deleted_visit["date"]
                if date_str != "free":
                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                    date_display = d.strftime("%d.%m.%Y")
                else:
                    date_display = "Свободная дата"
                
                await query.edit_message_text(
                    f"✅ Выезд от {date_display} удален!\n"
                    f"Осталось выездов: {len(visits)}",
                    reply_markup=kb_admin_visits_management(cid, visits)
                )
            else:
                await query.edit_message_text(
                    "❌ Ошибка: выезд не найден",
                    reply_markup=kb_admin_visits_management(cid, visits) 
                )
            return SELECT_ACTION
        
        elif action == "admin_delete_all":
            visits_count = len(visits)
            customer["visits"] = []
            recalc_discount(customer)
            save_data()
            
            await query.edit_message_text(
                f"✅ Все выезды удалены! Удалено записей: {visits_count}",
                reply_markup=kb_admin_actions(cid)
            )
            return SELECT_ACTION
    
    parts = query.data.split(":")
    
    # Обработка привязки/отвязки пользователей
    if query.data.startswith("admin_unlink_specific:"):
        cid = parts[1]
        uid = parts[2]
        
        customer = data["customers"][cid]
        if uid in customer.get("ids", []):
            customer["ids"].remove(uid)
            save_data()
            await query.edit_message_text(
                f"✅ Пользователь {uid} отвязан от заказчика {customer['name']}",
                reply_markup=kb_admin_user_management(cid)
            )
        else:
            await query.edit_message_text(
                f"❌ Пользователь {uid} не привязан к заказчику",
                reply_markup=kb_admin_user_management(cid)
            )
        return SELECT_ACTION
    
    # Обработка управления суммой проектов
    if query.data.startswith("admin_add_amount:"):
        cid = parts[1]
        amount = int(parts[2])
        
        customer = data["customers"][cid]
        customer["projects_sum"] = customer.get("projects_sum", 0) + amount
        recalc_discount(customer)
        save_data()
        
        await query.edit_message_text(
            f"✅ Добавлено {fmt_rub(amount)} к сумме проектов\n"
            f"Текущая сумма: {fmt_rub(customer['projects_sum'])}",
            reply_markup=kb_admin_projects_management(cid)
        )
        return SELECT_ACTION
    
    if query.data.startswith("admin_reset_sum:"):
        cid = parts[1]
        
        customer = data["customers"][cid]
        customer["projects_sum"] = 0
        recalc_discount(customer)
        save_data()
        
        await query.edit_message_text(
            f"✅ Сумма проектов обнулена",
            reply_markup=kb_admin_projects_management(cid)
        )
        return SELECT_ACTION
    
    # Ожидается формат admin_action:<action>:<cid>
    if len(parts) >= 3 and parts[0] == "admin_action":
        action = parts[1]
        cid = parts[2]
    else:
        cid = context.user_data.get('admin_visit', {}).get('cid')
        action = None
    
    if cid:
        context.user_data['admin_visit']['cid'] = cid
        context.user_data['admin_visit']['customer_name'] = data["customers"][cid]["name"]
    
    if action == "back":
        await query.edit_message_text(
            f"👥 Заказчик: {data['customers'][cid]['name']}\n\nВыберите действие:",
            reply_markup=kb_admin_actions(cid)
        )
        return SELECT_ACTION
    
    elif action == "tariff":
        customer = data["customers"][cid]
        status = "Да ✅" if customer.get("discount") else "Нет ❌"
        total_visits = len(customer.get("visits", []))
        text = (f"📊 Тариф заказчика {customer['name']}\n"
                f"— Выездов: {total_visits}\n"
                f"— Льготный тариф: {status}\n"
                f"— Сумма проектов: {fmt_rub(customer.get('projects_sum',0))}")
        await query.edit_message_text(text, reply_markup=kb_admin_back(cid))
        return SELECT_ACTION
    
    elif action == "visits":
        customer = data["customers"][cid]
        visits = customer.get("visits", [])
        
        if not visits:
            text = f"🚗 У заказчика {customer['name']} пока нет записей о выездах."
            await query.edit_message_text(text, reply_markup=kb_admin_visits_management(cid, []))
            return SELECT_ACTION
        
        text = f"🚗 Выезды сварщиков для {customer['name']}:\n\n"
        
        for i, visit in enumerate(visits, 1):
            date_str = datetime.strptime(visit["date"], "%Y-%m-%d").strftime("%d.%m.%Y") if visit["date"] != "free" else "Свободная дата"
            kind_str = {
                "exact": "📅 К точному времени",
                "urgent_tomorrow": "⏰ Срочный (на завтра)",
                "urgent_today": "⏰ Срочный (сегодня)",
                "holiday": "🎉 Праздничный",
                "free": "🆓 Свободный график"
            }.get(visit["kind"], visit["kind"])
            duration_str = {
                "4": "4 часа ☀",
                "8": "8 часов ☀",
                "night_4": "4 часа 🌙 (ночной тариф)",
                "night_8": "8 часов 🌙 (ночной тариф)"
            }.get(visit["duration"], visit["duration"])
            tariff_str = "Льготный" if visit.get("tariff_type") == "discount" else "Стандартный"
            
            text += (f"{i}. 📅 {date_str}\n"
                    f"   📌 {kind_str}\n"
                    f"   ⏳ {duration_str}\n"
                    f"   💰 {fmt_rub(visit['price'])}\n"
                    f"   📊 Тариф: {tariff_str}\n"
                    f"   ——————————————\n")
        
        if len(text) > 4000:
            text = text[:4000] + "\n... (список обрезан)"
        
        await query.edit_message_text(text, reply_markup=kb_admin_visits_management(cid, visits))
        return SELECT_ACTION
    
    elif action == "users":
        customer = data["customers"][cid]
        users_count = len(customer.get("ids", []))
        await query.edit_message_text(
            f"👥 Управление пользователями для {customer['name']}\n"
            f"Привязано пользователей: {users_count}",
            reply_markup=kb_admin_user_management(cid)
        )
        return SELECT_ACTION
    
    elif action == "projects":
        customer = data["customers"][cid]
        current_sum = customer.get("projects_sum", 0)
        await query.edit_message_text(
            f"💰 Управление суммой проектов для {customer['name']}\n"
            f"Текущая сумма: {fmt_rub(current_sum)}",
            reply_markup=kb_admin_projects_management(cid)
        )
        return SELECT_ACTION
    
    elif action == "add_visit":
        await query.edit_message_text("📅 Выберите дату выезда:", reply_markup=kb_admin_dates(cid))
        return SELECT_DATE
    
    elif action == "remove":
        customer_name = data["customers"][cid]["name"]
        del data["customers"][cid]
        save_data()
        await query.edit_message_text(f"✅ Заказчик '{customer_name}' удален!")
        return ConversationHandler.END
    
    elif action == "clear_visits":
        visits_count = len(data["customers"][cid].get("visits", []))
        data["customers"][cid]["visits"] = []
        recalc_discount(data["customers"][cid])
        save_data()
        await query.edit_message_text(f"✅ Выезды очищены! Удалено: {visits_count} записей")
        return SELECT_ACTION
    
    # Обработка привязки пользователя
    if query.data.startswith("admin_link_user:"):
        cid = parts[1]
        context.user_data['link_cid'] = cid
        await query.edit_message_text(
            "🔗 Привязка пользователя к заказчику\n\n"
            "Введите ID пользователя:",
            reply_markup=kb_admin_cancel()
        )
        return LINK_USER
    
    # Обработка установки точной суммы
    if query.data.startswith("admin_set_exact:"):
        cid = parts[1]
        context.user_data['set_sum_cid'] = cid
        await query.edit_message_text(
            "💵 Установка точной суммы проектов\n\n"
            "Введите сумму:",
            reply_markup=kb_admin_cancel()
        )
        return SET_SUM

async def admin_link_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        try:
            uid = update.message.text.strip()
            cid = context.user_data.get('link_cid')
            
            if not cid or cid not in data["customers"]:
                await update.message.reply_text("❌ Ошибка: заказчик не найден")
                return LINK_USER
            
            # Отвязываем от других заказчиков
            for other_cid, other_cust in data["customers"].items():
                if uid in other_cust.get("ids", []):
                    other_cust["ids"].remove(uid)
            
            # Привязываем к текущему
            customer = data["customers"][cid]
            if uid not in customer["ids"]:
                customer["ids"].append(uid)
                save_data()
                
                # Уведомляем пользователя
                await notify_user_registered(context, int(uid), customer['name'])
                
                await update.message.reply_text(
                    f"✅ Пользователь {uid} привязан к заказчика {customer['name']}",
                    reply_markup=kb_admin_user_management(cid)
                )
            else:
                await update.message.reply_text(
                    f"ℹ Пользователь {uid} уже привязан к заказчику",
                    reply_markup=kb_admin_user_management(cid)
                )
            return SELECT_ACTION
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return LINK_USER

async def admin_set_sum_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        try:
            amount = int(update.message.text.strip())
            cid = context.user_data.get('set_sum_cid')
            
            if not cid or cid not in data["customers"]:
                await update.message.reply_text("❌ Ошибка: заказчик не найден")
                return SET_SUM
            
            customer = data["customers"][cid]
            customer["projects_sum"] = amount
            recalc_discount(customer)
            save_data()
            
            await update.message.reply_text(
                f"✅ Сумма проектов установлена: {fmt_rub(amount)}",
                reply_markup=kb_admin_projects_management(cid)
            )
            return SELECT_ACTION
            
        except ValueError:
            await update.message.reply_text("❌ Ошибка: введите число")
            return SET_SUM
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return SET_SUM

# Остальные обработчики дат, типов, длительности и подтверждения остаются без изменений
async def admin_select_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Добавление выезда отменено.")
        return ConversationHandler.END
    
    if query.data.startswith("admin_date:back"):
        cid = query.data.split(":")[2] if len(query.data.split(":")) > 2 else context.user_data['admin_visit']['cid']
        await query.edit_message_text(
            f"👥 Заказчик: {data['customers'][cid]['name']}\n\nВыберите действие:",
            reply_markup=kb_admin_actions(cid)
        )
        return SELECT_ACTION
    
    date_str = query.data.split(":")[1]
    context.user_data['admin_visit']['date'] = date_str
    
    await query.edit_message_text("📌 Выберите тип выезда:", reply_markup=kb_admin_kind())
    return SELECT_KIND

async def admin_select_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Добавление выезда отменено.")
        return ConversationHandler.END
    
    if query.data == "admin_kind:back":
        cid = context.user_data['admin_visit']['cid']
        await query.edit_message_text("📅 Выберите дату выезда:", reply_markup=kb_admin_dates(cid))
        return SELECT_DATE
    
    kind = query.data.split(":")[1]
    context.user_data['admin_visit']['kind'] = kind
    
    TYPES = {
        "exact": "📅 К точному времени",
        "urgent_tomorrow": "⏰ Срочный (на завтра)",
        "urgent_today": "⏰ Срочный (сегодня)",
        "holiday": "🎉 Праздничный",
        "free": "🆓 Свободный график"
    }
    
    date_str = context.user_data['admin_visit']['date']
    if date_str == "free":
        date_display = "Свободная дата"
    else:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        date_display = d.strftime("%d.%m.%Y")
    
    await query.edit_message_text(
        f"📅 Дата: {date_display}\n"
        f"📌 Тип: {TYPES[kind]}\n\n"
        f"⏳ Выберите длительность:",
        reply_markup=kb_admin_duration()
    )
    return SELECT_DURATION

async def admin_select_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Добавление выезда отменено.")
        return ConversationHandler.END
    
    if query.data == "admin_duration:back":
        await query.edit_message_text("📌 Выберите тип выезда:", reply_markup=kb_admin_kind())
        return SELECT_KIND
    
    duration = query.data.split(":")[1]
    context.user_data['admin_visit']['duration'] = duration
    
    await query.edit_message_text("💰 Выберите тип тарифа:", reply_markup=kb_admin_tariff_type())
    return SELECT_TARIFF_TYPE

async def admin_select_tariff_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Добавление выезда отменено.")
        return ConversationHandler.END
    
    if query.data == "admin_tariff:back":
        await query.edit_message_text("⏳ Выберите длительность:", reply_markup=kb_admin_duration())
        return SELECT_DURATION
    
    tariff_type = query.data.split(":")[1]
    context.user_data['admin_visit']['tariff_type'] = tariff_type
    
    cid = context.user_data['admin_visit']['cid']
    kind = context.user_data['admin_visit']['kind']
    duration = context.user_data['admin_visit']['duration']
    
    discount = (tariff_type == "discount")
    price = calc_price(kind, duration, discount)
    context.user_data['admin_visit']['price'] = price
    
    TYPES = {
        "exact": "📅 К точному времени",
        "urgent_tomorrow": "⏰ Срочный (на завтра)",
        "urgent_today": "⏰ Срочный (сегодня)",
        "holiday": "🎉 Праздничный",
        "free": "🆓 Свободный график"
    }
    
    DURATIONS = {
        "4": "4 часа ☀",
        "8": "8 часов ☀",
        "night_4": "4 часа 🌙 (ночной тариф)",
        "night_8": "8 часов 🌙 (ночной тариф)"
    }
    
    TARIFF_TYPES = {
        "discount": "Льготный",
        "standard": "Стандартный"
    }
    
    date_str = context.user_data['admin_visit']['date']
    if date_str == "free":
        date_display = "Свободная дата"
    else:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        date_display = d.strftime("%d.%m.%Y")
    
    text = (
        f"✅ Подтвердите добавление выезда:\n\n"
        f"👥 Заказчик: {context.user_data['admin_visit']['customer_name']}\n"
        f"📅 Дата: {date_display}\n"
        f"📌 Тип: {TYPES[kind]}\n"
        f"⏳ Длительность: {DURATIONS[duration]}\n"
        f"💰 Тип тарифа: {TARIFF_TYPES[tariff_type]}\n"
        f"💵 Стоимость: {fmt_rub(price)}"
    )
    
    await query.edit_message_text(text, reply_markup=kb_admin_confirm())
    return CONFIRM_VISIT

async def admin_confirm_visit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "admin_cancel":
        await query.edit_message_text("❌ Добавление выезда отменено.")
        return ConversationHandler.END
    
    if query.data == "admin_confirm:back":
        await query.edit_message_text("💰 Выберите тип тарифа:", reply_markup=kb_admin_tariff_type())
        return SELECT_TARIFF_TYPE
    
    if query.data == "admin_confirm:yes":
        cid = context.user_data['admin_visit']['cid']
        visit_data = {
            "date": context.user_data['admin_visit']['date'],
            "kind": context.user_data['admin_visit']['kind'],
            "duration": context.user_data['admin_visit']['duration'],
            "price": context.user_data['admin_visit']['price'],
            "tariff_type": context.user_data['admin_visit']['tariff_type']
        }
        
        if "visits" not in data["customers"][cid]:
            data["customers"][cid]["visits"] = []
        
        data["customers"][cid]["visits"].append(visit_data)
        recalc_discount(data["customers"][cid])
        save_data()
        
        await query.edit_message_text("✅ Выезд успешно добавлен!")
        return ConversationHandler.END

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Добавление выезда отменено.")
    return ConversationHandler.END

# ====== ТЕКСТОВЫЕ КОМАНДЫ АДМИНИСТРАТОРА ======
async def cmd_customers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    if not data["customers"]:
        await update.message.reply_text("Список заказчиков пуст.")
        return
    
    lines = ["📋 Заказчики:"]
    for cid, cust in data["customers"].items():
        status = "Да ✅" if cust.get("discount") else "Нет ❌"
        visits_count = len(cust.get("visits", []))
        lines.append(f"├─ {cid}: {cust['name']}")
        lines.append(f"│  ID пользователей: {', '.join(cust['ids']) or 'нет'}")
        lines.append(f"│  Выездов: {visits_count} | Льгота: {status} | Проекты: {fmt_rub(cust['projects_sum'])}")
        lines.append("╰──────────────────")
    
    message = "\n".join(lines)
    if len(message) > 4000:
        parts = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for part in parts:
            await update.message.reply_text(part)
    else:
        await update.message.reply_text(message)

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 2:
            await update.message.reply_text("Использование: /link <id_заказчика> <id_пользователя>")
            return
            
        cid, uid = context.args[0], context.args[1]
        
        if cid not in data["customers"]:
            await update.message.reply_text(f"❌ Заказчик с ID {cid} не найден.")
            return
            
        for other_cid, other_cust in data["customers"].items():
            if uid in other_cust.get("ids", []):
                other_cust["ids"].remove(uid)
                logging.info(f"Пользователь {uid} отвязан от заказчика {other_cid}")
        
        if uid not in data["customers"][cid]["ids"]:
            data["customers"][cid]["ids"].append(uid)
            save_data()
            
            # Уведомляем пользователя
            await notify_user_registered(context, int(uid), data["customers"][cid]["name"])
            await update.message.reply_text(f"✅ Пользователь {uid} привязан к заказчику {cid} ({data['customers'][cid]['name']})")
        else:
            await update.message.reply_text(f"ℹ Пользователь {uid} уже привязан к заказчику {cid}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /link <id_заказчика> <id_пользователя>")

async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 2:
            await update.message.reply_text("Использование: /unlink <id_заказчика> <id_пользователя>")
            return
            
        cid, uid = context.args[0], context.args[1]
        
        if cid not in data["customers"]:
            await update.message.reply_text(f"❌ Заказчик с ID {cid} не найден.")
            return
            
        if uid in data["customers"][cid]["ids"]:
            data["customers"][cid]["ids"].remove(uid)
            save_data()
            await update.message.reply_text(f"✅ Пользователь {uid} отвязан от заказчика {cid}")
        else:
            await update.message.reply_text(f"ℹ Пользователь {uid} не привязан к заказчику {cid}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /unlink <id_заказчика> <id_пользователя>")

async def cmd_addsum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 2:
            await update.message.reply_text("Использование: /addsum <id_заказчика> <сумма>")
            return
            
        cid, summ = context.args[0], int(context.args[1])
        
        if cid not in data["customers"]:
            await update.message.reply_text(f"❌ Заказчик с ID {cid} не найден.")
            return
            
        data["customers"][cid]["projects_sum"] += summ
        recalc_discount(data["customers"][cid])
        save_data()
        await update.message.reply_text(f"✅ {fmt_rub(summ)} добавлено заказчику {cid}. Всего: {fmt_rub(data['customers'][cid]['projects_sum'])}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /addsum <id_заказчика> <сумма>")

async def cmd_setsum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 2:
            await update.message.reply_text("Использование: /setsum <id_заказчика> <сумма>")
            return
            
        cid, summ = context.args[0], int(context.args[1])
        
        if cid not in data["customers"]:
            await update.message.reply_text(f"❌ Заказчик с ID {cid} не найден.")
            return
            
        data["customers"][cid]["projects_sum"] = summ
        recalc_discount(data["customers"][cid])
        save_data()
        await update.message.reply_text(f"✅ Сумма заказчика {cid} установлена: {fmt_rub(summ)}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /setsum <id_заказчика> <сумма>")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 1:
            await update.message.reply_text("Использование: /remove <id_заказчика>")
            return
            
        cid = context.args[0]
        
        if cid not in data["customers"]:
            await update.message.reply_text(f"❌ Заказчик с ID {cid} не найден.")
            return
            
        customer_name = data["customers"][cid]["name"]
        del data["customers"][cid]
        save_data()
        await update.message.reply_text(f"✅ Заказчик {customer_name} (ID: {cid}) удален.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /remove <id_заказчика>")

async def cmd_finduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 1:
            await update.message.reply_text("Использование: /finduser <id_пользователя>")
            return

        uid = context.args[0]

        found = False
        for cid, cust in data["customers"].items():
            if uid in cust.get("ids", []):
                status = "Да ✅" if cust.get("discount") else "Нет ❌"
                visits_count = len(cust.get("visits", []))
                await update.message.reply_text(
                    f"👤 Пользователь {uid} привязан к:\n"
                    f"Заказчик: {cust['name']} (ID: {cid})\n"
                    f"Выездов: {visits_count} | Льгота: {status} | Проекты: {fmt_rub(cust['projects_sum'])}"
                )
                found = True
                break

        if not found:
            await update.message.reply_text(f"❌ Пользователь {uid} не привязан ни к одному заказчику.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /finduser <id_пользователя>")

async def cmd_clearvisits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    # УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ С РАСЧЁТАМИ
    try:
        await delete_calculation_messages(context, update.effective_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении сообщений: {e}")
    
    try:
        if len(context.args) != 1:
            await update.message.reply_text("Использование: /clearvisits <id_заказчика>")
            return
        
        cid = context.args[0]
        
        if cid not in data["customers"]:
            await update.message.reply_text(f"❌ Заказчик с ID {cid} не найден.")
            return
        
        visits_count = len(data["customers"][cid].get("visits", []))
        data["customers"][cid]["visits"] = []
        recalc_discount(data["customers"][cid])
        save_data()
        await update.message.reply_text(f"✅ История выездов заказчика {cid} очищена. Удалено записей: {visits_count}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /clearvisits <id_заказчика>")

# ====== MAIN ======

async def post_init(application: Application) -> None:
    """Скрывает список команд у клиентов и оставляет команды только админу.

    /start как системную команду Telegram полностью запретить нельзя (пользователь может ввести вручную),
    но можно убрать её из меню команд, чтобы клиенты работали только через кнопки.
    """

    # Для всех пользователей: не показываем никаких команд (включая /start)
    await application.bot.set_my_commands([], scope=BotCommandScopeDefault())

    # Для админа: можно оставить нужные команды в списке (по желанию)
    await application.bot.set_my_commands(
        [
            BotCommand("admin", "Панель администратора"),
            BotCommand("create", "Создать заказчика"),
            BotCommand("link", "Привязать пользователя"),
            BotCommand("unlink", "Отвязать пользователя"),
            BotCommand("addvisit", "Добавить выезд"),
            BotCommand("finduser", "Найти заказчика по ID"),
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )
def main():
    load_data()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("create", cmd_create))
    app.add_handler(CommandHandler("addvist", cmd_admin))  # алиас: открыть админ-панель
    # Вход в админ-панель кнопкой
    app.add_handler(CallbackQueryHandler(admin_quick_link, pattern=r"^admin_quick_link:"))
    app.add_handler(CallbackQueryHandler(admin_open_panel, pattern=r"^admin_panel"))
    
    # УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ДЛЯ ВСЕХ КНОПОК menu:
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_date_choice, pattern=r"^date:"))
    app.add_handler(CallbackQueryHandler(on_time_choice, pattern=r"^time:"))
    
    # Важно: админ-панель открывается отдельным хендлером (admin_open_panel).
    # Поэтому ConversationHandler должен уметь стартовать по кликам в админке,
    # иначе кнопки будут «иногда» не реагировать (когда диалог не был начат).
    #
    # Стартуем диалог админки по:
    #  - выбору заказчика / созданию / поиску
    #  - любым действиям admin_action:* (например «Пользователи»)
    #  - операциям удаления/отвязки/сумм проектов
    conv_handler = ConversationHandler(
        allow_reentry=True,
        entry_points=[
            CommandHandler('admin', cmd_admin),
            CommandHandler('addvist', cmd_admin),
            CommandHandler('addvisit', cmd_admin),
            CallbackQueryHandler(admin_open_panel, pattern=r'^admin_panel'),
            CallbackQueryHandler(
                admin_select_customer,
                pattern=r'^(admin_customer:\d+|admin_create_customer|admin_find_customer|admin_cancel)$'
            ),
            CallbackQueryHandler(
                admin_select_action,
                pattern=r'^(admin_action:|admin_delete_|admin_unlink_specific:|admin_add_amount:|admin_reset_sum:|admin_link_user:|admin_set_exact:|admin_user_info:|admin_visit_info:)'
            ),
        ],
        states={
            SELECT_CUSTOMER: [CallbackQueryHandler(admin_select_customer, pattern=r'^admin_')],
            CREATE_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_create_customer_handler)],
            FIND_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_find_customer_handler)],
            SELECT_ACTION: [CallbackQueryHandler(admin_select_action, pattern=r'^admin_')],
            LINK_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_link_user_handler)],
            SET_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_sum_handler)],
            SELECT_DATE: [CallbackQueryHandler(admin_select_date, pattern=r'^admin_date:')],
            SELECT_KIND: [CallbackQueryHandler(admin_select_kind, pattern=r'^admin_kind:')],
            SELECT_DURATION: [CallbackQueryHandler(admin_select_duration, pattern=r'^admin_duration:')],
            SELECT_TARIFF_TYPE: [CallbackQueryHandler(admin_select_tariff_type, pattern=r'^admin_tariff:')],
            CONFIRM_VISIT: [CallbackQueryHandler(admin_confirm_visit, pattern=r'^admin_confirm:')],
        },
        fallbacks=[CallbackQueryHandler(admin_cancel, pattern=r'^admin_cancel')],
    )
    
    app.add_handler(conv_handler)
    
    # Текстовые команды администратора
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("customers", cmd_customers))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CommandHandler("addsum", cmd_addsum))
    app.add_handler(CommandHandler("setsum", cmd_setsum))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("finduser", cmd_finduser))
    app.add_handler(CommandHandler("clearvisits", cmd_clearvisits))
    
    logging.info("Бот запущен.")

    # --- RUN MODE: polling locally, webhook on hosting (Railway, etc.) ---
    port = int(os.getenv("PORT", "0") or "0")
    public_url = (os.getenv("PUBLIC_URL") or os.getenv("WEBHOOK_URL") or "").strip().rstrip("/")
    secret_token = (os.getenv("WEBHOOK_SECRET") or "").strip() or None

    # If PUBLIC_URL provided OR Railway set PORT, run webhook; else polling.
    if public_url and port:
        # Use token as URL path (simple & safe enough); Telegram will call /<TOKEN>
        url_path = TOKEN
        webhook_url = f"{public_url}/{url_path}"
        logging.info("Запуск в режиме WEBHOOK: %s (port=%s)", webhook_url, port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url,
            secret_token=secret_token,
            drop_pending_updates=True,
        )
    else:
        logging.info("Запуск в режиме POLLING (локально).")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
