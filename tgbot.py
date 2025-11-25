import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import itertools
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

# --------------------------- НАСТРОЙКИ ---------------------------

# ОБЯЗАТЕЛЬНО: замените токен на НОВЫЙ, сгенерированный в BotFather!
BOT_TOKEN = "8498624987:AAE9A-DK5riBJ1H513nRoUhlzjD6uUR7Cwo"

# ID админов
ADMINS = {1731199152, 8260773398, 7209896378}

ADMIN_LABELS = {
    1731199152: "Админ 1",
    8260773398: "Админ 2",
    7209896378: "Админ 3",
}

# Сервисы и цены по умолчанию
services: Dict[str, Dict] = {
    "max":   {"title": "MAX",          "price": 3.1},
    "gmail": {"title": "Gmail",        "price": 0.7},
    "tg_nr": {"title": "Telegram ne reg", "price": 1.7},
    "tg_r":  {"title": "Telegram reg", "price": 2.0},
    "mamba": {"title": "MAMBA",        "price": 0.4},
    "vk":    {"title": "VK",           "price": 1.35},
}

# --------------------------- МОДЕЛИ ДАННЫХ ---------------------------

@dataclass
class Order:
    id: int
    user_id: int
    username: Optional[str]
    phone: str
    service_key: str
    status: str = "new"  # new, taken, code_requested, accepted, not_accepted, canceled
    code: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    price: float = 0.0   # фиксируем цену на момент сдачи
    assigned_admin_id: Optional[int] = None
    assigned_admin_username: Optional[str] = None
    assigned_at: Optional[datetime] = None


@dataclass
class Withdrawal:
    id: int
    user_id: int
    username: Optional[str]
    amount: float
    created_at: datetime = field(default_factory=datetime.now)
    status: str = "pending"  # pending, paid, rejected
    successful_orders_count: int = 0


@dataclass
class CodeRequest:
    order_id: int
    request_message_id: int
    expires_at: datetime


# --------------------------- ГЛОБАЛЬНЫЕ ХРАНИЛКИ ---------------------------

# Все заявки по id
orders: Dict[int, Order] = {}
order_id_counter = itertools.count(1)

# Сообщения админам по каждой заявке: order_id -> list of (chat_id, message_id)
order_admin_messages: Dict[int, List[Tuple[int, int]]] = {}

# Список заявок по пользователю
user_orders: Dict[int, List[int]] = {}

# Баланс пользователей
user_balances: Dict[int, float] = {}

# Перерыв глобальный
break_mode: bool = False

# Перерыв по сервисам: service_key -> bool
service_breaks: Dict[str, bool] = {}

# Ожидание ввода номера после выбора сервиса: user_id -> service_key
user_pending_service: Dict[int, str] = {}

# Ожидание кода от пользователя: user_id -> CodeRequest
waiting_code_for_user: Dict[int, CodeRequest] = {}

# Ожидание новой цены от админа: admin_id -> service_key
admin_waiting_price: Dict[int, str] = {}

# Добавление нового сервиса админом
admin_add_service_stage: Dict[int, str] = {}        # 'name' или 'price'
admin_add_service_temp_name: Dict[int, str] = {}

# Выводы средств
withdrawals: Dict[int, Withdrawal] = {}
withdrawal_id_counter = itertools.count(1)
user_withdrawals: Dict[int, List[int]] = {}
withdraw_admin_messages: Dict[int, List[Tuple[int, int]]] = {}


# --------------------------- УТИЛИТЫ ---------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def get_main_keyboard(is_admin_flag: bool) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="➕ Сдать номер")],
        [KeyboardButton(text="📋 Мои номера"), KeyboardButton(text="💰 Баланс")],
        [KeyboardButton(text="📤 Мои выводы")],
        [KeyboardButton(text="☎️ Связь с админом")],
    ]
    if is_admin_flag:
        buttons.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )


def get_services_inline_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, data in services.items():
        title = data["title"]
        price = data["price"]
        paused = service_breaks.get(key, False)
        if paused:
            text = f"{title} ({price}$) ⏸"
        else:
            text = f"{title} ({price}$)"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"service:{key}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_service")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_balance_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Вывод", callback_data="user:withdraw")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="user:back_main")],
        ]
    )


def get_admin_panel_kb() -> InlineKeyboardMarkup:
    global break_mode
    break_text = "⏸ Перерыв (все сервисы)" if not break_mode else "▶️ Выход из перерыва (все)"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=break_text, callback_data="admin:toggle_break")],
            [InlineKeyboardButton(text="⏸ Перерывы по сервисам", callback_data="admin:svc_breaks")],
            [InlineKeyboardButton(text="💵 Изменить цены", callback_data="admin:prices")],
            [InlineKeyboardButton(text="➕ Добавить сервис", callback_data="admin:add_service")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="📞 Номера", callback_data="admin:orders")],
            [InlineKeyboardButton(text="💸 Выводы", callback_data="admin:withdraws")],
        ]
    )


def get_admin_take_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять", callback_data=f"order_take:{order_id}")],
            [InlineKeyboardButton(text="🚫 Не брать", callback_data=f"order_nottake:{order_id}")],
        ]
    )


def get_admin_status_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📨 Запрошен код", callback_data=f"status:{order_id}:code")],
            [
                InlineKeyboardButton(text="✅ Встал", callback_data=f"status:{order_id}:ok"),
                InlineKeyboardButton(text="❌ Не встал", callback_data=f"status:{order_id}:bad"),
            ],
            [InlineKeyboardButton(text="🚫 Отменён", callback_data=f"status:{order_id}:cancel")],
            [InlineKeyboardButton(text="🔁 Переназначить", callback_data=f"order_reassign:{order_id}")],
        ]
    )


def get_withdraw_admin_kb(wid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Отклонить выплату", callback_data=f"wd_status:{wid}:rej"),
                InlineKeyboardButton(text="✅ Выплачено", callback_data=f"wd_status:{wid}:paid"),
            ]
        ]
    )


def format_status(status: str) -> str:
    mapping = {
        "new": "новый",
        "taken": "в работе",
        "code_requested": "запрошен код",
        "accepted": "встал (успешно)",
        "not_accepted": "не встал",
        "canceled": "отменён",
    }
    return mapping.get(status, status)


def format_withdraw_status(status: str) -> str:
    mapping = {
        "pending": "в обработке",
        "paid": "выплачено",
        "rejected": "отклонено",
    }
    return mapping.get(status, status)


def get_user_mention_by(uid: int, username: Optional[str]) -> str:
    if username:
        return f"@{username}"
    else:
        return f'<a href="tg://user?id={uid}">профиль</a>'


def get_user_mention(order: Order) -> str:
    return get_user_mention_by(order.user_id, order.username)


def get_admin_label(admin_id: int) -> str:
    label = ADMIN_LABELS.get(admin_id, "Админ")
    return f"{label} (ID {admin_id})"


# --------------------------- ИНИЦИАЛИЗАЦИЯ БОТА ---------------------------

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()


# --------------------------- ТАЙМАУТ ДЛЯ КОДОВ ---------------------------

async def code_request_timeout(order_id: int, user_id: int):
    await asyncio.sleep(180)  # 3 минуты
    order = orders.get(order_id)
    if not order:
        return
    # Если статус уже не code_requested — ничего не делаем
    if order.status != "code_requested":
        return

    cr = waiting_code_for_user.get(user_id)
    if not cr or cr.order_id != order_id:
        return

    # Время истекло — отменяем заявку
    waiting_code_for_user.pop(user_id, None)
    order.status = "canceled"

    service_name = services[order.service_key]["title"]

    try:
        await bot.send_message(
            user_id,
            "⏰ Время на ввод кода истекло, заявка автоматически <b>отменена</b>.\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>"
        )
    except Exception:
        pass

    # Обновляем сообщения у админов
    for chat_id, msg_id in order_admin_messages.get(order_id, []):
        try:
            new_text = (
                "📥 Заявка обновлена (авто-отмена по таймеру)\n\n"
                f"ID: <b>{order.id}</b>\n"
                f"Сервис: <b>{service_name}</b>\n"
                f"Номер: <code>{order.phone}</code>\n"
                f"Пользователь: {get_user_mention(order)}\n"
                f"Текущий статус: <b>{format_status(order.status)}</b>"
            )
            await bot.edit_message_text(
                new_text,
                chat_id=chat_id,
                message_id=msg_id
            )
        except Exception:
            pass


# --------------------------- ХЕНДЛЕРЫ ---------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    uid = user.id
    is_admin_flag = is_admin(uid)

    kb = get_main_keyboard(is_admin_flag)
    text = (
        "👋 Добро пожаловать!\n\n"
        "Здесь вы можете безопасно сдавать номера +7 для разных сервисов:\n"
        "• MAX\n• Gmail\n• Telegram (reg / ne reg)\n• MAMBA\n• VK\n\n"
        "Выберите действие в меню ниже 👇"
    )
    await message.answer(text, reply_markup=kb)


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return

    await message.answer(
        "🛠 Админ-панель",
        reply_markup=get_admin_panel_kb()
    )


# --------------------------- ОБЩИЙ TEXT-ХЕНДЛЕР ---------------------------

@dp.message()
async def handle_text(message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    # 1. Ввод кода (только ответом на сообщение с запросом кода)
    if not text.startswith("/") and uid in waiting_code_for_user:
        cr = waiting_code_for_user[uid]
        if message.reply_to_message and message.reply_to_message.message_id == cr.request_message_id:
            # Проверяем срок
            if datetime.now() > cr.expires_at:
                waiting_code_for_user.pop(uid, None)
                await message.answer("⏰ Время на ввод кода уже истекло, заявка отменена.")
                return

            order = orders.get(cr.order_id)
            if not order:
                waiting_code_for_user.pop(uid, None)
                await message.answer("Что-то пошло не так, заявка не найдена.")
                return

            order.code = text
            waiting_code_for_user.pop(uid, None)

            service_name = services[order.service_key]["title"]

            # Отправляем код админам
            for admin_id in ADMINS:
                msg_for_admin = (
                    "🔐 Новый код от пользователя\n\n"
                    f"Пользователь: {get_user_mention(order)}\n"
                    f"Сервис: <b>{service_name}</b>\n"
                    f"Номер: <code>{order.phone}</code>\n"
                    f"Код: <b>{order.code}</b>"
                )
                try:
                    await bot.send_message(admin_id, msg_for_admin)
                except Exception:
                    pass

            await message.answer("✅ Код отправлен администратору. Ожидайте решение.")
            return
        # Если не reply на нужное сообщение — идём дальше по логике

    # 2. Ожидаем новую цену от админа
    if not text.startswith("/") and uid in admin_waiting_price:
        if not is_admin(uid):
            admin_waiting_price.pop(uid, None)
            await message.answer("⛔ Вы не администратор.")
            return

        service_key = admin_waiting_price[uid]
        service_name = services[service_key]["title"]
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            await message.answer("❗ Введите корректное число, например: 1.75")
            return

        services[service_key]["price"] = value
        admin_waiting_price.pop(uid, None)
        await message.answer(f"💵 Цена для сервиса <b>{service_name}</b> обновлена: <b>{value}$</b>.")
        return

    # 3. Добавление нового сервиса админом
    if not text.startswith("/") and uid in admin_add_service_stage:
        if not is_admin(uid):
            admin_add_service_stage.pop(uid, None)
            admin_add_service_temp_name.pop(uid, None)
            await message.answer("⛔ Вы не администратор.")
            return

        stage = admin_add_service_stage[uid]
        if stage == "name":
            name = text.strip()
            if not name:
                await message.answer("❗ Название сервиса не может быть пустым. Введите ещё раз:")
                return
            admin_add_service_temp_name[uid] = name
            admin_add_service_stage[uid] = "price"
            await message.answer(
                f"Название сервиса: <b>{name}</b>\nТеперь введите цену за номер в $ (например, <code>1.50</code>):"
            )
            return
        elif stage == "price":
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                await message.answer("❗ Введите корректное число, например: 1.75")
                return

            name = admin_add_service_temp_name.get(uid, "Service")
            # Генерируем ключ
            base = "".join(ch.lower() for ch in name if ch.isalnum() or ch == "_") or "srv"
            key = base
            i = 1
            while key in services:
                key = f"{base}{i}"
                i += 1

            services[key] = {"title": name, "price": value}

            admin_add_service_stage.pop(uid, None)
            admin_add_service_temp_name.pop(uid, None)

            await message.answer(
                f"✅ Новый сервис добавлен:\nНазвание: <b>{name}</b>\nЦена: <b>{value}$</b>."
            )
            return

    # 4. Ожидаем ввод номера после выбора сервиса
    if not text.startswith("/") and uid in user_pending_service:
        global break_mode
        if break_mode:
            user_pending_service.pop(uid, None)
            await message.answer("⏸ Сейчас приём номеров на паузе. Попробуйте позже.")
            return

        service_key = user_pending_service.pop(uid)
        # Проверка перерыва по сервису
        if service_breaks.get(service_key, False):
            await message.answer("⏸ По этому сервису сейчас перерыв. Попробуйте позже.")
            return

        phone_raw = text
        digits = "".join(ch for ch in phone_raw if ch.isdigit())
        if not (digits.startswith("7") and len(digits) == 11):
            await message.answer(
                "❗ Пожалуйста, введите номер в формате <b>+7XXXXXXXXXX</b>.\n"
                "Например: <code>+79991234567</code>"
            )
            user_pending_service[uid] = service_key
            return

        phone = "+7" + digits[1:]

        # Создаём заявку
        new_id = next(order_id_counter)
        user = message.from_user
        username = user.username

        price_now = services[service_key]["price"]

        order = Order(
            id=new_id,
            user_id=uid,
            username=username,
            phone=phone,
            service_key=service_key,
            price=price_now,
        )
        orders[new_id] = order
        user_orders.setdefault(uid, []).append(new_id)

        service_name = services[service_key]["title"]

        await message.answer(
            "📥 Ваш номер отправлен на проверку.\n\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{phone}</code>\n"
            f"Цена: <b>{price_now}$</b>\n\n"
            "Ожидайте, пока администратор возьмёт номер в работу."
        )

        # Уведомляем админов (с кнопками Взять / Не брать)
        msgs = []
        for admin_id in ADMINS:
            try:
                mention = get_user_mention(order)
                text_admin = (
                    "📥 <b>Новый номер</b>\n\n"
                    f"Сервис: <b>{service_name}</b>\n"
                    f"Номер: <code>{phone}</code>\n"
                    f"Цена: <b>{price_now}$</b>\n"
                    f"От пользователя: {mention}\n"
                    f"ID заявки: <code>{new_id}</code>"
                )
                msg = await bot.send_message(
                    admin_id,
                    text_admin,
                    reply_markup=get_admin_take_kb(new_id)
                )
                msgs.append((admin_id, msg.message_id))
            except Exception:
                pass

        if msgs:
            order_admin_messages[new_id] = msgs

        return

    # 5. Кнопки основного меню
    if text == "➕ Сдать номер":
        global break_mode
        if break_mode:
            await message.answer("⏸ Сейчас приём номеров на паузе. Попробуйте позже.")
            return
        await message.answer(
            "Выберите сервис, для которого сдаёте номер:",
            reply_markup=get_services_inline_kb()
        )
        return

    if text == "📋 Мои номера":
        user_order_ids = user_orders.get(uid, [])
        # Фильтруем только те, которые ещё не взяты / не обработаны (status == new)
        pending = []
        for oid in user_order_ids:
            o = orders.get(oid)
            if not o:
                continue
            if o.status == "new":
                pending.append(o)

        if not pending:
            await message.answer("📋 У вас нет номеров, которые ещё не были взяты админами.")
            return

        lines = ["📋 <b>Ваши активные номера</b>:\n"]
        for o in pending[-20:]:
            dt = o.created_at.strftime("%d.%m %H:%M")
            service_name = services[o.service_key]["title"]
            lines.append(
                f"#{o.id} | {dt} | {service_name} | <code>{o.phone}</code>"
            )

        await message.answer("\n".join(lines))
        return

    if text == "💰 Баланс":
        balance = user_balances.get(uid, 0.0)
        await message.answer(
            f"💰 Ваш баланс: <b>{balance:.2f}$</b>",
            reply_markup=get_balance_kb()
        )
        return

    if text == "📤 Мои выводы":
        wids = user_withdrawals.get(uid, [])
        if not wids:
            await message.answer("📤 У вас пока нет заявок на вывод средств.")
            return

        lines = ["📤 <b>Ваши выводы</b>:\n"]
        for wid in wids[-20:]:
            w = withdrawals.get(wid)
            if not w:
                continue
            dt = w.created_at.strftime("%d.%m %H:%M")
            line = f"#{w.id} | {dt} | сумма: <b>{w.amount:.2f}$</b>"
            if w.status in ("paid", "rejected"):
                line += f" | статус: <b>{format_withdraw_status(w.status)}</b>"
            lines.append(line)

        await message.answer("\n".join(lines))
        return

    if text == "☎️ Связь с админом":
        await message.answer(
            "📨 Для связи с администратором напишите сюда свой вопрос.\n\n"
            "Админ увидит ваш username / ID и сможет с вами связаться."
        )
        return

    if text == "🛠 Админ-панель":
        if not is_admin(uid):
            await message.answer("⛔ У вас нет доступа к админ-панели.")
            return
        await message.answer("🛠 Админ-панель", reply_markup=get_admin_panel_kb())
        return

    # Если ничего не подошло — просто подсказка
    await message.answer("❓ Я не понял команду. Используйте меню кнопок ниже.")


# --------------------------- CALLBACK-ХЕНДЛЕРЫ ---------------------------

@dp.callback_query(F.data == "cancel_service")
async def cancel_service(call: CallbackQuery):
    uid = call.from_user.id
    user_pending_service.pop(uid, None)
    await call.message.edit_text("❌ Выбор сервиса отменён.")
    await call.answer()


@dp.callback_query(F.data.startswith("service:"))
async def choose_service(call: CallbackQuery):
    global break_mode
    if break_mode:
        await call.answer("Сейчас приём номеров на паузе.", show_alert=True)
        return

    uid = call.from_user.id
    _, service_key = call.data.split(":", 1)

    if service_key not in services:
        await call.answer("Неизвестный сервис.", show_alert=True)
        return

    if service_breaks.get(service_key, False):
        await call.answer("По этому сервису сейчас перерыв.", show_alert=True)
        return

    user_pending_service[uid] = service_key
    service_name = services[service_key]["title"]

    await call.message.edit_text(
        f"Вы выбрали сервис: <b>{service_name}</b>\n\n"
        "Теперь отправьте номер в формате <b>+7XXXXXXXXXX</b> одним сообщением."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("user:"))
async def user_balance_actions(call: CallbackQuery):
    uid = call.from_user.id
    action = call.data.split(":", 1)[1]

    if action == "withdraw":
        balance = user_balances.get(uid, 0.0)
        if balance <= 0:
            await call.answer("Недостаточно средств для вывода.", show_alert=True)
            return

        # Считаем количество успешно сданных номеров
        success_count = sum(
            1 for o in orders.values()
            if o.user_id == uid and o.status == "accepted"
        )

        wid = next(withdrawal_id_counter)
        username = call.from_user.username
        w = Withdrawal(
            id=wid,
            user_id=uid,
            username=username,
            amount=balance,
            successful_orders_count=success_count,
        )
        withdrawals[wid] = w
        user_withdrawals.setdefault(uid, []).append(wid)

        # Обнуляем баланс
        user_balances[uid] = 0.0

        await call.message.edit_text(
            f"📤 Заявка на вывод создана.\n"
            f"Сумма: <b>{w.amount:.2f}$</b>\n"
            f"Ваш баланс обнулён."
        )
        await call.answer("Заявка на вывод отправлена администратору.")

        # Уведомляем админов
        msgs = []
        user_mention = get_user_mention_by(uid, username)
        for admin_id in ADMINS:
            try:
                text_admin = (
                    "💸 <b>Новая заявка на вывод</b>\n\n"
                    f"Пользователь: {user_mention}\n"
                    f"ID пользователя: <code>{uid}</code>\n"
                    f"Успешных номеров: <b>{success_count}</b>\n"
                    f"Сумма к выводу: <b>{w.amount:.2f}$</b>\n"
                    f"ID заявки: <code>{w.id}</code>"
                )
                msg = await bot.send_message(
                    admin_id,
                    text_admin,
                    reply_markup=get_withdraw_admin_kb(w.id)
                )
                msgs.append((admin_id, msg.message_id))
            except Exception:
                pass

        if msgs:
            withdraw_admin_messages[w.id] = msgs

    elif action == "back_main":
        is_admin_flag = is_admin(uid)
        await call.message.edit_text(
            "Главное меню обновлено. Используйте клавиатуру ниже 👇"
        )
        await bot.send_message(
            uid,
            "Вы в главном меню.",
            reply_markup=get_main_keyboard(is_admin_flag)
        )
        await call.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def admin_panel_actions(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    action = call.data.split(":", 1)[1]

    if action == "toggle_break":
        global break_mode
        break_mode = not break_mode
        if break_mode:
            msg = "⏸ Режим перерыва включён. Новые номера принять нельзя."
        else:
            msg = "▶️ Режим перерыва выключен. Приём номеров возобновлён."
        await call.message.edit_text(msg, reply_markup=get_admin_panel_kb())
        await call.answer("Статус перерыва изменён.")
        return

    if action == "svc_breaks":
        lines = ["⏸ <b>Перерывы по сервисам</b>:\n"]
        kb_rows = []
        for key, data in services.items():
            paused = service_breaks.get(key, False)
            state = "перерыв" if paused else "работает"
            lines.append(f"{data['title']}: <b>{state}</b>")
            kb_rows.append(
                [InlineKeyboardButton(
                    text=f"{data['title']} ({state})",
                    callback_data=f"svc_break:{key}"
                )]
            )
        kb_rows.append(
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")]
        )
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await call.message.edit_text("\n".join(lines), reply_markup=kb)
        await call.answer()
        return

    if action == "prices":
        lines = ["💵 <b>Текущие цены</b>:\n"]
        for key, data in services.items():
            lines.append(f"{data['title']}: <b>{data['price']}$</b>")

        kb_rows = []
        for key, data in services.items():
            kb_rows.append(
                [InlineKeyboardButton(text=data["title"], callback_data=f"price:{key}")]
            )
        kb_rows.append(
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")]
        )

        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await call.message.edit_text("\n".join(lines), reply_markup=kb)
        await call.answer()
        return

    if action == "stats":
        successful = [o for o in orders.values() if o.status == "accepted"]
        if not successful:
            await call.message.edit_text("📊 Пока нет успешно сданных номеров.", reply_markup=get_admin_panel_kb())
            await call.answer()
            return

        lines = [f"📊 <b>Статистика</b>\nВсего успешно: <b>{len(successful)}</b>\n"]
        for o in successful:
            dt = o.created_at.strftime("%d.%m %H:%M")
            service_name = services[o.service_key]["title"]
            mention = get_user_mention(o)
            lines.append(
                f"#{o.id} | {dt} | {service_name} | <code>{o.phone}</code> | "
                f"пользователь: {mention} | цена: <b>{o.price}$</b>"
            )

        await call.message.edit_text("\n".join(lines), reply_markup=get_admin_panel_kb())
        await call.answer()
        return

    if action == "orders":
        active_orders = [
            o for o in orders.values()
            if o.status in ("new", "taken")
        ]
        if not active_orders:
            await call.message.edit_text("📞 Активных номеров нет.", reply_markup=get_admin_panel_kb())
            await call.answer()
            return

        lines = ["📞 <b>Активные номера</b>:\n"]
        kb_rows = []
        for o in sorted(active_orders, key=lambda x: x.created_at)[-30:]:
            dt = o.created_at.strftime("%d.%m %H:%M")
            service_name = services[o.service_key]["title"]
            lines.append(
                f"#{o.id} | {dt} | {service_name} | <code>{o.phone}</code> | пользователь: {get_user_mention(o)} | статус: <b>{format_status(o.status)}</b>"
            )
            kb_rows.append(
                [InlineKeyboardButton(
                    text=f"Открыть #{o.id}",
                    callback_data=f"admin_open_order:{o.id}"
                )]
            )
        kb_rows.append(
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")]
        )
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await call.message.edit_text("\n".join(lines), reply_markup=kb)
        await call.answer()
        return

    if action == "withdraws":
        if not withdrawals:
            await call.message.edit_text("💸 Заявок на вывод пока нет.", reply_markup=get_admin_panel_kb())
            await call.answer()
            return

        lines = ["💸 <b>Заявки на вывод</b>:\n"]
        kb_rows = []
        for w in sorted(withdrawals.values(), key=lambda x: x.created_at)[-30:]:
            dt = w.created_at.strftime("%d.%m %H:%M")
            mention = get_user_mention_by(w.user_id, w.username)
            lines.append(
                f"#{w.id} | {dt} | {mention} | сумма: <b>{w.amount:.2f}$</b> | статус: <b>{format_withdraw_status(w.status)}</b>"
            )
            kb_rows.append(
                [InlineKeyboardButton(
                    text=f"Открыть вывод #{w.id}",
                    callback_data=f"wd_open:{w.id}"
                )]
            )
        kb_rows.append(
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")]
        )
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await call.message.edit_text("\n".join(lines), reply_markup=kb)
        await call.answer()
        return

    if action == "add_service":
        admin_add_service_stage[uid] = "name"
        await call.message.edit_text(
            "➕ Добавление нового сервиса.\n\n"
            "Введите название сервиса (как его увидят пользователи):"
        )
        await call.answer()
        return

    if action == "back":
        await call.message.edit_text("🛠 Админ-панель", reply_markup=get_admin_panel_kb())
        await call.answer()
        return


@dp.callback_query(F.data.startswith("svc_break:"))
async def svc_break_toggle(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, service_key = call.data.split(":", 1)
    if service_key not in services:
        await call.answer("Неизвестный сервис.", show_alert=True)
        return

    current = service_breaks.get(service_key, False)
    service_breaks[service_key] = not current

    # Обновляем список перерывов
    lines = ["⏸ <b>Перерывы по сервисам</b>:\n"]
    kb_rows = []
    for key, data in services.items():
        paused = service_breaks.get(key, False)
        state = "перерыв" if paused else "работает"
        lines.append(f"{data['title']}: <b>{state}</b>")
        kb_rows.append(
            [InlineKeyboardButton(
                text=f"{data['title']} ({state})",
                callback_data=f"svc_break:{key}"
            )]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await call.message.edit_text("\n".join(lines), reply_markup=kb)
    await call.answer("Статус сервиса обновлён.")


@dp.callback_query(F.data.startswith("price:"))
async def admin_change_price(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, service_key = call.data.split(":", 1)
    if service_key not in services:
        await call.answer("Неизвестный сервис.", show_alert=True)
        return

    service_name = services[service_key]["title"]
    admin_waiting_price[uid] = service_key

    await call.message.edit_text(
        f"Введите новую цену для сервиса <b>{service_name}</b> в $.\n"
        "Например: <code>1.75</code>"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("order_take:"))
async def order_take(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, oid_str = call.data.split(":", 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        await call.answer("Некорректный ID заявки.", show_alert=True)
        return

    order = orders.get(order_id)
    if not order:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    if order.assigned_admin_id and order.assigned_admin_id != uid:
        await call.answer("Эта заявка уже взята другим админом.", show_alert=True)
        return

    order.assigned_admin_id = uid
    order.assigned_admin_username = call.from_user.username
    order.assigned_at = datetime.now()
    order.status = "taken"

    service_name = services[order.service_key]["title"]

    # Уведомляем пользователя
    try:
        await bot.send_message(
            order.user_id,
            "👨‍💻 Ваш номер взят в работу администратором.\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>"
        )
    except Exception:
        pass

    # Обновляем сообщения у всех админов
    taken_time = order.assigned_at.strftime("%d.%m %H:%M")
    taker_label = get_admin_label(uid)
    for chat_id, msg_id in order_admin_messages.get(order_id, []):
        try:
            if chat_id == uid:
                # Для взявшего — кнопки статусов
                new_text = (
                    "📥 Заявка взята вами в работу\n\n"
                    f"ID: <b>{order.id}</b>\n"
                    f"Сервис: <b>{service_name}</b>\n"
                    f"Номер: <code>{order.phone}</code>\n"
                    f"Пользователь: {get_user_mention(order)}\n"
                    f"Статус: <b>{format_status(order.status)}</b>\n"
                    f"Вы взяли: <b>{taken_time}</b>"
                )
                await bot.edit_message_text(
                    new_text,
                    chat_id=chat_id,
                    message_id=msg_id,
                    reply_markup=get_admin_status_kb(order.id)
                )
            else:
                # Для остальных — информация, кто взял
                new_text = (
                    "📥 Заявка уже взята другим администратором\n\n"
                    f"ID: <b>{order.id}</b>\n"
                    f"Сервис: <b>{service_name}</b>\n"
                    f"Номер: <code>{order.phone}</code>\n"
                    f"Пользователь: {get_user_mention(order)}\n"
                    f"В работе у: <b>{taker_label}</b> с <b>{taken_time}</b>"
                )
                await bot.edit_message_text(
                    new_text,
                    chat_id=chat_id,
                    message_id=msg_id
                )
        except Exception:
            pass

    await call.answer("Заявка взята в работу.")


@dp.callback_query(F.data.startswith("order_nottake:"))
async def order_nottake(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, oid_str = call.data.split(":", 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        await call.answer("Некорректный ID заявки.", show_alert=True)
        return

    order = orders.get(order_id)
    if not order:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    order.status = "canceled"
    service_name = services[order.service_key]["title"]

    # Уведомляем пользователя
    try:
        await bot.send_message(
            order.user_id,
            "🚫 Ваш номер был отменён администратором.\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>"
        )
    except Exception:
        pass

    # Обновляем сообщения у админов
    for chat_id, msg_id in order_admin_messages.get(order_id, []):
        try:
            new_text = (
                "📥 Заявка обновлена (не взята и отменена)\n\n"
                f"ID: <b>{order.id}</b>\n"
                f"Сервис: <b>{service_name}</b>\n"
                f"Номер: <code>{order.phone}</code>\n"
                f"Пользователь: {get_user_mention(order)}\n"
                f"Статус: <b>{format_status(order.status)}</b>"
            )
            await bot.edit_message_text(
                new_text,
                chat_id=chat_id,
                message_id=msg_id
            )
        except Exception:
            pass

    await call.answer("Заявка отменена.")


@dp.callback_query(F.data.startswith("order_reassign:"))
async def order_reassign(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, oid_str = call.data.split(":", 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        await call.answer("Некорректный ID заявки.", show_alert=True)
        return

    order = orders.get(order_id)
    if not order:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    if order.assigned_admin_id != uid:
        await call.answer("Вы не являетесь ответственным за эту заявку.", show_alert=True)
        return

    kb_rows = []
    for admin_id in ADMINS:
        if admin_id == uid:
            continue
        label = get_admin_label(admin_id)
        kb_rows.append(
            [InlineKeyboardButton(
                text=label,
                callback_data=f"order_reassign_to:{order_id}:{admin_id}"
            )]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_open_order:{order_id}")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await call.message.edit_reply_markup(reply_markup=kb)
    await call.answer("Выберите, кому переназначить.")


@dp.callback_query(F.data.startswith("order_reassign_to:"))
async def order_reassign_to(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, oid_str, new_admin_str = call.data.split(":", 2)
    try:
        order_id = int(oid_str)
        new_admin_id = int(new_admin_str)
    except ValueError:
        await call.answer("Некорректные данные.", show_alert=True)
        return

    if new_admin_id not in ADMINS:
        await call.answer("Некорректный админ.", show_alert=True)
        return

    order = orders.get(order_id)
    if not order:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    if order.assigned_admin_id != uid:
        await call.answer("Вы не являетесь ответственным за эту заявку.", show_alert=True)
        return

    order.assigned_admin_id = new_admin_id
    order.assigned_admin_username = None
    order.assigned_at = datetime.now()

    service_name = services[order.service_key]["title"]
    new_admin_label = get_admin_label(new_admin_id)
    taken_time = order.assigned_at.strftime("%d.%m %H:%M")

    # Сообщение новому админу
    try:
        msg = await bot.send_message(
            new_admin_id,
            "📥 Вам переназначена заявка\n\n"
            f"ID: <b>{order.id}</b>\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>\n"
            f"Пользователь: {get_user_mention(order)}\n"
            f"Статус: <b>{format_status(order.status)}</b>",
            reply_markup=get_admin_status_kb(order.id)
        )
        order_admin_messages.setdefault(order_id, []).append((new_admin_id, msg.message_id))
    except Exception:
        pass

    # Обновляем тексты у всех админов
    for chat_id, msg_id in order_admin_messages.get(order_id, []):
        try:
            if chat_id == new_admin_id:
                # уже отправили выше
                continue
            new_text = (
                "📥 Заявка переназначена\n\n"
                f"ID: <b>{order.id}</b>\n"
                f"Сервис: <b>{service_name}</b>\n"
                f"Номер: <code>{order.phone}</code>\n"
                f"Пользователь: {get_user_mention(order)}\n"
                f"Ответственный админ: <b>{new_admin_label}</b> с <b>{taken_time}</b>"
            )
            await bot.edit_message_text(
                new_text,
                chat_id=chat_id,
                message_id=msg_id
            )
        except Exception:
            pass

    await call.answer("Заявка переназначена.")


@dp.callback_query(F.data.startswith("admin_open_order:"))
async def admin_open_order(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, oid_str = call.data.split(":", 1)
    try:
        order_id = int(oid_str)
    except ValueError:
        await call.answer("Некорректный ID заявки.", show_alert=True)
        return

    order = orders.get(order_id)
    if not order:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    service_name = services[order.service_key]["title"]
    dt = order.created_at.strftime("%d.%m %H:%M")
    text = (
        "📥 <b>Заявка</b>\n\n"
        f"ID: <b>{order.id}</b>\n"
        f"Создана: <b>{dt}</b>\n"
        f"Сервис: <b>{service_name}</b>\n"
        f"Номер: <code>{order.phone}</code>\n"
        f"Пользователь: {get_user_mention(order)}\n"
        f"Статус: <b>{format_status(order.status)}</b>"
    )

    kb = None
    if order.status in ("new",):
        kb = get_admin_take_kb(order.id)
    elif order.status in ("taken", "code_requested"):
        if order.assigned_admin_id == uid:
            kb = get_admin_status_kb(order.id)
        elif order.assigned_admin_id:
            # Закреплена за другим
            text += f"\nОтветственный: <b>{get_admin_label(order.assigned_admin_id)}</b>"
        else:
            kb = get_admin_take_kb(order.id)

    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("status:"))
async def change_status(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, oid_str, action = call.data.split(":", 2)
    try:
        order_id = int(oid_str)
    except ValueError:
        await call.answer("Некорректный ID заявки.", show_alert=True)
        return

    order = orders.get(order_id)
    if not order:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    # Только ответственный админ может менять статусы
    if order.assigned_admin_id and order.assigned_admin_id != uid:
        await call.answer("Заявка закреплена за другим админом.", show_alert=True)
        return

    user_id = order.user_id
    service_name = services[order.service_key]["title"]

    # Обработка статусов
    if action == "code":
        order.status = "code_requested"
        # Отправляем запрос кода пользователю
        expires_at = datetime.now() + timedelta(minutes=3)
        msg = await bot.send_message(
            user_id,
            "📨 По вашему номеру <b>запрошен код</b>.\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>\n\n"
            "У вас есть <b>3 минуты</b>, чтобы ввести код.\n"
            "Пожалуйста, ответьте <b>на это сообщение</b>, отправив код одним сообщением."
        )
        waiting_code_for_user[user_id] = CodeRequest(
            order_id=order.id,
            request_message_id=msg.message_id,
            expires_at=expires_at,
        )
        asyncio.create_task(code_request_timeout(order.id, user_id))

        await call.answer("Статус обновлён: запрошен код.")
    elif action == "ok":
        if order.status == "accepted":
            await call.answer("Уже имеет статус 'встал'.", show_alert=True)
            return
        order.status = "accepted"
        # Начисление баланса
        user_balances[user_id] = user_balances.get(user_id, 0.0) + order.price

        await bot.send_message(
            user_id,
            "✅ Ваш номер <b>принят</b>.\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>\n\n"
            f"На ваш баланс начислено <b>{order.price}$</b>."
        )
        await call.answer("Статус обновлён: встал (успешно).")
    elif action == "bad":
        order.status = "not_accepted"
        await bot.send_message(
            user_id,
            "❌ Ваш номер не подошёл.\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>\n\n"
            "Начисление на баланс не производится."
        )
        await call.answer("Статус обновлён: не встал.")
    elif action == "cancel":
        order.status = "canceled"
        await bot.send_message(
            user_id,
            "🚫 Заявка по вашему номеру отменена администратором.\n"
            f"Сервис: <b>{service_name}</b>\n"
            f"Номер: <code>{order.phone}</code>"
        )
        await call.answer("Статус обновлён: отменён.")
    else:
        await call.answer("Неизвестное действие.", show_alert=True)
        return

    # Обновим текст сообщения у админов (покажем новый статус)
    for chat_id, msg_id in order_admin_messages.get(order.id, []):
        try:
            new_text = (
                "📥 Заявка обновлена\n\n"
                f"ID: <b>{order.id}</b>\n"
                f"Сервис: <b>{service_name}</b>\n"
                f"Номер: <code>{order.phone}</code>\n"
                f"Пользователь: {get_user_mention(order)}\n"
                f"Текущий статус: <b>{format_status(order.status)}</b>"
            )
            # Кнопки отображаем только ответственному админу и только если ещё есть смысл
            if order.status in ("accepted", "not_accepted", "canceled"):
                await bot.edit_message_text(
                    new_text,
                    chat_id=chat_id,
                    message_id=msg_id
                )
            else:
                if order.assigned_admin_id == chat_id:
                    await bot.edit_message_text(
                        new_text,
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=get_admin_status_kb(order.id)
                    )
                else:
                    await bot.edit_message_text(
                        new_text,
                        chat_id=chat_id,
                        message_id=msg_id
                    )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("wd_open:"))
async def wd_open(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, wid_str = call.data.split(":", 1)
    try:
        wid = int(wid_str)
    except ValueError:
        await call.answer("Некорректный ID заявки.", show_alert=True)
        return

    w = withdrawals.get(wid)
    if not w:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    dt = w.created_at.strftime("%d.%m %H:%M")
    mention = get_user_mention_by(w.user_id, w.username)
    text = (
        "💸 <b>Заявка на вывод</b>\n\n"
        f"ID: <b>{w.id}</b>\n"
        f"Дата: <b>{dt}</b>\n"
        f"Пользователь: {mention}\n"
        f"ID пользователя: <code>{w.user_id}</code>\n"
        f"Успешных номеров на момент заявки: <b>{w.successful_orders_count}</b>\n"
        f"Сумма: <b>{w.amount:.2f}$</b>\n"
        f"Статус: <b>{format_withdraw_status(w.status)}</b>"
    )
    await call.message.edit_text(text, reply_markup=get_withdraw_admin_kb(w.id))
    await call.answer()


@dp.callback_query(F.data.startswith("wd_status:"))
async def wd_status_change(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа.", show_alert=True)
        return

    _, wid_str, action = call.data.split(":", 2)
    try:
        wid = int(wid_str)
    except ValueError:
        await call.answer("Некорректный ID заявки.", show_alert=True)
        return

    w = withdrawals.get(wid)
    if not w:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    if action == "paid":
        w.status = "paid"
        # Уведомляем пользователя
        try:
            await bot.send_message(
                w.user_id,
                "✅ Ваша заявка на вывод средств <b>выплачена</b>.\n"
                f"Сумма: <b>{w.amount:.2f}$</b>"
            )
        except Exception:
            pass
        await call.answer("Статус вывода: выплачено.")
    elif action == "rej":
        w.status = "rejected"
        try:
            await bot.send_message(
                w.user_id,
                "❌ Ваша заявка на вывод средств <b>отклонена</b>.\n"
                "По вопросам обратитесь к администратору."
            )
        except Exception:
            pass
        await call.answer("Статус вывода: отклонено.")
    else:
        await call.answer("Неизвестное действие.", show_alert=True)
        return

    # Обновляем текст у всех админов, где есть эта заявка
    dt = w.created_at.strftime("%d.%m %H:%M")
    mention = get_user_mention_by(w.user_id, w.username)
    new_text = (
        "💸 <b>Заявка на вывод</b>\n\n"
        f"ID: <b>{w.id}</b>\n"
        f"Дата: <b>{dt}</b>\n"
        f"Пользователь: {mention}\n"
        f"ID пользователя: <code>{w.user_id}</code>\n"
        f"Успешных номеров на момент заявки: <b>{w.successful_orders_count}</b>\n"
        f"Сумма: <b>{w.amount:.2f}$</b>\n"
        f"Статус: <b>{format_withdraw_status(w.status)}</b>"
    )

    for chat_id, msg_id in withdraw_admin_messages.get(w.id, []):
        try:
            await bot.edit_message_text(
                new_text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=get_withdraw_admin_kb(w.id)
            )
        except Exception:
            pass


# --------------------------- MAIN ---------------------------

async def main():
    print("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())