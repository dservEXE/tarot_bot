"""Customer checkout, payment support and successful-payment handlers."""
import asyncio
import html
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery

import commerce
from config import get_settings
from db import upsert_user
from legal_documents import terms_text, privacy_text
from yookassa_provider import ProviderError, check_order, rubles, start_checkout


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def button(text, data):
    style = ('success' if data.startswith(('tokens:buy:', 'payments:create:')) else
             'primary' if data.startswith('payments:check:') else None)
    return InlineKeyboardButton(text=text, callback_data=data, style=style)


def package_price_text(package, settings) -> str:
    if settings.payment_provider == 'stars':
        return f'{package.stars} ⭐'
    price = f'{rubles(package.amount_kopecks)} ₽'
    if package.savings_kopecks:
        price += f' · −{package.discount_percent}%'
    return price


def package_discount_text(package, settings) -> str:
    if settings.payment_provider == 'stars' or not package.savings_kopecks:
        return ''
    return (f'🔥 Скидка {package.discount_percent}%: стандартная цена — '
            f'{rubles(package.standard_amount_kopecks)} ₽, экономия '
            f'{rubles(package.savings_kopecks)} ₽.')


def wallet_keyboard():
    settings = get_settings()
    rows = []
    packages = []
    for key, package in commerce.PACKAGES.items():
        price = package_price_text(package, settings)
        packages.append(button(f'{package.title} · {price}', f'tokens:buy:{key}'))
    rows.extend([packages[:2], packages[2:4], packages[4:6], packages[6:]])
    rows += [[button('🧾 Мои платежи', 'payments:history')], [button('⬅️ Главное меню', 'back:main')]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_text():
    settings = get_settings()
    contact = html.escape(settings.support_contact) if settings.support_contact else 'Контакт поддержки будет указан перед запуском оплаты.'
    return ('<b>Поддержка по оплате</b>\n\n' + contact + '\n\n'
            'Сообщи номер заказа из «Мои платежи», дату и суть вопроса. '
            'Пароли, данные карты и коды из банка присылать не нужно. '
            'Если деньги списаны, нажми «Проверить оплату»: повторно платить не требуется.\n\n'
            'Возврат рассматривает поддержка; при подтверждении он поступает через исходный способ оплаты. '
            'Токены этого пакета снимаются с баланса; если они уже потрачены, баланс может стать отрицательным.')


def make_payment_router() -> Router:
    router = Router(name='payments')
    active_checkouts: set[int] = set()
    last_checkout: dict[int, float] = {}
    active_checks: set[int] = set()
    last_check: dict[int, float] = {}

    @router.callback_query(F.data.in_({'payments:terms', 'payments:privacy'}))
    async def documents(callback: CallbackQuery):
        await callback.answer()
        if callback.message.chat.type == 'private':
            text = terms_text() if callback.data == 'payments:terms' else privacy_text()
            rows = []
            if callback.data == 'payments:privacy':
                rows.append([button('Условия покупки', 'payments:terms')])
            rows.append([button('⬅️ Кошелёк', 'menu:tokens')])
            await callback.message.answer(text, reply_markup=kb(*rows))

    @router.message(Command('paysupport', 'support'))
    async def support(message: Message):
        if message.chat.type == 'private':
            await message.answer(support_text(), reply_markup=kb([button('🧾 Мои платежи', 'payments:history')], [button('⬅️ Меню', 'back:main')]))

    @router.callback_query(F.data == 'payments:history')
    async def history(callback: CallbackQuery):
        await callback.answer()
        if callback.message.chat.type != 'private':
            return
        orders = await commerce.list_payments(user_id=callback.from_user.id, limit=8)
        labels = {'pending': 'ожидает оплаты', 'paid': 'оплачен', 'canceled': 'отменён',
                  'refunded': 'возвращён', 'refund_pending': 'возврат проверяется'}
        text = '<b>🧾 Последние платежи</b>\n\n'
        rows = []
        for order in orders:
            amount = f"{order['stars']} ⭐" if order['currency'] == 'XTR' else f"{rubles(order['amount_kopecks'])} ₽"
            text += f"<code>{order['order_id']}</code>\n{order['tokens']} токенов · {amount} · {labels.get(order['status'], 'проверяется')}\n\n"
            if order['provider'] == 'yookassa' and order['status'] == 'pending':
                rows.append([button(f"Проверить · {order['tokens']} токенов", 'payments:check:' + order['order_id'])])
        if not orders:
            text += 'Покупок пока нет.'
        text += '\n/paysupport — помощь с оплатой и возвратом'
        rows.append([button('⬅️ Кошелёк', 'menu:tokens')])
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.callback_query(F.data.startswith('tokens:buy:'))
    async def preview(callback: CallbackQuery):
        await callback.answer()
        if callback.message.chat.type != 'private':
            return
        settings = get_settings()
        key = callback.data.rsplit(':', 1)[-1]
        package = commerce.PACKAGES.get(key)
        if not package:
            await callback.message.answer('Такого пакета нет. Открой /tokens.')
            return
        if not settings.payment_allowed(callback.from_user.id):
            await callback.message.answer('💳 Пополнение готовится к запуску. Пока оплату не принимаем.\n\n/paysupport — поддержка')
            return
        price = package_price_text(package, settings)
        discount = package_discount_text(package, settings)
        test = '\n<b>Тестовый магазин — это проверка интеграции.</b>\n' if settings.payment_provider == 'yookassa' and settings.yookassa_test else ''
        if settings.payments_admin_only:
            test += '\n<b>Закрытая проверка владельца.</b> Оплата доступна только администратору.\n'
            if settings.payment_provider == 'yookassa' and not settings.yookassa_test:
                test += '<b>Рабочий магазин: деньги будут списаны по-настоящему.</b>\n'
        if settings.payment_documents_in_bot:
            documents = ('Условия покупки и сведения об обработке данных доступны на кнопках ниже '
                         'и через /terms и /privacy. Продолжая, ты принимаешь условия покупки.')
        elif settings.payments_admin_only and any(
                name in settings.payment_problems() for name in ('TERMS_URL', 'PRIVACY_URL')):
            documents = 'Документы готовятся к публикации. Публичный приём оплаты закрыт.'
        else:
            documents = (f'<a href="{html.escape(settings.terms_url, quote=True)}">Условия покупки</a> · '
                         f'<a href="{html.escape(settings.privacy_url, quote=True)}">Конфиденциальность</a>\n'
                         'Продолжая, ты принимаешь условия покупки.')
        text = (f'<b>Пополнение: {package.tokens} токенов</b>\n\nК оплате: <b>{price}</b>\n'
                + (discount + '\n' if discount else '') +
                'Разовый платёж. Автоматических списаний нет.\n'
                'Токены используются внутри бота для раскладов и матрицы.\n' + test +
                f'\nПоддержка: {html.escape(settings.support_contact)}\n\n'
                + documents)
        label = 'Перейти к проверке оплаты' if settings.payments_admin_only else 'Согласен · перейти к оплате'
        await callback.message.answer(text, reply_markup=kb(
            [button('Условия покупки', 'payments:terms'), button('О данных', 'payments:privacy')],
            [button(label, f'payments:create:{key}')], [button('⬅️ Кошелёк', 'menu:tokens')]))

    @router.callback_query(F.data.startswith('payments:create:'))
    async def create(callback: CallbackQuery):
        await callback.answer()
        uid = callback.from_user.id
        if callback.message.chat.type != 'private':
            return
        settings = get_settings()
        if not settings.payment_allowed(uid):
            await callback.message.answer('Приём платежей пока не включён. /paysupport')
            return
        key = callback.data.rsplit(':', 1)[-1]
        if key not in commerce.PACKAGES:
            return
        if uid in active_checkouts or time.monotonic() - last_checkout.get(uid, 0) < 5:
            await callback.message.answer('Счёт уже создаётся. Подожди несколько секунд.')
            return
        active_checkouts.add(uid)
        last_checkout[uid] = time.monotonic()
        if len(last_checkout) > 2000:
            cutoff = time.monotonic() - 60
            for old in [k for k, v in last_checkout.items() if v < cutoff]:
                last_checkout.pop(old, None)
        try:
            await upsert_user(uid, callback.from_user.full_name, callback.from_user.username)
            package = commerce.PACKAGES[key]
            if settings.payment_provider == 'stars':
                order = await commerce.create_order(uid, key)
                await callback.message.answer_invoice(
                    title=package.title, description='Токены для персональных раскладов и матрицы. Разовая покупка.',
                    payload=order['payload'], currency='XTR', provider_token='',
                    prices=[LabeledPrice(label=package.title, amount=package.stars)])
            else:
                me = await callback.bot.get_me()
                return_url = settings.payment_return_url or f'https://t.me/{me.username}?start=payment'
                order = await start_checkout(uid, key, return_url)
                rows = []
                if order.get('confirmation_url'):
                    rows.append([InlineKeyboardButton(text=f"Оплатить {rubles(order['amount_kopecks'])} ₽", url=order['confirmation_url'], style='success')])
                rows.append([button('Проверить оплату', 'payments:check:' + order['order_id'])])
                rows.append([button('⬅️ Кошелёк', 'menu:tokens')])
                await callback.message.answer(
                    f"<b>Счёт готов</b>\n\n{package.tokens} токенов · {rubles(package.amount_kopecks)} ₽"
                    + (f" · −{package.discount_percent}%" if package.savings_kopecks else '') + "\n"
                    f"Номер: <code>{order['order_id']}</code>\n\n"
                    'Выбери способ на странице ЮKassa. После оплаты токены начислятся автоматически. '
                    'Если ты уже оплатил, не создавай новый платёж — нажми «Проверить оплату».',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except (ProviderError, commerce.CommerceError):
            logging.warning('Checkout pending or unavailable for user %s', uid)
            await callback.message.answer('Не удалось получить платёжную страницу. Проверь «Мои платежи»: заказ мог сохраниться.\n/paysupport — помощь',
                                          reply_markup=kb([button('Мои платежи', 'payments:history')]))
        finally:
            active_checkouts.discard(uid)

    @router.callback_query(F.data.startswith('payments:check:'))
    async def check(callback: CallbackQuery):
        await callback.answer('Проверяю платёж…')
        if callback.message.chat.type != 'private':
            return
        order_id = callback.data.rsplit(':', 1)[-1]
        uid = callback.from_user.id
        if uid in active_checks or time.monotonic() - last_check.get(uid, 0) < 3:
            return
        active_checks.add(uid)
        last_check[uid] = time.monotonic()
        if len(last_check) > 2000:
            for old in [k for k, v in last_check.items() if v < time.monotonic()-60]:
                last_check.pop(old, None)
        try:
            result = await check_order(order_id, callback.from_user.id)
            text = {'paid': '✅ Платёж подтверждён. Токены уже на балансе.',
                    'canceled': 'Счёт отменён или срок оплаты истёк. Можно создать новый.',
                    'refunded': 'Платёж возвращён.', 'refund_pending': 'Возврат проверяется.'}.get(result['status'],
                    'Пока ждём подтверждение ЮKassa. Если банк уже списал деньги, не плати повторно.')
            order = await commerce.get_payment(order_id)
            rows = []
            if result['status'] == 'pending' and order and order.get('confirmation_url'):
                rows.append([InlineKeyboardButton(text='Открыть платёжную страницу', url=order['confirmation_url'])])
                rows.append([button('Проверить ещё раз', 'payments:check:' + order_id)])
            rows.append([button('⬅️ Кошелёк', 'menu:tokens')])
            await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except (ProviderError, commerce.CommerceError, ValueError):
            await callback.message.answer('Не удалось подтвердить платёж. Повтори проверку позже или обратись в /paysupport.')
        finally:
            active_checks.discard(uid)

    @router.pre_checkout_query()
    async def precheckout(query: PreCheckoutQuery):
        try:
            settings = get_settings()
            if not settings.payment_allowed(query.from_user.id) or settings.payment_provider != 'stars':
                raise commerce.PaymentValidationError('Оплата временно отключена')
            await commerce.validate_pre_checkout(query.from_user.id, query.invoice_payload, query.currency, query.total_amount)
            await query.answer(ok=True)
        except (commerce.CommerceError, ValueError):
            await query.answer(ok=False, error_message='Счёт устарел или не соответствует заказу. Создай новый в /tokens.')

    @router.message(F.successful_payment)
    async def successful(message: Message):
        payment = message.successful_payment
        try:
            result = await commerce.confirm_payment(message.from_user.id, payment.invoice_payload, payment.currency,
                payment.total_amount, payment.telegram_payment_charge_id, payment.provider_payment_charge_id)
            await message.answer(f"✅ Оплата подтверждена. Баланс: <b>{result['balance']} токенов</b>.\n/paysupport — помощь", reply_markup=wallet_keyboard())
        except commerce.CommerceError:
            logging.error('A successful Telegram payment requires reconciliation')
            await message.answer('Платёж получен, но требует проверки. Не плати повторно; обратись в /paysupport.')

    @router.message(F.refunded_payment)
    async def refunded(message: Message):
        payment = message.refunded_payment
        try:
            await commerce.reconcile_refund(payment.telegram_payment_charge_id, message.chat.id, payment.currency, payment.total_amount)
            await message.answer('Возврат подтверждён. Баланс токенов обновлён. /tokens')
        except commerce.CommerceError:
            logging.error('Telegram refund requires reconciliation')

    return router
