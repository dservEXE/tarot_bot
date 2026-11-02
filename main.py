import asyncio
import calendar
import html
import aiosqlite
import logging
import os
import tempfile
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from config import get_settings

from db import (
    add_tokens,
    get_all_users_for_notifications,
    get_tokens,
    get_user,
    get_user_by_username,
    init_db,
    reset_profile,
    spend_tokens,
    set_birth_date,
    set_birth_place,
    set_birth_time,
    set_last_daily_sent_date,
    set_notify_time,
    set_notify_enabled,
    set_paid_full_date,
    set_paid_match_date,
    set_subscription_until,
    set_matrix_access,
    set_matrix_paid_at,
    set_personal_arcana,
    set_zodiac,
    upsert_user,
)
from horoscope import (
    arcana_label,
    arcana_meaning,
    arcana_name,
    day_arcana,
    day_code,
    generate_daily_reading,
    generate_pair_reading,
    life_path_number,
    matrix_of_destiny,
    personal_arcana_from_birth_date,
    zodiac_element,
    zodiac_sign,
)

# Новый модуль генерации матрицы судьбы (октаграмма Фадеева)
from matrix_image import build_matrix_image
from matrix_model import calc_matrix, ARCANA as MATRIX_ARCANA
from matrix_report import build_matrix_report, build_matrix_report_html
import commerce
from app_services import init_app, has_consent, accept_consent, get_daily_reading
from admin_panel import make_admin_router
from payment_router import make_payment_router, wallet_keyboard
from legal_documents import terms_text, privacy_text
from launch_actions import run_configured_actions
from yookassa_provider import reconciliation_loop

DEFAULT_TIMEZONE = "Europe/Moscow"
FULL_READING_PRICE = 10
MATCH_PRICE = 15
SUBSCRIPTION_PRICE = 199
SUBSCRIPTION_DAYS = 30
MATRIX_PRICE = commerce.PRODUCT_PRICES['matrix']
MATRIX_REPORT_PRICE = commerce.PRODUCT_PRICES['matrix_report']
DEFAULT_NOTIFY_TIME = "09:00"
REFERRER_BONUS = 5
REFERRAL_FRIEND_BONUS = 10

logging.basicConfig(level=logging.INFO)

bot: Bot | None = None
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(make_admin_router(set(get_settings().admin_ids)))
dp.include_router(make_payment_router())
dp.include_router(router)

BOT_USERNAME: str | None = None
_matrix_active: set[int] = set()
_matrix_workers = asyncio.Semaphore(2)


class PrivateChatMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if getattr(getattr(message, 'chat', None), 'type', None) != 'private':
            if isinstance(event, CallbackQuery):
                await event.answer('Открой личный чат с ботом.', show_alert=True)
            return
        return await handler(event, data)


dp.message.outer_middleware(PrivateChatMiddleware())
dp.callback_query.outer_middleware(PrivateChatMiddleware())


class Onboarding(StatesGroup):
    birth_year = State()
    birth_month = State()
    birth_day = State()
    birth_hour = State()
    birth_minute = State()
    birth_place = State()
    match_username = State()


class ScheduleSetup(StatesGroup):
    hour = State()
    minute = State()




# ---------------------------
# Helpers
# ---------------------------

def e(value) -> str:
    return html.escape("" if value is None else str(value))


def is_admin(user_id: int | None) -> bool:
    return user_id in get_settings().admin_ids


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔮 Мой расклад", style="primary")],
            [KeyboardButton(text="✦ Матрица судьбы"), KeyboardButton(text="📋 Меню")],
            [KeyboardButton(text="💳 Пополнить токены", style="success")],
        ],
        resize_keyboard=True,
    )


def main_menu_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔮 Расклады", callback_data="menu:readings", style="primary"),
            InlineKeyboardButton(text="✦ Матрица судьбы", callback_data="menu:matrix", style="primary"),
        ],
        [
            InlineKeyboardButton(text="💕 Совместимость", callback_data="menu:match"),
            InlineKeyboardButton(text="⭐ Подписка", callback_data="menu:subscription"),
        ],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
            InlineKeyboardButton(text="💳 Токены", callback_data="menu:tokens", style="success"),
        ],
        [
            InlineKeyboardButton(text="⏰ Доставка", callback_data="menu:schedule"),
            InlineKeyboardButton(text="ℹ️ О боте", callback_data="menu:about"),
        ],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)




def back_button(back_cb: str, text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=back_cb)]
        ]
    )


def one_row_buttons(*buttons: tuple[str, str], back_cb: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t, callback_data=cb) for t, cb in buttons]]
    if back_cb:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def readings_menu_keyboard(user) -> InlineKeyboardMarkup:
    full_free = bool(user) and (subscription_active(user) or full_unlocked_today(user, current_local_date(user)))
    full_label = "💎 Полный — открыт" if full_free else f"💎 Полный • {FULL_READING_PRICE}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="☀️ Краткий · бесплатно", callback_data="reading:short"),
                InlineKeyboardButton(text=full_label, callback_data="reading:full", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🧿 Личный портрет", callback_data="reading:personal"),
                InlineKeyboardButton(text="📅 Код дня", callback_data="reading:daycode"),
            ],
            [
                InlineKeyboardButton(text="🧮 Матрица судьбы", callback_data="menu:matrix"),
            ],
            [
                InlineKeyboardButton(text="👥 Совместимость", callback_data="menu:match"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main"),
            ],
        ]
    )


def tokens_menu_keyboard() -> InlineKeyboardMarkup:
    return wallet_keyboard()


def profile_menu_keyboard(has_profile: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Показать профиль", callback_data="profile:show")],
    ]
    if not has_profile:
        rows.append([InlineKeyboardButton(text="▶️ Продолжить заполнение", callback_data="profile:continue")])
    rows.append([InlineKeyboardButton(text="✏️ Сбросить профиль", callback_data="profile:reset")])
    rows.append([InlineKeyboardButton(text="🌍 Часовой пояс доставки", callback_data="profile:timezone")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_menu_keyboard(user) -> InlineKeyboardMarkup:
    enabled = bool(user["notify_enabled"]) if user is not None else True
    toggle_text = "⏸ Отключить доставку" if enabled else "▶️ Включить доставку"
    toggle_cb = "schedule:disable_prompt" if enabled else "schedule:enable"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Изменить время", callback_data="schedule:set_time")],
            [InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)],
            [InlineKeyboardButton(text="🌍 Изменить часовой пояс", callback_data="profile:timezone")],
            [InlineKeyboardButton(text="⭐ Моя подписка", callback_data="menu:subscription")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")],
        ]
    )


def subscription_menu_keyboard(user) -> InlineKeyboardMarkup:
    if subscription_active(user):
        rows = [
            [InlineKeyboardButton(text='💎 Расклад на сегодня', callback_data='reading:full', style='primary')],
            [InlineKeyboardButton(text='👥 Совместимость', callback_data='menu:match')],
            [InlineKeyboardButton(text='⏰ Настроить доставку', callback_data='menu:schedule')],
        ]
    else:
        rows = [[InlineKeyboardButton(text=f'⭐ Подключить · {SUBSCRIPTION_PRICE} токенов', callback_data='sub:buy_prompt', style='primary')]]
    rows.append([InlineKeyboardButton(text='⬅️ Главное меню', callback_data='back:main')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(confirm_cb: str, back_cb: str, confirm_text: str = "✅ Подтвердить") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=confirm_text, callback_data=confirm_cb,
                                  style='danger' if 'reset' in confirm_cb or 'disable' in confirm_cb else 'primary')],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
        ]
    )


def year_keyboard() -> InlineKeyboardMarkup:
    current_year = datetime.now().year
    years = [(str(y), str(y)) for y in range(current_year, current_year - 101, -1)]
    return make_grid_keyboard("birth_year", years, 3)


def month_keyboard() -> InlineKeyboardMarkup:
    months = [
        ("Янв", "01"), ("Фев", "02"), ("Мар", "03"), ("Апр", "04"),
        ("Май", "05"), ("Июн", "06"), ("Июл", "07"), ("Авг", "08"),
        ("Сен", "09"), ("Окт", "10"), ("Ноя", "11"), ("Дек", "12"),
    ]
    return make_grid_keyboard("birth_month", months, 3)


def days_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    max_day = calendar.monthrange(year, month)[1]
    days = [(str(d).zfill(2), str(d).zfill(2)) for d in range(1, max_day + 1)]
    return make_grid_keyboard("birth_day", days, 7)


def hours_keyboard(prefix: str, back_cb: str | None = None) -> InlineKeyboardMarkup:
    hours = [(str(h).zfill(2), str(h).zfill(2)) for h in range(24)]
    return make_grid_keyboard(prefix, hours, 6, back_cb=back_cb)


def minutes_keyboard(prefix: str, back_cb: str | None = None) -> InlineKeyboardMarkup:
    minutes = [(str(m).zfill(2), str(m).zfill(2)) for m in range(0, 60, 5)]
    return make_grid_keyboard(prefix, minutes, 6, back_cb=back_cb)


def make_grid_keyboard(prefix: str, values: list[tuple[str, str]], columns: int, back_cb: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(values), columns):
        chunk = values[i:i + columns]
        row = [InlineKeyboardButton(text=text, callback_data=f"{prefix}:{value}") for text, value in chunk]
        rows.append(row)
    if back_cb:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tz_for_user(user) -> timezone:
    tz_name = user["timezone"] or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone(timedelta(hours=3))


def current_local_date(user) -> date_cls:
    return datetime.now(tz_for_user(user)).date()


def pretty_date(value: str | None) -> str:
    if not value:
        return "не задано"
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
        return d.strftime("%d.%m.%Y")
    except ValueError:
        return value


def pretty_time(value: str | None) -> str:
    return value if value else "не задано"


def pretty_iso_dt(value: str | None) -> str:
    if not value:
        return "не задано"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.strftime("%d.%m.%Y %H:%M")
        local = dt.astimezone(timezone.utc)
        return local.strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        return value


def referral_link(user_id: int) -> str | None:
    if not BOT_USERNAME:
        return None
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"


def referral_explanation(link: str) -> str:
    return (
        '<b>🎁 Пригласи друга — бонус обоим</b>\n'
        f'Тебе — <b>{REFERRER_BONUS} токенов</b>, другу — <b>{REFERRAL_FRIEND_BONUS} токенов</b>.\n'
        'Начислим, когда друг впервые запустит бота по твоей ссылке. '
        'Один раз за каждого нового пользователя.\n'
        f'{e(link)}'
    )


def missing_profile_steps(user) -> list[str]:
    missing = []
    if not user["birth_date"]:
        missing.append("дата рождения")
    if not user["birth_time"]:
        missing.append("время рождения")
    if not user["birth_place"]:
        missing.append("место рождения")
    return missing


def profile_complete(user) -> bool:
    return bool(user) and len(missing_profile_steps(user)) == 0


def subscription_active(user) -> bool:
    if not user or not user["subscription_until"]:
        return False
    try:
        until = datetime.fromisoformat(user["subscription_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)
    except Exception:
        return False


def full_unlocked_today(user, local_date: date_cls) -> bool:
    return bool(user and user["paid_full_date"] == local_date.isoformat())


def match_unlocked_today(user, local_date: date_cls) -> bool:
    return bool(user and user["paid_match_date"] == local_date.isoformat())


def format_profile(user) -> str:
    zodiac = user["zodiac"] or (zodiac_sign(user["birth_date"]) if user["birth_date"] else "не задан")
    element = zodiac_element(zodiac) if zodiac != "не задан" else "—"
    life_num = life_path_number(user["birth_date"]) if user["birth_date"] else None
    tokens = int(user["tokens"] or 0)
    username = f"@{user['username']}" if user["username"] else "не задан"
    personal_arcana = user["personal_arcana"] or (personal_arcana_from_birth_date(user["birth_date"]) if user["birth_date"] else "не задан")
    personal_arcana_label_text = arcana_label(personal_arcana) if user["birth_date"] else "не задан"
    sub_text = f"до {pretty_iso_dt(user['subscription_until'])}" if subscription_active(user) else "не подключена"

    text = f"""
<b>👤 Твой профиль</b>
{e(user['name'])} · {e(username)}

<b>Данные рождения</b>
Дата · {pretty_date(user['birth_date'])}
Время · {pretty_time(user['birth_time'])}
Место · {e(user['birth_place'] or 'не задано')}

<b>Твой символический портрет</b>
Знак · {e(zodiac)} · {e(element)}
Число пути · {life_num if life_num is not None else 'не задано'}
Личный аркан · {e(personal_arcana_label_text)}

<b>Баланс и доступ</b>
◈ {tokens} токенов
⭐ Подписка · {sub_text}
Схема и разбор — в разделе «Матрица судьбы».

<b>Доставка расклада</b>
Время · {pretty_time(user['notify_time'])}
Часовой пояс · {e(user['timezone'] or DEFAULT_TIMEZONE)}
"""
    if not profile_complete(user):
        text += "\n<b>Осталось заполнить:</b>\n"
        for item in missing_profile_steps(user):
            text += f"• {e(item)}\n"

    link = referral_link(user["telegram_id"])
    if link:
        text += '\n' + referral_explanation(link) + '\n'

    return text


def format_short_reading(reading, local_date: date_cls) -> str:
    return f"""
<b>🔮 Краткий расклад на {local_date.strftime('%d.%m.%Y')}</b>

🃏 <b>Карта дня:</b> {e(reading['card'])}
🌙 <b>Тема:</b> {e(reading['theme'])}
💡 <b>Совет:</b> {e(reading['advice'])}

🔢 <b>Код дня:</b> {reading['day_code']}
🃏 <b>Аркан дня:</b> {reading['day_arcana_label']}
✨ <b>Личный аркан:</b> {reading['personal_arcana_label']}
♈ <b>Знак:</b> {e(reading['zodiac'])}
"""


def format_full_reading(reading, local_date: date_cls) -> str:
    return f"""
<b>🔮 Полный расклад на {local_date.strftime('%d.%m.%Y')}</b>

🃏 <b>Карта дня:</b> {e(reading['card'])}
🌙 <b>Тема дня:</b> {e(reading['theme'])}
⚡ <b>Энергия:</b> {e(reading['energy'])}
🪞 <b>Тень:</b> {e(reading['challenge'])}
🌟 <b>Возможность:</b> {e(reading['opportunity'])}

💖 <b>Любовь:</b> {e(reading['love'])}
💼 <b>Работа:</b> {e(reading['work'])}
💰 <b>Деньги:</b> {e(reading['money'])}

<i>Условные акценты дня, не вероятности событий:</i>
🤝 <b>Контактность:</b> {reading['social']}%
🧠 <b>Интуиция:</b> {reading['intuition']}%
🎯 <b>Фокус:</b> {reading['focus']}%
🍀 <b>Удача:</b> {reading['luck']}%

🔢 <b>Код дня:</b> {reading['day_code']}
🃏 <b>Аркан дня:</b> {reading['day_arcana_label']}
✨ <b>Личный аркан:</b> {reading['personal_arcana_label']}
♈ <b>Знак:</b> {e(reading['zodiac'])}
🧭 <b>Стихия:</b> {e(reading['zodiac_element'])}
🔢 <b>Число пути:</b> {reading['life_path_number'] if reading['life_path_number'] is not None else 'не задано'}
🎨 <b>Цвет дня:</b> {e(reading['color'])}
⏳ <b>Лучшее время:</b> {e(reading['timing'])}
📍 <b>Личный акцент:</b> {e(reading['birth_place_hint'])}

💬 <b>Совет:</b> {e(reading['advice'])}
🧿 <b>Ритуал:</b> {e(reading['ritual'])}
🗝 <b>Фраза дня:</b> {e(reading['mantra'])}

<i>{"Интерпретация дополнена ИИ" if reading.get("generation_source") == "ai" else "Персональная алгоритмическая интерпретация"}</i>
"""


def format_personal_matrix(user) -> str:
    zodiac = user["zodiac"] or (zodiac_sign(user["birth_date"]) if user["birth_date"] else "не задан")
    element = zodiac_element(zodiac) if zodiac != "не задан" else "—"
    life_num = life_path_number(user["birth_date"]) if user["birth_date"] else None
    personal_arcana = user["personal_arcana"] or (personal_arcana_from_birth_date(user["birth_date"]) if user["birth_date"] else "не задан")
    personal_arcana_label_text = arcana_label(personal_arcana) if user["birth_date"] else "не задан"

    return f"""
<b>🧿 Личный портрет</b>

Четыре ориентира, с которых можно начать знакомство с собой.

• <b>Знак:</b> {e(zodiac)}
• <b>Стихия:</b> {e(element)}
• <b>Число пути:</b> {life_num if life_num is not None else 'не задано'}
• <b>Личный аркан:</b> {e(personal_arcana_label_text)}
• <b>Место рождения:</b> {e(user['birth_place'] or 'не задано')}

Знак и стихия задают образ темперамента, число пути — тему развития,
а личный аркан — символ для размышления о своих привычках и сильных сторонах.

Подробная схема с линиями и позициями — в разделе «Матрица судьбы».
"""


def format_day_code_text(local_date: date_cls) -> str:
    code = day_code(local_date)
    arc = day_arcana(local_date)
    return f"""
<b>📅 Код дня на {local_date.strftime('%d.%m.%Y')}</b>

• <b>Код дня:</b> {code}
• <b>Аркан дня:</b> {arcana_label(arc)}
• <b>Смысл:</b> {arcana_meaning(arc)}

Это общий символический фон даты — он одинаков для всех.
В разделе «Расклады» ты найдёшь личную интерпретацию дня с учётом своего профиля.
"""


def format_match_result(user, other, pair, local_date: date_cls, target_username: str) -> str:
    return f"""
<b>👥 Совместимость на {local_date.strftime('%d.%m.%Y')}</b>

• <b>Ты:</b> {e(user['name'])}
• <b>Партнёр:</b> @{e(target_username)}

• <b>Совместимость:</b> {pair['score']}%
• <b>Энергия связки:</b> {e(pair['theme'])}
• <b>Сильная сторона:</b> {e(pair['strength'])}
• <b>Риск:</b> {e(pair['risk'])}
• <b>Лучшее окно:</b> {e(pair['timing'])}
• <b>Коммуникация:</b> {pair['communication']}%
• <b>Химия:</b> {pair['chemistry']}%
• <b>Поддержка:</b> {pair['support']}%
• <b>Стабильность:</b> {pair['stability']}%
• <b>Итог:</b> {e(pair['result'])}

• <b>Знак партнёра:</b> {e(pair['sign_b'])}
• <b>Стихия партнёра:</b> {e(pair['element_b'])}
• <b>Код дня:</b> {pair['day_code']}
• <b>Аркан дня:</b> {pair['day_arcana_label']}

• <b>Совет:</b> {e(pair['advice'])}
"""


def format_full_pay_prompt(user, local_date: date_cls) -> str:
    tokens = int(user["tokens"] or 0)
    return f"""
<b>💎 Полный расклад на сегодня</b>

Больше деталей, чтобы спокойно расставить акценты на {local_date.strftime('%d.%m')}.

<b>Что внутри</b>
• Отношения, работа и деньги.
• Возможности дня и то, что может помешать.
• Личный совет, небольшой ритуал и фраза дня.

<b>Открыть на сегодня · {FULL_READING_PRICE} токенов</b>
Повторный просмотр сегодня — без доплаты.
Твой баланс · {tokens} токенов

⭐ Читаешь регулярно? Подписка на {SUBSCRIPTION_DAYS} дней — {SUBSCRIPTION_PRICE} токенов.
{subscription_savings_text()}
"""


def format_match_pay_prompt(user, target_username: str) -> str:
    tokens = int(user["tokens"] or 0)
    premium = subscription_active(user)
    price = 0 if premium else MATCH_PRICE
    price_text = "Включено в твою подписку" if premium else f"{price} токенов"

    return f"""
<b>👥 Совместимость</b>

Партнёр: @{e(target_username)}

Взгляд на вашу пару сегодня: что сближает, где могут возникнуть разногласия
и как сделать общение теплее. Внутри — сильная сторона пары и совет на день.

• <b>Стоимость:</b> {price_text}
• <b>Баланс:</b> {tokens}

Доступ действует сегодня; повторный просмотр — без доплаты.
С подпиской совместимость уже включена.
"""


def subscription_savings_text() -> str:
    separate = FULL_READING_PRICE * SUBSCRIPTION_DAYS
    savings = separate - SUBSCRIPTION_PRICE
    if savings <= 0:
        return 'Сравни стоимость с тем, как часто ты открываешь полный расклад.'
    threshold = SUBSCRIPTION_PRICE // FULL_READING_PRICE + 1
    token_word = 'токенов' if 11 <= savings % 100 <= 14 else (
        'токен' if savings % 10 == 1 else 'токена' if savings % 10 in (2, 3, 4) else 'токенов'
    )
    return (
        f'{SUBSCRIPTION_DAYS} ежедневных раскладов по отдельности — {separate} токенов. '
        f'С подпиской экономия — <b>{savings} {token_word}</b>.\n'
        f'Она выгоднее разовых покупок, если открывать полный расклад '
        f'хотя бы {threshold} дней за срок подписки.'
    )


def format_subscription_prompt(user) -> str:
    tokens = int(user["tokens"] or 0)
    return f"""
<b>⭐ Подключить подписку</b>

<b>{SUBSCRIPTION_DAYS} дней · {SUBSCRIPTION_PRICE} токенов</b>
Срок начнётся сразу после подтверждения.

💎 Полный расклад каждый день.
👥 Совместимость без отдельных списаний.
⏰ Доставка в выбранное время, если она включена в настройках.

С баланса спишется <b>{SUBSCRIPTION_PRICE} токенов</b>.
Сейчас на балансе · {tokens} токенов

<b>Без автопродления.</b> Следующее подключение — только по твоему решению.
Схема и разбор матрицы приобретаются отдельно.
"""


def format_subscription_menu_text(user) -> str:
    active = subscription_active(user)
    if active:
        return f"""
<b>⭐ Твоя подписка активна</b>
Действует до <b>{pretty_iso_dt(user['subscription_until'])}</b>

<b>Уже включено</b>
💎 Полный расклад на каждый день.
👥 Совместимость без отдельных списаний.
⏰ Автоматическая доставка в удобное время.

<b>Твоя доставка</b>
{'Включена' if user['notify_enabled'] else 'Выключена'} · {pretty_time(user['notify_time'])}
Часовой пояс · {e(user['timezone'] or DEFAULT_TIMEZONE)}

Открывай расклад кнопкой ниже, даже если доставка выключена.
Автопродления нет. Схема и разбор матрицы приобретаются отдельно.
"""
    return (
        f'<b>⭐ Месяц внимания к себе</b>\n'
        f'{SUBSCRIPTION_DAYS} дней · <b>{SUBSCRIPTION_PRICE} токенов</b>\n\n'
        '<b>Всё для твоего дня</b>\n'
        '💎 Полный расклад каждый день.\n'
        '💕 Совместимость без отдельных списаний.\n'
        '⏰ Доставка в выбранное время — включается в настройках.\n\n'
        '<b>Почему выгоднее</b>\n'
        f'{SUBSCRIPTION_DAYS} полных раскладов отдельно — {FULL_READING_PRICE * SUBSCRIPTION_DAYS} токенов. '
        f'С подпиской — {SUBSCRIPTION_PRICE}: экономия <b>{FULL_READING_PRICE * SUBSCRIPTION_DAYS - SUBSCRIPTION_PRICE} токен</b>. '
        f'Она окупается с {SUBSCRIPTION_PRICE // FULL_READING_PRICE + 1}-го дня; совместимость уже включена.\n\n'
        'Срок начинается при подключении. <b>Без автопродления.</b>\n'
        'Матрица и её разбор приобретаются отдельно.'
    )






async def render(target: Message | CallbackQuery, text: str, reply_markup=None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=reply_markup)
    else:
        await target.answer(text, reply_markup=reply_markup)


async def safe_answer(message: Message, text: str, reply_markup=None) -> None:
    await message.answer(text, reply_markup=reply_markup)


def parse_ref_payload(message_text: str | None) -> int | None:
    if not message_text:
        return None
    parts = message_text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    if not payload.startswith("ref_"):
        return None
    try:
        return int(payload[4:])
    except ValueError:
        return None


async def prepare_subscription_if_needed(user_id: int) -> None:
    user = await get_user(user_id)
    if user and not user["notify_time"]:
        await set_notify_time(user_id, DEFAULT_NOTIFY_TIME)


async def send_main_menu(target, state=None):
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    text = (
        f"<b>🔮 {e(get_settings().brand_name)}</b>\n"
        "Твой день. Твои ориентиры.\n\n"
        f"💎 Баланс: <b>{int(user['tokens'] or 0) if user else 0} токенов</b>"
        + ("  ·  ⭐ Подписка активна" if user and subscription_active(user) else "") + "\n\n"
        "<b>Расклады</b> — настроение дня и личный совет по дате, времени и месту рождения. "
        "Краткий расклад — бесплатно.\n\n"
        "<b>Матрица судьбы</b> — твои сильные стороны, сценарии и точки роста.\n\n"
        "Выбери раздел ниже ↓"
    )
    if user and not profile_complete(user):
        text += "\n\n👤 Сначала заверши профиль — раздел «Профиль»."
    await render(target, text, main_menu_keyboard(target.from_user.id))


async def send_readings_menu(target: Message | CallbackQuery, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    full_access = bool(user) and (subscription_active(user) or full_unlocked_today(user, current_local_date(user)))
    full_price = 'сегодня уже открыт' if full_access else f'{FULL_READING_PRICE} токенов на день'
    text = f"""
<b>🔮 Твой день в деталях</b>

<b>На сегодня</b>
🌟 Краткий расклад — карта, тема и совет. Бесплатно.
💎 Полный расклад — отношения, работа, деньги и личные акценты; {full_price}.

<b>Познакомиться с собой</b>
🧿 Личный портрет — знак, стихия, число пути и аркан. Бесплатно.
📅 Код дня — общий символический фон даты. Бесплатно.
🧮 Матрица судьбы — схема и подробный разбор по дате рождения; отдельная покупка.

<b>Посмотреть на отношения</b>
👥 Совместимость — сильные стороны пары и совет для общения.

Начни с того, что тебе сейчас ближе.
"""
    await render(target, text, readings_menu_keyboard(user))


async def send_profile_menu(target: Message | CallbackQuery, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    if not user:
        await render(target, "Сначала нажми /start.", main_menu_keyboard(target.from_user.id))
        return
    await render(target, format_profile(user), profile_menu_keyboard(profile_complete(user)))


async def send_tokens_menu(target, state=None):
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    tokens = int(user['tokens'] or 0) if user else 0
    text = (
        f"<b>💎 Твой кошелёк</b>\n\nНа балансе: <b>{tokens} токенов</b>\n\n"
        "<b>Что можно открыть</b>\n"
        f"Расклад — {FULL_READING_PRICE} · совместимость — {MATCH_PRICE} токенов на день\n"
        f"Подписка на {SUBSCRIPTION_DAYS} дней — {SUBSCRIPTION_PRICE} токенов\n"
        f"Матрица: схема — {MATRIX_PRICE} · полный разбор со схемой — {MATRIX_REPORT_PRICE} токенов\n\n"
        "<b>Выбери пакет ниже ↓</b>\n"
        "На кнопке — количество токенов и сумма оплаты.\n"
        "Разовое пополнение. Без автосписаний."
    )
    payment_settings = get_settings()
    if not payment_settings.payment_allowed(target.from_user.id):
        text += "\n\n<i>Пополнение скоро откроется. Пока оплату не принимаем.</i>"
    elif payment_settings.payments_admin_only:
        text += "\n\n<i>Закрытая проверка: пополнение доступно только администратору.</i>"
        if payment_settings.payment_provider == 'yookassa' and not payment_settings.yookassa_test:
            text += "\n<b>Платёж настоящий — деньги будут списаны.</b>"
    link = referral_link(target.from_user.id)
    if link:
        text += '\n\n' + referral_explanation(link)
    await render(target, text, tokens_menu_keyboard())


async def send_schedule_menu(target: Message | CallbackQuery, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    if not user:
        await render(target, "Сначала нажми /start.", main_menu_keyboard(target.from_user.id))
        return

    enabled = bool(user["notify_enabled"]) if user["notify_enabled"] is not None else True
    text = f"""
<b>⏰ Расклад в твоём ритме</b>

Выбери момент для себя — бот пришлёт полный расклад прямо в этот чат.

<b>Твои настройки</b>
Время · <b>{pretty_time(user['notify_time'])}</b>
Часовой пояс · {e(user['timezone'] or DEFAULT_TIMEZONE)}
Доставка · <b>{'включена' if enabled else 'выключена'}</b>
Подписка · <b>{'активна' if subscription_active(user) else 'не активна'}</b>

{'Для автоматической доставки оставь её включённой и задай время.' if subscription_active(user) else 'Настройки можно сохранить заранее. Автоматическая доставка начнётся после подключения подписки, если она включена и задано время.'}

Отключение доставки сохраняет оплаченный доступ и не продлевает срок подписки.
Расклад всегда можно открыть самостоятельно в разделе «Расклады».
"""
    await render(target, text, schedule_menu_keyboard(user))


async def send_subscription_menu(target: Message | CallbackQuery, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    if not user:
        await render(target, "Сначала нажми /start.", main_menu_keyboard(target.from_user.id))
        return
    await render(target, format_subscription_menu_text(user), subscription_menu_keyboard(user))


async def send_about(target, state=None):
    if state:
        await state.clear()
    ai = ("Полный расклад может дополняться ИИ: он раскрывает уже рассчитанную тему, сохраняя исходные значения. "
          if get_settings().ai_enabled else "Сейчас работает персональный алгоритм интерпретаций. ")
    text = (
        f"<b>✦ {e(get_settings().brand_name)}</b>\n\n"
        "Место для ежедневной рефлексии через символы Таро, нумерологию и астрологическую традицию.\n\n"
        "Дата, время и место рождения участвуют в персональном выборе расклада. "
        "При одинаковом профиле в течение дня карта остаётся прежней. " + ai +
        "\n\nМатрица рассчитывается по дате рождения: для её чисел время и город не нужны. "
        "В схеме и разборе используются одни и те же формулы.\n\n"
        "Символические интерпретации не предсказывают неизбежные события и не заменяют решения специалиста.\n\n"
        "/terms — условия · /privacy — данные · /paysupport — поддержка"
    )
    await render(target, text, main_menu_keyboard(target.from_user.id))


def birth_place_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


def match_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")]
        ]
    )


def matrix_access_active(user) -> bool:
    return bool(user and user.get('_matrix_unlocked', False))


def matrix_menu_keyboard(user) -> InlineKeyboardMarkup:
    rows = []
    if matrix_access_active(user):
        rows.append([InlineKeyboardButton(text="✦ Открыть схему", callback_data="matrix:open", style='primary')])
        rows.append([InlineKeyboardButton(text="Скачать PNG без сжатия", callback_data="matrix:download")])
    else:
        rows.append([InlineKeyboardButton(text=f"Схема · {MATRIX_PRICE} токенов", callback_data="matrix:buy_prompt")])
    report_open = bool(user and user.get('_report_unlocked'))
    rows.append([InlineKeyboardButton(
        text="Читать мой разбор" if report_open else f"Полный разбор · {MATRIX_REPORT_PRICE} токенов",
        callback_data="matrix:report:open" if report_open else "matrix:report:prompt", style='primary')])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_matrix_preview(user) -> str:
    if not user or not user['birth_date']:
        return '<b>✦ Матрица судьбы</b>\n\nСначала заполни дату рождения.'
    matrix = calc_matrix(user['birth_date'])
    return (
        f"<b>✦ Матрица судьбы · {pretty_date(user['birth_date'])}</b>\n\n"
        f"Личность · <b>{matrix['A']} — {e(MATRIX_ARCANA[matrix['A']])}</b>\n"
        f"Таланты · <b>{matrix['B']} — {e(MATRIX_ARCANA[matrix['B']])}</b>\n"
        f"Внутренняя опора · <b>{matrix['E']} — {e(MATRIX_ARCANA[matrix['E']])}</b>\n\n"
        "<b>Схема</b> — 30 точек, родовые диагонали, линии отношений и реализации, направления и таблица ресурсов.\n\n"
        "<b>Полный разбор</b> — объяснение каждой позиции: сильные стороны, возможные трудности и конкретные шаги. "
        "Схема входит в разбор. Его можно читать по разделам и скачать целиком.\n\n"
        "Повторное открытие купленного материала для этой даты рождения бесплатно."
    )


def format_matrix_buy_prompt(user) -> str:
    return (
        f"<b>Твоя схема матрицы</b>\n\nДата: {pretty_date(user['birth_date'])}\n"
        f"Стоимость: <b>{MATRIX_PRICE} токенов</b>\nБаланс: <b>{user['tokens']}</b>\n\n"
        "Чёткая схема, подписи позиций и легенда. Можно скачать PNG в полном качестве. "
        "Повторное открытие для этой даты рождения — без оплаты.\n\n"
        f"Подробный текстовый разбор продаётся отдельно за {MATRIX_REPORT_PRICE} токенов и уже включает схему."
    )


# ---------------------------
# Matrix send helper
# ---------------------------

async def send_matrix_menu(target, state=None):
    if state:
        await state.clear()
    user = await load_matrix_user(target.from_user.id)
    if not user or not user['birth_date']:
        await render(target, 'Сначала заполни дату рождения в профиле.', main_menu_keyboard(target.from_user.id))
        return
    if matrix_access_active(user):
        await deliver_matrix(target, user)
    else:
        await render(target, format_matrix_preview(user), matrix_menu_keyboard(user))


# ---------------------------
# Admin helpers
# ---------------------------





# ---------------------------
# Start / Commands
# ---------------------------

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    referrer_id = parse_ref_payload(message.text)
    if referrer_id == message.from_user.id:
        referrer_id = None

    created = await upsert_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username,
        referrer_id=referrer_id,
    )

    if created and referrer_id:
        referrer = await get_user(referrer_id)
        if referrer:
            await add_tokens(message.from_user.id, REFERRAL_FRIEND_BONUS)
            await add_tokens(referrer_id, REFERRER_BONUS)

    user = await get_user(message.from_user.id)

    if not profile_complete(user):
        await safe_answer(
            message,
            "<b>Привет.</b>\n\nСначала заполним профиль, чтобы расклады были точнее и глубже.",
            main_reply_keyboard(),
        )
        await start_profile_flow(message, state, user)
        return

    await safe_answer(message, "<b>С возвращением.</b>\n\nТвой личный расклад готов к работе.", main_reply_keyboard())
    await send_main_menu(message, state)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_main_menu(message, state)


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_profile_menu(message, state)


@router.message(Command("balance"))
async def cmd_balance(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_tokens_menu(message, state)


@router.message(Command("tokens"))
async def cmd_tokens(message: Message, state: FSMContext):
    await cmd_balance(message, state)


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_schedule_menu(message, state)


@router.message(Command("reading"))
async def cmd_reading(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_readings_menu(message, state)


@router.message(Command("match"))
async def cmd_match(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await prompt_match(message, state)


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_subscription_menu(message, state)


@router.message(Command("about"))
async def cmd_about(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_about(message, state)




@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await safe_answer(message, "<b>Ок, шаг отменён.</b>", main_reply_keyboard())
    await send_main_menu(message, state)


@router.message(F.text == "📋 Меню")
async def menu_button(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_main_menu(message, state)


@router.message(F.text == "🔮 Мой расклад")
async def quick_readings(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_readings_menu(message, state)


@router.message(F.text == "✦ Матрица судьбы")
async def quick_matrix(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_matrix_menu(message, state)


@router.message(F.text == "💳 Пополнить токены")
async def quick_tokens(message: Message, state: FSMContext):
    await cmd_balance(message, state)


# ---------------------------
# Main menu callbacks
# ---------------------------

@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_main_menu(callback, state)


@router.callback_query(F.data == "menu:about")
async def menu_about(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_about(callback, state)


@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_profile_menu(callback, state)


@router.callback_query(F.data == "menu:tokens")
async def menu_tokens(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_tokens_menu(callback, state)


@router.callback_query(F.data == "menu:schedule")
async def menu_schedule(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_schedule_menu(callback, state)


@router.callback_query(F.data == "menu:readings")
async def menu_readings(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_readings_menu(callback, state)


@router.callback_query(F.data == "menu:subscription")
async def menu_subscription(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_subscription_menu(callback, state)


@router.callback_query(F.data == "menu:match")
async def menu_match(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await prompt_match(callback, state)


@router.callback_query(F.data == "menu:matrix")
async def menu_matrix(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await send_matrix_menu(callback, state)






# ---------------------------
# Back buttons
# ---------------------------

@router.callback_query(F.data.startswith("back:"))
async def back_handler(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    target = callback.data.split(":", 1)[1]
    user = await get_user(callback.from_user.id)

    if target == "main":
        await send_main_menu(callback, state)
    elif target == "readings":
        await send_readings_menu(callback, state)
    elif target == "profile":
        await send_profile_menu(callback, state)
    elif target == "tokens":
        await send_tokens_menu(callback, state)
    elif target == "schedule":
        await send_schedule_menu(callback, state)
    elif target == "subscription":
        await send_subscription_menu(callback, state)
    elif target == "match":
        await prompt_match(callback, state)
    elif target == "birth_year":
        await state.clear()
        await start_profile_flow(callback, state, user)
    elif target == "birth_month":
        await state.set_state(Onboarding.birth_year)
        await render(callback, "Выбери год рождения:", year_keyboard())
        return
    elif target == "birth_day":
        data = await state.get_data()
        year = int(data.get("birth_year") or (user["birth_date"] or "2000").split("-")[0])
        await state.update_data(birth_year=year)
        await state.set_state(Onboarding.birth_month)
        await render(callback, "Выбери месяц рождения:", month_keyboard())
        return
    elif target == "birth_hour":
        data = await state.get_data()
        year = int(data.get("birth_year") or (user["birth_date"] or "2000").split("-")[0])
        month = int(data.get("birth_month") or (user["birth_date"] or "2000-01").split("-")[1])
        await state.update_data(birth_year=year, birth_month=month)
        await state.set_state(Onboarding.birth_day)
        await render(callback, f"Год: {year}\nМесяц: {month:02d}\n\nВыбери день:", days_keyboard(year, month))
        return
    elif target == "birth_minute":
        await state.set_state(Onboarding.birth_hour)
        await render(callback, "Выбери час рождения:", hours_keyboard("birth_hour", "back:birth_day"))
        return
    elif target == "schedule_hour":
        await state.set_state(ScheduleSetup.hour)
        await render(callback, "<b>⏰ В какое время прислать расклад?</b>\n\nВыбери час в своём часовом поясе.", hours_keyboard("schedule_hour", "back:schedule"))
        return
    elif target == "schedule_minute":
        await state.set_state(ScheduleSetup.minute)
        await render(callback, "<b>⏰ Осталось выбрать минуты</b>", minutes_keyboard("schedule_minute", "back:schedule_hour"))
        return
    elif target == "full_pay":
        await send_readings_menu(callback, state)
    elif target == "match_pay":
        await prompt_match(callback, state)
    elif target == "sub_pay":
        await send_subscription_menu(callback, state)
    elif target == "sub_cancel":
        await send_subscription_menu(callback, state)
    else:
        await send_main_menu(callback, state)


# ---------------------------
# Profile callbacks
# ---------------------------

@router.callback_query(F.data == "profile:show")
async def profile_show(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user:
        await render(callback, "Сначала нажми /start.", main_menu_keyboard(callback.from_user.id))
        return
    await render(callback, format_profile(user), profile_menu_keyboard(profile_complete(user)))


@router.callback_query(F.data == "profile:continue")
async def profile_continue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    await start_profile_flow(callback, state, user)


@router.callback_query(F.data == "profile:reset")
async def profile_reset(callback: CallbackQuery):
    await callback.answer()
    await render(
        callback,
        "<b>Сбросить профиль?</b>\n\nВсе данные рождения будут очищены.",
        confirm_keyboard("profile:reset_yes", "back:profile", "Да, сбросить"),
    )


@router.callback_query(F.data == "profile:reset_yes")
async def profile_reset_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await reset_profile(callback.from_user.id)
    user = await get_user(callback.from_user.id)
    await start_profile_flow(callback, state, user)


# ---------------------------
# Tokens callbacks
# ---------------------------

@router.callback_query(F.data == "tokens:balance")
async def tokens_balance(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    tokens = int(user["tokens"] or 0) if user else 0
    link = referral_link(callback.from_user.id)
    text = f"<b>💰 Баланс:</b> {tokens}\n"
    if link:
        text += '\n' + referral_explanation(link)
    await render(callback, text, tokens_menu_keyboard())




# ---------------------------
# Schedule callbacks
# ---------------------------

@router.callback_query(F.data == "schedule:time_info")
async def schedule_time_info(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    await render(
        callback,
        f"<b>⏰ Расписание</b>\n\nТекущее время: <b>{pretty_time(user['notify_time'])}</b>",
        schedule_menu_keyboard(user),
    )


@router.callback_query(F.data == "schedule:set_time")
async def schedule_set_time(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ScheduleSetup.hour)
    await render(callback, "<b>⏰ В какое время прислать расклад?</b>\n\nВыбери час в своём часовом поясе.", hours_keyboard("schedule_hour", "back:schedule"))


@router.callback_query(ScheduleSetup.hour, F.data.startswith("schedule_hour:"))
async def schedule_hour(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        hour = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Кнопка устарела.', show_alert=True)
        return
    if not 0 <= hour <= 23:
        return
    await state.update_data(schedule_hour=hour)
    await state.set_state(ScheduleSetup.minute)
    await render(
        callback,
        f"<b>⏰ Время доставки · {hour:02d}:…</b>\n\nТеперь выбери минуты.",
        minutes_keyboard("schedule_minute", "back:schedule_hour"),
    )


@router.callback_query(ScheduleSetup.minute, F.data.startswith("schedule_minute:"))
async def schedule_minute(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        minute = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Кнопка устарела.', show_alert=True)
        return
    if not 0 <= minute <= 59:
        return
    data = await state.get_data()
    if data.get('schedule_hour') is None:
        await state.set_state(ScheduleSetup.hour)
        await render(callback, 'Сначала выбери час доставки.', hours_keyboard('schedule_hour', 'back:schedule'))
        return
    hour = int(data["schedule_hour"])
    notify_time = f"{hour:02d}:{minute:02d}"

    await set_notify_time(callback.from_user.id, notify_time)
    await state.clear()

    user = await get_user(callback.from_user.id)
    await render(
        callback,
        f"<b>✅ Время сохранено · {notify_time}</b>\n\n"
        f"Часовой пояс · {e(user['timezone'] or DEFAULT_TIMEZONE)}\n\n"
        "Расклад придёт в это время при активной подписке и включённой доставке.",
        schedule_menu_keyboard(user),
    )


@router.callback_query(F.data == "schedule:disable_prompt")
async def schedule_disable_prompt(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user:
        await render(callback, "Сначала нажми /start.", main_menu_keyboard(callback.from_user.id))
        return

    text = (
        "<b>⏸ Отключить доставку?</b>\n\n"
        "Ежедневный расклад перестанет приходить автоматически. "
        "Ты сможешь открывать его самостоятельно, пока действует подписка.\n\n"
        "Оплаченный срок продолжит идти. Вернуть доставку можно в этих же настройках."
    )
    await render(
        callback,
        text,
        confirm_keyboard("schedule:disable", "back:schedule", "❌ Отключить"),
    )


@router.callback_query(F.data == "schedule:disable")
async def schedule_disable(callback: CallbackQuery):
    await callback.answer()
    await set_notify_enabled(callback.from_user.id, False)
    user = await get_user(callback.from_user.id)
    await render(
        callback,
        "<b>⏸ Доставка отключена</b>\n\nОплаченный доступ сохранён до конца срока. "
        "Расклад можно открыть в меню, а доставку — включить снова здесь.",
        schedule_menu_keyboard(user),
    )


@router.callback_query(F.data == "schedule:enable")
async def schedule_enable(callback: CallbackQuery):
    await callback.answer()
    await set_notify_enabled(callback.from_user.id, True)
    user = await get_user(callback.from_user.id)
    await render(
        callback,
        "<b>✅ Доставка включена</b>\n\nРасклад будет приходить в выбранное время "
        "при активной подписке. Проверь время и часовой пояс в настройках.",
        schedule_menu_keyboard(user),
    )


# ---------------------------
# Subscription callbacks
# ---------------------------

@router.callback_query(F.data == "sub:buy_prompt")
async def sub_buy_prompt(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    await render(
        callback,
        format_subscription_prompt(user),
        confirm_keyboard("sub:buy", "back:subscription", f"✅ Подключить за {SUBSCRIPTION_PRICE} токенов"),
    )


@router.callback_query(F.data == "sub:buy")
async def sub_buy(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not profile_complete(user):
        await render(callback, 'Сначала заполни профиль — он нужен для ежедневных раскладов.', main_menu_keyboard(callback.from_user.id))
        return
    result = await commerce.purchase_product(callback.from_user.id, 'subscription')
    if result.status == 'insufficient':
        await render(callback,
            f'<b>⭐ До подписки один шаг</b>\n\n'
            f'Стоимость · {SUBSCRIPTION_PRICE} токенов\nБаланс · {result.balance} токенов\n'
            f'Не хватает · {SUBSCRIPTION_PRICE - result.balance} токенов\n\n'
            'Пополни баланс и вернись в раздел «Подписка». Подключение нужно будет подтвердить отдельно.',
            tokens_menu_keyboard())
        return
    await prepare_subscription_if_needed(callback.from_user.id)
    user = await get_user(callback.from_user.id)
    await render(callback,
        format_subscription_menu_text(user), subscription_menu_keyboard(user))


@router.callback_query(F.data == "sub:cancel_prompt")
async def sub_cancel_prompt(callback: CallbackQuery):
    await callback.answer()
    await render(callback, 'У подписки нет автопродления. Можно отключить ежедневную доставку: оплаченный доступ сохранится до конца срока.', confirm_keyboard('sub:cancel', 'menu:subscription', 'Отключить доставку'))


@router.callback_query(F.data == "sub:cancel")
async def sub_cancel(callback: CallbackQuery):
    await callback.answer()
    await set_notify_enabled(callback.from_user.id, False)
    await render(callback, 'Доставка отключена. Оплаченный доступ сохранён до конца срока. Автосписаний нет.', subscription_menu_keyboard(await get_user(callback.from_user.id)))


# ---------------------------
# Readings callbacks
# ---------------------------

@router.callback_query(F.data == "reading:short")
async def reading_short(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not profile_complete(user):
        await render(callback, "Сначала заполни профиль.", main_menu_keyboard(callback.from_user.id))
        return

    local_date = current_local_date(user)
    reading = generate_daily_reading(user, local_date)
    text = format_short_reading(reading, local_date)
    await render(callback, text, readings_menu_keyboard(user))


@router.callback_query(F.data == "reading:daycode")
async def reading_daycode(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user:
        await render(callback, "Сначала нажми /start.", main_menu_keyboard(callback.from_user.id))
        return

    local_date = current_local_date(user)
    await render(callback, format_day_code_text(local_date), readings_menu_keyboard(user))


@router.callback_query(F.data == "reading:personal")
async def reading_personal(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not profile_complete(user):
        await render(callback, "Сначала заполни профиль.", main_menu_keyboard(callback.from_user.id))
        return

    await render(callback, format_personal_matrix(user), readings_menu_keyboard(user))


@router.callback_query(F.data == "reading:full")
async def reading_full(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not profile_complete(user):
        await render(callback, "Сначала заполни профиль.", main_menu_keyboard(callback.from_user.id))
        return

    local_date = current_local_date(user)
    if subscription_active(user) or full_unlocked_today(user, local_date):
        reading = await get_daily_reading(user, local_date)
        await render(callback, format_full_reading(reading, local_date), readings_menu_keyboard(user))
        return

    await render(
        callback,
        format_full_pay_prompt(user, local_date),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=f"💎 Открыть полный расклад • {FULL_READING_PRICE}", callback_data="reading:full_pay"),
                ],
                [
                    InlineKeyboardButton(text=f"⭐ На {SUBSCRIPTION_DAYS} дней · {SUBSCRIPTION_PRICE} токенов", callback_data="menu:subscription"),
                ],
                [
                    InlineKeyboardButton(text="⬅️ Назад", callback_data="back:readings"),
                ],
            ]
        ),
    )


@router.callback_query(F.data == "reading:full_pay")
async def reading_full_pay(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not profile_complete(user):
        await render(callback, 'Сначала заполни профиль.', main_menu_keyboard(callback.from_user.id))
        return
    local_date = current_local_date(user)
    owned = subscription_active(user) or full_unlocked_today(user, local_date) or await commerce.has_entitlement(callback.from_user.id, 'full_reading', local_date.isoformat())
    if not owned and int(user['tokens'] or 0) < FULL_READING_PRICE:
        await render(callback, f'Нужно {FULL_READING_PRICE} токенов. Баланс: {user["tokens"]}.', tokens_menu_keyboard())
        return
    reading = await get_daily_reading(user, local_date)
    result = await commerce.purchase_product(callback.from_user.id, 'full_reading', local_date.isoformat())
    if result.status == 'insufficient':
        await render(callback, f'Нужно {FULL_READING_PRICE} токенов. Баланс: {result.balance}.', tokens_menu_keyboard())
        return
    await render(callback, format_full_reading(reading, local_date), readings_menu_keyboard(await get_user(callback.from_user.id)))


# ---------------------------
# Matrix callbacks
# ---------------------------

@router.callback_query(F.data == "matrix:buy_prompt")
async def matrix_buy_prompt(callback: CallbackQuery):
    await callback.answer()
    user = await load_matrix_user(callback.from_user.id)
    if not user or not user['birth_date']:
        await render(callback, 'Сначала заполни дату рождения.', main_menu_keyboard(callback.from_user.id))
    elif matrix_access_active(user):
        await deliver_matrix(callback, user)
    else:
        await render(callback, format_matrix_buy_prompt(user), confirm_keyboard('matrix:buy:' + user['birth_date'], 'menu:matrix', f'Купить за {MATRIX_PRICE} токенов'))


@router.callback_query(F.data.startswith("matrix:buy:"))
async def matrix_buy(callback: CallbackQuery):
    await callback.answer()
    user = await load_matrix_user(callback.from_user.id)
    if not user or not user['birth_date']:
        await render(callback, 'Сначала заполни дату рождения.', main_menu_keyboard(callback.from_user.id))
        return
    if callback.data.rsplit(':', 1)[-1] != user['birth_date']:
        await render(callback, 'Дата в профиле изменилась. Открой актуальное предложение.', matrix_menu_keyboard(user))
        return
    if matrix_access_active(user):
        await deliver_matrix(callback, user)
        return
    if user['tokens'] < MATRIX_PRICE:
        await render(callback, f'Для схемы нужно {MATRIX_PRICE} токенов. Баланс: {user["tokens"]}.', tokens_menu_keyboard())
        return
    await deliver_matrix(callback, user, purchase=True)


@router.callback_query(F.data == "matrix:open")
async def matrix_open(callback: CallbackQuery):
    await callback.answer()
    await send_matrix_menu(callback, None)


# ---------------------------
# Admin callbacks
# ---------------------------























# ---------------------------
# Matching
# ---------------------------

async def prompt_match(target: Message | CallbackQuery, state: FSMContext) -> None:
    user = await get_user(target.from_user.id)
    if not user:
        await render(target, "Сначала нажми /start.", main_menu_keyboard(target.from_user.id))
        return
    if not profile_complete(user):
        await render(target, "Сначала заполни свой профиль полностью.", main_menu_keyboard(target.from_user.id))
        return

    await state.set_state(Onboarding.match_username)
    text = (
        "<b>👥 На одной волне</b>\n\n"
        "Что вас сближает сегодня? Посмотри на сильные стороны пары, возможные разногласия "
        "и подсказки для тёплого общения.\n\n"
        "<b>Как начать</b>\n"
        "Отправь Telegram-имя партнёра в формате <b>@username</b>. "
        "Вы оба должны быть зарегистрированы в боте и заполнить данные рождения.\n\n"
        + ("⭐ Включено в твою подписку." if subscription_active(user)
           else f"Доступ на день · {MATCH_PRICE} токенов. В подписку уже включён.")
    )
    await render(target, text, match_prompt_keyboard())


@router.message(Onboarding.match_username)
async def match_username(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    username = raw.lstrip("@")

    if len(username) < 5 or len(username) > 32 or not username.replace("_", "").isalnum():
        await message.answer("Нужен корректный @username.")
        return

    user = await get_user(message.from_user.id)
    if not user or not profile_complete(user):
        await state.clear()
        await message.answer("Сначала заполни свой профиль полностью.", reply_markup=main_reply_keyboard())
        return

    other = await get_user_by_username(username)
    if not other:
        link = referral_link(message.from_user.id)
        text = f"""
<b>@{e(username)}</b> ещё не зарегистрирован в боте.

Отправь ему свою реферальную ссылку:"""
        if link:
            text += '\n\n' + referral_explanation(link)
        await state.clear()
        await message.answer(text, reply_markup=main_menu_keyboard(message.from_user.id))
        return

    if not profile_complete(other):
        await state.clear()
        await message.answer(
            f"<b>@{e(username)}</b> уже в боте, но профиль заполнен не полностью.",
            reply_markup=main_menu_keyboard(message.from_user.id),
        )
        return

    local_date = current_local_date(user)
    if subscription_active(user) or match_unlocked_today(user, local_date):
        pair = generate_pair_reading(user, other, local_date)
        await state.clear()
        await message.answer(format_match_result(user, other, pair, local_date, username), reply_markup=main_menu_keyboard(message.from_user.id))
        return

    await state.clear()
    await message.answer(
        format_match_pay_prompt(user, username),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Открыть за {MATCH_PRICE}", callback_data=f"match:pay:{other['telegram_id']}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:match")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("match:pay:"))
async def match_pay(callback: CallbackQuery):
    await callback.answer()
    try:
        other_id = int(callback.data.split(":")[-1])
    except ValueError:
        return

    user = await get_user(callback.from_user.id)
    other = await get_user(other_id)

    if not user or not other:
        await render(callback, "Пользователь не найден.", main_menu_keyboard(callback.from_user.id))
        return

    if not profile_complete(user) or not profile_complete(other):
        await render(callback, "Для совместимости нужны заполненные профили.", main_menu_keyboard(callback.from_user.id))
        return

    local_date = current_local_date(user)
    if subscription_active(user) or match_unlocked_today(user, local_date):
        pair = generate_pair_reading(user, other, local_date)
        await render(callback, format_match_result(user, other, pair, local_date, other["username"] or str(other_id)), main_menu_keyboard(callback.from_user.id))
        return

    result = await commerce.purchase_product(callback.from_user.id, 'match', local_date.isoformat())
    if result.status == 'insufficient':
        tokens = int(user["tokens"] or 0)
        await render(
            callback,
            f"""<b>Недостаточно токенов.</b>

Баланс: <b>{tokens}</b>
Нужно: <b>{MATCH_PRICE}</b>

Пополни баланс и вернись к совместимости позже.""",
            tokens_menu_keyboard(),
        )
        return

    pair = generate_pair_reading(user, other, local_date)
    await render(
        callback,
        format_match_result(user, other, pair, local_date, other["username"] or str(other_id)),
        main_menu_keyboard(callback.from_user.id),
    )


# ---------------------------
# Onboarding: birth date / time / place
# ---------------------------

async def start_profile_flow(target: Message | CallbackQuery, state: FSMContext, user) -> None:
    if not user:
        await render(target, 'Начни с /start.', main_menu_keyboard(target.from_user.id))
        return
    if not await has_consent(target.from_user.id):
        await state.clear()
        await render(target,
            '<b>Соберём твой профиль</b>\n\nПонадобятся дата, время и город рождения. '
            'Они сохраняются для персональных раскладов. Данные можно сбросить в профиле. '
            'Уведомления настраиваются отдельно.\n\n'
            '/privacy — обработка данных · /terms — условия\n\n'
            'Нажимая «Согласен», ты соглашаешься на обработку этих данных для работы сервиса.',
            one_row_buttons(('Согласен · заполнить профиль', 'profile:consent'), back_cb='back:main'))
        return
    if not user["birth_date"]:
        await state.set_state(Onboarding.birth_year)
        await render(target, "<b>Выбери год рождения:</b>", year_keyboard())
        return

    if not user["birth_time"]:
        await state.set_state(Onboarding.birth_hour)
        await render(target, "<b>Выбери час рождения:</b>", hours_keyboard("birth_hour", "back:birth_day"))
        return

    if not user["birth_place"]:
        await state.set_state(Onboarding.birth_place)
        await render(
            target,
            "<b>Напиши место рождения одним сообщением.</b>\n\nПример: <i>Казань, Россия</i>",
            birth_place_keyboard(),
        )
        return

    await state.clear()
    await send_main_menu(target, state)


@router.callback_query(Onboarding.birth_year, F.data.startswith("birth_year:"))
async def birth_year(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        year = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Кнопка устарела.', show_alert=True)
        return
    if not 1900 <= year <= date_cls.today().year:
        return
    await state.update_data(birth_year=year)
    await state.set_state(Onboarding.birth_month)
    await render(callback, "<b>Выбери месяц рождения:</b>", month_keyboard())


@router.callback_query(Onboarding.birth_month, F.data.startswith("birth_month:"))
async def birth_month(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        month = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Кнопка устарела.', show_alert=True)
        return
    if not 1 <= month <= 12:
        return
    await state.update_data(birth_month=month)
    data = await state.get_data()
    year = int(data["birth_year"])
    await state.set_state(Onboarding.birth_day)
    await render(callback, f"<b>Год:</b> {year}\n<b>Месяц:</b> {month:02d}\n\n<b>Выбери день:</b>", days_keyboard(year, month))


@router.callback_query(Onboarding.birth_day, F.data.startswith("birth_day:"))
async def birth_day(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        day = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Кнопка устарела.', show_alert=True)
        return
    data = await state.get_data()
    year = int(data["birth_year"])
    month = int(data["birth_month"])

    try:
        birth_date = date_cls(year, month, day).isoformat()
    except ValueError:
        await render(callback, "Эта дата не существует.", days_keyboard(year, month))
        return

    if date_cls.fromisoformat(birth_date) > date_cls.today():
        await render(callback, "Дата рождения не может быть в будущем.", days_keyboard(year, month))
        return
    await set_birth_date(callback.from_user.id, birth_date)
    await set_zodiac(callback.from_user.id, zodiac_sign(birth_date))
    await set_personal_arcana(callback.from_user.id, personal_arcana_from_birth_date(birth_date))
    await state.set_state(Onboarding.birth_hour)
    await render(callback, f"<b>Дата сохранена:</b> {birth_date}\n\n<b>Выбери час рождения:</b>", hours_keyboard("birth_hour", "back:birth_day"))


@router.callback_query(Onboarding.birth_hour, F.data.startswith("birth_hour:"))
async def birth_hour(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        hour = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Кнопка устарела.', show_alert=True)
        return
    if not 0 <= hour <= 23:
        return
    await state.update_data(birth_hour=hour)
    await state.set_state(Onboarding.birth_minute)
    await render(callback, f"<b>Час рождения:</b> {hour:02d}\n\n<b>Выбери минуты:</b>", minutes_keyboard("birth_minute", "back:birth_hour"))


@router.callback_query(Onboarding.birth_minute, F.data.startswith("birth_minute:"))
async def birth_minute(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        minute = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer('Кнопка устарела.', show_alert=True)
        return
    if not 0 <= minute <= 59:
        return
    data = await state.get_data()
    hour = int(data["birth_hour"])
    birth_time = f"{hour:02d}:{minute:02d}"

    await set_birth_time(callback.from_user.id, birth_time)
    await state.set_state(Onboarding.birth_place)
    await render(
        callback,
        f"<b>Время рождения сохранено:</b> {birth_time}\n\n<b>Напиши место рождения одним сообщением.</b>\n<i>Пример: Казань, Россия</i>",
        birth_place_keyboard(),
    )


@router.message(Onboarding.birth_place)
async def birth_place(message: Message, state: FSMContext):
    place = (message.text or '').strip()
    if not 2 <= len(place) <= 120 or '\n' in place or place.startswith('/'):
        await message.answer('Напиши город и страну одной строкой, от 2 до 120 символов. Например: Казань, Россия.')
        return
    await set_birth_place(message.from_user.id, place)
    await state.clear()
    await message.answer('<b>Профиль сохранён.</b> Дату, время и место рождения будем учитывать в раскладах.\nЧасовой пояс доставки можно выбрать в профиле.', reply_markup=main_reply_keyboard())
    await send_main_menu(message, state)


# ---------------------------
# Daily notifications
# ---------------------------

async def notification_loop():
    while True:
        try:
            users = await get_all_users_for_notifications()
            for user in users:
                if not subscription_active(user):
                    continue
                if not bool(user["notify_enabled"]):
                    continue

                tz = tz_for_user(user)
                now_local = datetime.now(tz)
                today = now_local.date().isoformat()

                if user["last_daily_sent_date"] == today:
                    continue

                notify_time = user["notify_time"] or DEFAULT_NOTIFY_TIME
                target_hour, target_minute = map(int, notify_time.split(":"))
                now_minutes = now_local.hour * 60 + now_local.minute
                target_minutes = target_hour * 60 + target_minute

                if now_minutes < target_minutes:
                    continue

                reading_date = now_local.date()
                reading = await get_daily_reading(user, reading_date)
                text = format_full_reading(reading, reading_date)

                try:
                    await bot.send_message(
                        chat_id=user["telegram_id"],
                        text=text,
                        reply_markup=main_menu_keyboard(user["telegram_id"]),
                    )
                    await set_last_daily_sent_date(user["telegram_id"], today)
                    logging.info("Daily reading sent to %s", user["telegram_id"])
                except Exception as exc:
                    logging.exception("Failed to send daily reading to %s: %s", user["telegram_id"], exc)
        except Exception as exc:
            logging.exception("Notification loop error: %s", exc)

        await asyncio.sleep(30)


# ---------------------------
# Fallback
# ---------------------------


async def load_matrix_user(user_id):
    row = await get_user(user_id)
    if not row:
        return None
    user = dict(row)
    birth = user['birth_date'] or ''
    user['_matrix_unlocked'] = await commerce.has_entitlement(user_id, 'matrix', birth)
    user['_report_unlocked'] = await commerce.has_entitlement(user_id, 'matrix_report', birth)
    return user


async def deliver_matrix(target, user, *, purchase=False, document=False):
    uid = target.from_user.id
    message = target.message if isinstance(target, CallbackQuery) else target
    if uid in _matrix_active:
        await message.answer('Схема уже готовится — несколько секунд.')
        return
    _matrix_active.add(uid)
    image_path = None
    status = None
    try:
        status = await message.answer('✦ Строю матрицу: рассчитываю точки и соединяю линии…')
        async with _matrix_workers:
            image_path = await asyncio.to_thread(build_matrix_image, user)
        if purchase:
            result = await commerce.purchase_product(uid, 'matrix', user['birth_date'])
            if result.status == 'insufficient':
                await status.edit_text(f'Нужно {MATRIX_PRICE} токенов. Баланс: {result.balance}.', reply_markup=tokens_menu_keyboard())
                return
            user = await load_matrix_user(uid)
        caption = (f"<b>✦ Твоя матрица · {pretty_date(user['birth_date'])}</b>\n\n"
                   'Схема и разбор используют единый расчёт. Буквы рядом с числами — адреса точек в разборе. '
                   'Для всех деталей скачай PNG без сжатия.')
        if document:
            await message.answer_document(FSInputFile(image_path, filename='Моя матрица.png'), caption=caption, reply_markup=matrix_menu_keyboard(user))
        else:
            await message.answer_photo(FSInputFile(image_path), caption=caption, reply_markup=matrix_menu_keyboard(user))
        await status.edit_text('Матрица готова ✦')
    except (ValueError, commerce.CommerceError):
        if status:
            await status.edit_text('Не удалось построить матрицу для текущего профиля. Проверь дату рождения и попробуй снова. Купленный доступ сохраняется.')
    except Exception:
        logging.exception('Matrix rendering or delivery failed')
        if status:
            await status.edit_text('Не удалось отправить схему. Открой её ещё раз: купленный доступ сохраняется, повторного списания не будет.')
    finally:
        try:
            if image_path:
                Path(image_path).unlink(missing_ok=True)
        finally:
            _matrix_active.discard(uid)


def report_keyboard(page, count):
    rows = []
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text='← Назад', callback_data=f'matrix:report:page:{page-1}'))
    nav.append(InlineKeyboardButton(text=f'{page+1} / {count}', callback_data='matrix:report:noop'))
    if page + 1 < count:
        nav.append(InlineKeyboardButton(text='Далее →', callback_data=f'matrix:report:page:{page+1}'))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text='Скачать весь разбор со схемой', callback_data='matrix:report:download')])
    rows.append([InlineKeyboardButton(text='✦ Схема матрицы', callback_data='menu:matrix'),
                 InlineKeyboardButton(text='Главное меню', callback_data='back:main')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == 'matrix:buy')
async def legacy_matrix_purchase(callback: CallbackQuery):
    # Old confirmation buttons never silently charge for a changed profile.
    await matrix_buy_prompt(callback)


@router.callback_query(F.data == 'matrix:download')
async def matrix_download(callback: CallbackQuery):
    await callback.answer()
    user = await load_matrix_user(callback.from_user.id)
    if not user or not user['birth_date'] or not matrix_access_active(user):
        await send_matrix_menu(callback)
        return
    await deliver_matrix(callback, user, document=True)


@router.callback_query(F.data == 'matrix:report:prompt')
async def report_prompt(callback: CallbackQuery):
    await callback.answer()
    user = await load_matrix_user(callback.from_user.id)
    if not user or not user['birth_date']:
        await render(callback, 'Сначала заполни дату рождения.', main_menu_keyboard(callback.from_user.id))
        return
    if user['_report_unlocked']:
        await deliver_report(callback, user)
        return
    await render(callback,
        f"<b>Полный разбор твоей матрицы</b>\n\nДата: {pretty_date(user['birth_date'])}\n\n"
        'Личность и таланты, обе родовые линии, отношения, реализация, повторяющиеся уроки, '
        'направления развития и таблица ресурсов. Каждая точка схемы объяснена: что означает, '
        'как может проявляться и с чем поработать.\n\n'
        'Включены схема в полном качестве и полный файл для сохранения. '
        'Повторное открытие для этой даты бесплатно.\n\n'
        f"Стоимость: <b>{MATRIX_REPORT_PRICE} токенов</b>\nБаланс: <b>{user['tokens']}</b>",
        confirm_keyboard('matrix:report:buy:' + user['birth_date'], 'menu:matrix', f'Получить за {MATRIX_REPORT_PRICE} токенов'))


async def deliver_report(callback, user, *, purchase=False, page=0, document=False):
    uid = callback.from_user.id
    if uid in _matrix_active:
        await callback.message.answer('Твой разбор уже готовится.')
        return
    _matrix_active.add(uid)
    status = None
    output_path = None
    try:
        if purchase or document or page == 0:
            status = await callback.message.answer('✦ Загружаю разбор матрицы: проверяю значения и собираю разделы…')
        async with _matrix_workers:
            chunks = await asyncio.to_thread(build_matrix_report, user)
            if document:
                output_path = await asyncio.to_thread(build_matrix_report_html, user)
        if purchase:
            result = await commerce.purchase_product(uid, 'matrix_report', user['birth_date'])
            if result.status == 'insufficient':
                await status.edit_text(f'Нужно {MATRIX_REPORT_PRICE} токенов. Баланс: {result.balance}.', reply_markup=tokens_menu_keyboard())
                return
        if document:
            await callback.message.answer_document(FSInputFile(output_path, filename='Разбор моей матрицы.html'),
                caption='Полный разбор со схемой. Открой файл в браузере; он работает без интернета. Через «Печать» можно сохранить PDF.')
        else:
            page = max(0, min(page, len(chunks)-1))
            if page == 0 or purchase:
                await callback.message.answer(chunks[page], reply_markup=report_keyboard(page, len(chunks)))
            else:
                await render(callback, chunks[page], report_keyboard(page, len(chunks)))
        if status:
            await status.edit_text('Разбор готов. Он сохранён в твоих покупках — возвращайся без повторной оплаты ✦')
    except Exception:
        logging.exception('Matrix report delivery failed')
        text = 'Не удалось подготовить или отправить разбор. Попробуй открыть его ещё раз. Купленный доступ сохраняется, повторного списания не будет.'
        if status:
            await status.edit_text(text)
        else:
            await callback.message.answer(text)
    finally:
        try:
            if output_path:
                Path(output_path).unlink(missing_ok=True)
        finally:
            _matrix_active.discard(uid)


@router.callback_query(F.data.startswith('matrix:report:'))
async def report_actions(callback: CallbackQuery):
    await callback.answer()
    if callback.data == 'matrix:report:noop':
        return
    user = await load_matrix_user(callback.from_user.id)
    if not user or not user['birth_date']:
        await render(callback, 'Сначала заполни дату рождения.', main_menu_keyboard(callback.from_user.id))
        return
    purchase = callback.data.startswith('matrix:report:buy:')
    if purchase and callback.data.rsplit(':', 1)[-1] != user['birth_date']:
        await render(callback, 'Дата в профиле изменилась. Открой актуальное предложение.', matrix_menu_keyboard(user))
        return
    if not user['_report_unlocked'] and not purchase:
        await render(callback, 'Подробный разбор ещё не открыт.', matrix_menu_keyboard(user))
        return
    if purchase and not user['_report_unlocked'] and user['tokens'] < MATRIX_REPORT_PRICE:
        await render(callback, f'Для разбора нужно {MATRIX_REPORT_PRICE} токенов. Баланс: {user["tokens"]}.', tokens_menu_keyboard())
        return
    page = 0
    if callback.data.startswith('matrix:report:page:'):
        try:
            page = int(callback.data.rsplit(':', 1)[-1])
        except ValueError:
            return
    await deliver_report(callback, user, purchase=purchase, page=page, document=callback.data == 'matrix:report:download')


@router.callback_query(F.data == 'profile:consent')
async def profile_consent(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await upsert_user(callback.from_user.id, callback.from_user.full_name, callback.from_user.username)
    await accept_consent(callback.from_user.id)
    await start_profile_flow(callback, state, await get_user(callback.from_user.id))


@router.message(Command('privacy'))
async def privacy(message: Message):
    await message.answer(privacy_text())


TIMEZONES = {
    'kaliningrad': ('Калининград · UTC+2', 'Europe/Kaliningrad'),
    'moscow': ('Москва · UTC+3', 'Europe/Moscow'),
    'samara': ('Самара · UTC+4', 'Europe/Samara'),
    'ekaterinburg': ('Екатеринбург · UTC+5', 'Asia/Yekaterinburg'),
    'omsk': ('Омск · UTC+6', 'Asia/Omsk'),
    'krasnoyarsk': ('Красноярск · UTC+7', 'Asia/Krasnoyarsk'),
    'irkutsk': ('Иркутск · UTC+8', 'Asia/Irkutsk'),
    'yakutsk': ('Якутск · UTC+9', 'Asia/Yakutsk'),
    'vladivostok': ('Владивосток · UTC+10', 'Asia/Vladivostok'),
    'magadan': ('Магадан · UTC+11', 'Asia/Magadan'),
    'kamchatka': ('Камчатка · UTC+12', 'Asia/Kamchatka'),
}


@router.callback_query(F.data == 'profile:timezone')
async def timezone_menu(callback: CallbackQuery):
    await callback.answer()
    rows = [[InlineKeyboardButton(text=title, callback_data='profile:tz:' + key)] for key, (title, _) in TIMEZONES.items()]
    rows.append([InlineKeyboardButton(text='⬅️ Профиль', callback_data='menu:profile')])
    rows.append([InlineKeyboardButton(text='⏰ Доставка расклада', callback_data='menu:schedule')])
    await render(callback, '<b>Часовой пояс</b>\n\nВыбери пояс, в котором хочешь получать расклады. Это настройка текущего дня и доставки; место рождения в профиле сохраняется.', InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith('profile:tz:'))
async def timezone_save(callback: CallbackQuery):
    await callback.answer()
    choice = TIMEZONES.get(callback.data.rsplit(':', 1)[-1])
    if not choice:
        return
    from db import DB_NAME
    async with aiosqlite.connect(DB_NAME) as connection:
        await connection.execute('UPDATE users SET timezone=?,updated_at=? WHERE telegram_id=?',
                                 (choice[1], datetime.now(timezone.utc).isoformat(), callback.from_user.id))
        await connection.commit()
    await render(callback, 'Часовой пояс сохранён: ' + e(choice[0]), profile_menu_keyboard(True))


@router.message(Command('terms'))
async def terms(message: Message):
    await message.answer(terms_text())


@router.callback_query()
async def stale_button(callback: CallbackQuery):
    await callback.answer('Эта кнопка устарела. Открой /menu и продолжи.', show_alert=True)

@router.message()
async def unknown_message(message: Message):
    if message.text and message.text.startswith("/"):
        return
    await message.answer("Открой <b>📋 Меню</b> или нажми <b>/menu</b>.", reply_markup=main_reply_keyboard())


# ---------------------------
# Main
# ---------------------------

async def main():
    global BOT_USERNAME, bot
    settings = get_settings()
    settings.validate_startup()
    await init_db()
    await commerce.init_commerce()
    launch_result = run_configured_actions()
    if launch_result['status'] != 'not_requested':
        logging.info('Owner launch action: %s', launch_result)
    await init_app()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    tasks = []
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        await bot.set_my_commands([
            BotCommand(command='menu', description='Главное меню'),
            BotCommand(command='profile', description='Мой профиль'),
            BotCommand(command='tokens', description='Кошелёк и покупки'),
            BotCommand(command='paysupport', description='Помощь с оплатой'),
            BotCommand(command='privacy', description='Данные и конфиденциальность'),
            BotCommand(command='terms', description='Условия использования'),
        ])
        tasks = [asyncio.create_task(notification_loop()), asyncio.create_task(reconciliation_loop(bot))]
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
