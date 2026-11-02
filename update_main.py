"""One-time source migration; kept local, not included in release."""
import ast
import re
from pathlib import Path

path = Path(__file__).with_name('main.py')
source = path.read_text(encoding='utf-8')
replacements = {}

def replace(name, code):
    replacements[name] = code.strip() + '\n'

replace('is_admin', '''
def is_admin(user_id: int | None) -> bool:
    return user_id in get_settings().admin_ids
''')
replace('tokens_menu_keyboard', '''
def tokens_menu_keyboard() -> InlineKeyboardMarkup:
    return wallet_keyboard()
''')
replace('send_main_menu', '''
async def send_main_menu(target, state=None):
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    text = (
        f"<b>✦ {e(get_settings().brand_name)}</b>\\n\\n"
        "Твой день. Твой ритм. Точка опоры.\\n\\n"
        "Персональные расклады учитывают дату, время и место рождения. "
        "Краткий расклад — бесплатно каждый день. В полном — отношения, дела, возможности и практический совет.\\n\\n"
        "<b>Матрица судьбы</b> помогает рассмотреть таланты, привычные сценарии и направления развития.\\n\\n"
        "Выбери, с чего начнём."
    )
    if user and not profile_complete(user):
        text += "\\n\\n👤 Сначала заверши профиль — раздел «Профиль»."
    await render(target, text, main_menu_keyboard(target.from_user.id))
''')
replace('send_tokens_menu', '''
async def send_tokens_menu(target, state=None):
    if state:
        await state.clear()
    user = await get_user(target.from_user.id)
    tokens = int(user['tokens'] or 0) if user else 0
    text = (
        f"<b>◈ Твой кошелёк</b>\\n\\nБаланс: <b>{tokens} токенов</b>\\n\\n"
        f"Полный расклад на день — {FULL_READING_PRICE}\\n"
        f"Совместимость на день — {MATCH_PRICE}\\n"
        f"Подписка на 30 дней — {SUBSCRIPTION_PRICE}\\n"
        f"Схема матрицы — {MATRIX_PRICE}\\n"
        f"Полный разбор матрицы со схемой — {MATRIX_REPORT_PRICE}\\n\\n"
        "Цена каждого пакета указана на кнопке. Это разовые покупки, без автосписаний."
    )
    if not get_settings().payments_enabled:
        text += "\\n\\n<i>Пополнение скоро откроется. Пока оплату не принимаем.</i>"
    link = referral_link(target.from_user.id)
    if link:
        text += f"\\n\\nПриглашение другу: {e(link)}"
    await render(target, text, tokens_menu_keyboard())
''')
replace('send_about', '''
async def send_about(target, state=None):
    if state:
        await state.clear()
    ai = ("Полный расклад может дополняться ИИ: он раскрывает уже рассчитанную тему, сохраняя исходные значения. "
          if get_settings().ai_enabled else "Сейчас работает персональный алгоритм интерпретаций. ")
    text = (
        f"<b>✦ {e(get_settings().brand_name)}</b>\\n\\n"
        "Место для ежедневной рефлексии через символы Таро, нумерологию и астрологическую традицию.\\n\\n"
        "Дата, время и место рождения участвуют в персональном выборе расклада. "
        "При одинаковом профиле в течение дня карта остаётся прежней. " + ai +
        "\\n\\nМатрица рассчитывается по дате рождения: для её чисел время и город не нужны. "
        "В схеме и разборе используются одни и те же формулы.\\n\\n"
        "Символические интерпретации не предсказывают неизбежные события и не заменяют решения специалиста.\\n\\n"
        "/terms — условия · /privacy — данные · /paysupport — поддержка"
    )
    await render(target, text, main_menu_keyboard(target.from_user.id))
''')
replace('matrix_access_active', '''
def matrix_access_active(user) -> bool:
    return bool(user and user.get('_matrix_unlocked', False))
''')
replace('matrix_menu_keyboard', '''
def matrix_menu_keyboard(user) -> InlineKeyboardMarkup:
    rows = []
    if matrix_access_active(user):
        rows.append([InlineKeyboardButton(text="✦ Открыть схему", callback_data="matrix:open")])
        rows.append([InlineKeyboardButton(text="Скачать PNG без сжатия", callback_data="matrix:download")])
    else:
        rows.append([InlineKeyboardButton(text=f"Схема · {MATRIX_PRICE} токенов", callback_data="matrix:buy_prompt")])
    report_open = bool(user and user.get('_report_unlocked'))
    rows.append([InlineKeyboardButton(
        text="Читать мой разбор" if report_open else f"Полный разбор · {MATRIX_REPORT_PRICE} токенов",
        callback_data="matrix:report:open" if report_open else "matrix:report:prompt")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
''')
replace('format_matrix_preview', '''
def format_matrix_preview(user) -> str:
    if not user or not user['birth_date']:
        return '<b>✦ Матрица судьбы</b>\\n\\nСначала заполни дату рождения.'
    matrix = calc_matrix(user['birth_date'])
    return (
        f"<b>✦ Матрица судьбы · {pretty_date(user['birth_date'])}</b>\\n\\n"
        f"Личность · <b>{matrix['A']} — {e(MATRIX_ARCANA[matrix['A']])}</b>\\n"
        f"Таланты · <b>{matrix['B']} — {e(MATRIX_ARCANA[matrix['B']])}</b>\\n"
        f"Внутренняя опора · <b>{matrix['E']} — {e(MATRIX_ARCANA[matrix['E']])}</b>\\n\\n"
        "<b>Схема</b> — 30 точек, родовые диагонали, линии отношений и реализации, направления и таблица ресурсов.\\n\\n"
        "<b>Полный разбор</b> — объяснение каждой позиции: сильные стороны, возможные трудности и конкретные шаги. "
        "Схема входит в разбор. Его можно читать по разделам и скачать целиком.\\n\\n"
        "Повторное открытие купленного материала для этой даты рождения бесплатно."
    )
''')
replace('format_matrix_buy_prompt', '''
def format_matrix_buy_prompt(user) -> str:
    return (
        f"<b>Твоя схема матрицы</b>\\n\\nДата: {pretty_date(user['birth_date'])}\\n"
        f"Стоимость: <b>{MATRIX_PRICE} токенов</b>\\nБаланс: <b>{user['tokens']}</b>\\n\\n"
        "Чёткая схема, подписи позиций и легенда. Можно скачать PNG в полном качестве. "
        "Повторное открытие для этой даты рождения — без оплаты.\\n\\n"
        f"Подробный текстовый разбор продаётся отдельно за {MATRIX_REPORT_PRICE} токенов и уже включает схему."
    )
''')
replace('send_matrix_menu', '''
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
''')
replace('matrix_buy_prompt', '''
@router.callback_query(F.data == "matrix:buy_prompt")
async def matrix_buy_prompt(callback: CallbackQuery):
    await callback.answer()
    user = await load_matrix_user(callback.from_user.id)
    if not user or not user['birth_date']:
        await render(callback, 'Сначала заполни дату рождения.', main_menu_keyboard(callback.from_user.id))
    elif matrix_access_active(user):
        await deliver_matrix(callback, user)
    else:
        await render(callback, format_matrix_buy_prompt(user), confirm_keyboard('matrix:buy', 'menu:matrix', f'Купить за {MATRIX_PRICE} токенов'))
''')
replace('matrix_buy', '''
@router.callback_query(F.data == "matrix:buy")
async def matrix_buy(callback: CallbackQuery):
    await callback.answer()
    user = await load_matrix_user(callback.from_user.id)
    if not user or not user['birth_date']:
        await render(callback, 'Сначала заполни дату рождения.', main_menu_keyboard(callback.from_user.id))
        return
    if matrix_access_active(user):
        await deliver_matrix(callback, user)
        return
    if user['tokens'] < MATRIX_PRICE:
        await render(callback, f'Для схемы нужно {MATRIX_PRICE} токенов. Баланс: {user["tokens"]}.', tokens_menu_keyboard())
        return
    await deliver_matrix(callback, user, purchase=True)
''')
replace('reading_full_pay', '''
@router.callback_query(F.data == "reading:full_pay")
async def reading_full_pay(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not profile_complete(user):
        await render(callback, 'Сначала заполни профиль.', main_menu_keyboard(callback.from_user.id))
        return
    local_date = current_local_date(user)
    reading = await get_daily_reading(user, local_date)
    result = await commerce.purchase_product(callback.from_user.id, 'full_reading', local_date.isoformat())
    if result.status == 'insufficient':
        await render(callback, f'Нужно {FULL_READING_PRICE} токенов. Баланс: {result.balance}.', tokens_menu_keyboard())
        return
    await render(callback, format_full_reading(reading, local_date), readings_menu_keyboard(await get_user(callback.from_user.id)))
''')
replace('sub_buy', '''
@router.callback_query(F.data == "sub:buy")
async def sub_buy(callback: CallbackQuery):
    await callback.answer()
    user = await get_user(callback.from_user.id)
    if not user or not profile_complete(user):
        await render(callback, 'Сначала заполни профиль — он нужен для ежедневных раскладов.', main_menu_keyboard(callback.from_user.id))
        return
    result = await commerce.purchase_product(callback.from_user.id, 'subscription')
    if result.status == 'insufficient':
        await render(callback, f'Для подписки нужно {SUBSCRIPTION_PRICE} токенов. Баланс: {result.balance}.', tokens_menu_keyboard())
        return
    await prepare_subscription_if_needed(callback.from_user.id)
    user = await get_user(callback.from_user.id)
    await render(callback,
        f"<b>Подписка активна до {pretty_iso_dt(user['subscription_until'])}</b>\\n\\n"
        "Полные расклады и совместимость доступны без отдельных списаний. Автопродления нет.\\n"
        "Доставку можно включить в разделе «Рассылка».", subscription_menu_keyboard(user))
''')
replace('subscription_menu_keyboard', '''
def subscription_menu_keyboard(user) -> InlineKeyboardMarkup:
    if subscription_active(user):
        return one_row_buttons(('Настроить доставку', 'menu:schedule'), back_cb='back:main')
    return one_row_buttons((f'30 дней · {SUBSCRIPTION_PRICE} токенов', 'sub:buy_prompt'), back_cb='back:main')
''')
replace('sub_cancel_prompt', '''
@router.callback_query(F.data == "sub:cancel_prompt")
async def sub_cancel_prompt(callback: CallbackQuery):
    await callback.answer()
    await render(callback, 'У подписки нет автопродления. Можно отключить ежедневную доставку: оплаченный доступ сохранится до конца срока.', confirm_keyboard('sub:cancel', 'menu:subscription', 'Отключить доставку'))
''')
replace('sub_cancel', '''
@router.callback_query(F.data == "sub:cancel")
async def sub_cancel(callback: CallbackQuery):
    await callback.answer()
    await set_notify_enabled(callback.from_user.id, False)
    await render(callback, 'Доставка отключена. Оплаченный доступ сохранён до конца срока. Автосписаний нет.', subscription_menu_keyboard(await get_user(callback.from_user.id)))
''')
replace('birth_place', '''
@router.message(Onboarding.birth_place)
async def birth_place(message: Message, state: FSMContext):
    place = (message.text or '').strip()
    if not 2 <= len(place) <= 120 or '\\n' in place or place.startswith('/'):
        await message.answer('Напиши город и страну одной строкой, от 2 до 120 символов. Например: Казань, Россия.')
        return
    await set_birth_place(message.from_user.id, place)
    await state.clear()
    await message.answer('<b>Профиль сохранён.</b> Дату, время и место рождения будем учитывать в раскладах.\\nЧасовой пояс доставки можно выбрать в профиле.', reply_markup=main_reply_keyboard())
    await send_main_menu(message, state)
''')
replace('main', '''
async def main():
    global BOT_USERNAME, bot
    settings = get_settings()
    settings.validate_startup()
    await init_db()
    await commerce.init_commerce()
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
''')

remove_names = {'AdminStates', 'tokens_buy', 'cmd_admin', 'send_admin_panel', 'get_admin_users'}
tree = ast.parse(source)
lines = source.splitlines(keepends=True)
changes = []
for node in tree.body:
    name = getattr(node, 'name', '')
    if name in replacements or name in remove_names or name.startswith('admin_'):
        start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])]) - 1
        changes.append((start, node.end_lineno, replacements.get(name, '')))
for start, end, code in reversed(changes):
    lines[start:end] = [code]
source = ''.join(lines)
source = re.sub(r'^BOT_TOKEN\s*=.*\n', '', source, flags=re.M)
source = re.sub(r'^ADMIN_ID\s*=.*\n', '', source, flags=re.M)
source = source.replace('from aiogram import Bot, Dispatcher, F, Router', 'from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router')
source = source.replace('    CallbackQuery,', '    BotCommand,\n    CallbackQuery,')
source = source.replace('from db import (', 'from config import get_settings\n\nfrom db import (')
source = source.replace('from matrix_image import build_matrix_image', '''from matrix_image import build_matrix_image
from matrix_model import calc_matrix, ARCANA as MATRIX_ARCANA
from matrix_report import build_matrix_report, build_matrix_report_html
import commerce
from app_services import init_app, has_consent, accept_consent, get_daily_reading
from admin_panel import make_admin_router
from payment_router import make_payment_router, wallet_keyboard
from yookassa_provider import reconciliation_loop''')
source = source.replace('MATRIX_PRICE = 349', "MATRIX_PRICE = commerce.PRODUCT_PRICES['matrix']\nMATRIX_REPORT_PRICE = commerce.PRODUCT_PRICES['matrix_report']")
source = re.sub(r'bot = Bot\(\s*token=BOT_TOKEN,\s*default=DefaultBotProperties\(parse_mode=ParseMode.HTML\),\s*\)', 'bot: Bot | None = None', source)
source = source.replace('dp.include_router(router)', '''dp.include_router(make_admin_router(set(get_settings().admin_ids)))
dp.include_router(make_payment_router())
dp.include_router(router)''')
# User-facing wording and an unnecessary immediate subscription cancellation are fixed above.
source = source.replace('Это одна из самых сильных функций для длительного удержания: человек возвращается к ней снова и снова.', '')
source = source.replace('отменить подписку', 'отключить доставку')
source = source.replace('Чем выше баланс, тем свободнее можно брать глубину без пауз.', '')
source = source.replace('await start_profile_flow(callback.message, state, user)', 'await start_profile_flow(callback, state, user)')
source = source.replace('year = int(data.get("birth_year"))', 'year = int(data.get("birth_year") or (user["birth_date"] or "2000").split("-")[0])')
source = source.replace('month = int(data.get("birth_month"))', 'month = int(data.get("birth_month") or (user["birth_date"] or "2000-01").split("-")[1])')
source = source.replace('if not await spend_tokens(callback.from_user.id, MATCH_PRICE):', "result = await commerce.purchase_product(callback.from_user.id, 'match', local_date.isoformat())\n    if result.status == 'insufficient':")
source = source.replace('    await set_paid_match_date(callback.from_user.id, local_date.isoformat())\n', '')
source = source.replace('    other_id = int(callback.data.split(":")[-1])', '    try:\n        other_id = int(callback.data.split(":")[-1])\n    except ValueError:\n        return')
# Only full reading paths use optional AI; the original short reading is unchanged.
source = source.replace('reading = generate_daily_reading(user, local_date)\n        await render(callback, format_full_reading', 'reading = await get_daily_reading(user, local_date)\n        await render(callback, format_full_reading')
source = source.replace('reading = generate_daily_reading(user, reading_date)\n                text = format_full_reading', 'reading = await get_daily_reading(user, reading_date)\n                text = format_full_reading')
source = source.replace('📍 <b>Место рождения:</b> {e(reading[\'birth_place_hint\'])}', '📍 <b>Личный акцент:</b> {e(reading[\'birth_place_hint\'])}')
source = source.replace('🤝 <b>Контактность:</b>', '<i>Условные акценты дня, не вероятности событий:</i>\n🤝 <b>Контактность:</b>')
source = source.replace('🗝 <b>Фраза дня:</b> {e(reading[\'mantra\'])}', '🗝 <b>Фраза дня:</b> {e(reading[\'mantra\'])}\n\n<i>{"Интерпретация дополнена ИИ" if reading.get("generation_source") == "ai" else "Персональная алгоритмическая интерпретация"}</i>')
# Consent is requested at first actual profile collection, including legacy profiles when edited.
source = source.replace('async def start_profile_flow(target: Message | CallbackQuery, state: FSMContext, user) -> None:\n', '''async def start_profile_flow(target: Message | CallbackQuery, state: FSMContext, user) -> None:
    if not user:
        await render(target, 'Начни с /start.', main_menu_keyboard(target.from_user.id))
        return
    if not await has_consent(target.from_user.id):
        await state.clear()
        await render(target,
            '<b>Соберём твой профиль</b>\\n\\nПонадобятся дата, время и город рождения. '
            'Они сохраняются для персональных раскладов. Данные можно сбросить в профиле. '
            'Уведомления настраиваются отдельно.\\n\\n'
            '/privacy — обработка данных · /terms — условия\\n\\n'
            'Нажимая «Согласен», ты соглашаешься на обработку этих данных для работы сервиса.',
            one_row_buttons(('Согласен · заполнить профиль', 'profile:consent'), back_cb='back:main'))
        return
''')
# No future birthdays or forged callback ranges.
source = source.replace('    await state.update_data(birth_year=year)', '    if not 1900 <= year <= date_cls.today().year:\n        return\n    await state.update_data(birth_year=year)')
source = source.replace('    await state.update_data(birth_month=month)', '    if not 1 <= month <= 12:\n        return\n    await state.update_data(birth_month=month)')
source = source.replace('    await set_birth_date(callback.from_user.id, birth_date)', '    if date_cls.fromisoformat(birth_date) > date_cls.today():\n        await render(callback, "Дата рождения не может быть в будущем.", days_keyboard(year, month))\n        return\n    await set_birth_date(callback.from_user.id, birth_date)')
source = source.replace('    await state.update_data(birth_hour=hour)', '    if not 0 <= hour <= 23:\n        return\n    await state.update_data(birth_hour=hour)')
source = source.replace('    data = await state.get_data()\n    hour = int(data["birth_hour"])', '    if not 0 <= minute <= 59:\n        return\n    data = await state.get_data()\n    hour = int(data["birth_hour"])')
path.write_text(source, encoding='utf-8')
print('main.py migrated; embedded bot credential removed')
