"""YooKassa redirect payments. A return link is never proof of payment.

All credits follow an authenticated GET to the provider and full comparison with
the immutable local order. Durable idempotency keys survive process restarts.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import aiohttp

import commerce
from config import get_settings

API_URL = 'https://api.yookassa.ru/v3'


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, definitive: bool = False):
        super().__init__(message)
        self.definitive = definitive


def rubles(kopecks: int) -> str:
    return f'{kopecks // 100}.{kopecks % 100:02d}'


def to_kopecks(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError('Некорректная сумма провайдера')
        return int(amount * 100)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Некорректная сумма провайдера') from exc


async def _request(method: str, path: str, *, payload=None, key=None) -> dict:
    settings = get_settings()
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        raise ProviderError('ЮKassa пока не подключена', definitive=True)
    headers = {'Idempotence-Key': key} if key else {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=18)) as session:
            async with session.request(method, API_URL + path, json=payload, headers=headers,
                                       auth=aiohttp.BasicAuth(settings.yookassa_shop_id, settings.yookassa_secret_key)) as response:
                if response.status >= 400:
                    # Never put response bodies or authentication headers in logs/chat.
                    raise ProviderError(f'ЮKassa: HTTP {response.status}',
                                        definitive=response.status in {400, 401, 403, 404})
                result = await response.json()
                if not isinstance(result, dict):
                    raise ProviderError('Неожиданный ответ ЮKassa')
                return result
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ProviderError('ЮKassa временно не отвечает. Заказ сохранён для повторной проверки.') from exc


def validate_payment(order: dict, payment: dict) -> None:
    settings = get_settings()
    if payment.get('id') != order['provider_payment_id']:
        raise commerce.PaymentValidationError('Идентификатор платежа не совпал')
    metadata = payment.get('metadata') or {}
    if metadata.get('order_id') != order['order_id'] or str(metadata.get('user_id')) != str(order['user_id']):
        raise commerce.PaymentValidationError('Платёж относится к другому заказу')
    if payment.get('recipient', {}).get('account_id') != settings.yookassa_shop_id:
        raise commerce.PaymentValidationError('Получатель платежа не совпал')
    if payment.get('test') is not settings.yookassa_test:
        raise commerce.PaymentValidationError('Тестовый и рабочий режимы оплаты не совпали')
    amount = payment.get('amount') or {}
    if amount.get('currency') != 'RUB' or to_kopecks(amount.get('value', '')) != order['amount_kopecks']:
        raise commerce.PaymentValidationError('Сумма платежа не совпала')


async def _create_remote(order: dict) -> dict:
    settings = get_settings()
    if not settings.payment_allowed(order['user_id']) or settings.payment_provider != 'yookassa':
        raise ProviderError('Создание новых платежей для этого аккаунта отключено', definitive=True)
    stored_request = order.get('request_json')
    if not stored_request:
        raise ProviderError('Платёжная заявка не сохранена')
    created = datetime.fromisoformat(order['created_at'])
    if datetime.now(timezone.utc) - created > timedelta(hours=23):
        raise ProviderError('Заказ требует ручной сверки: срок повторного запроса истёк')
    payment = await _request('POST', '/payments', payload=json.loads(stored_request), key=order['order_id'])
    payment_id = payment.get('id', '')
    if not payment_id or not all(c.isalnum() or c == '-' for c in payment_id):
        raise ProviderError('ЮKassa не вернула номер платежа')
    url = payment.get('confirmation', {}).get('confirmation_url')
    if url and (urlparse(url).scheme != 'https' or not urlparse(url).netloc):
        raise ProviderError('Некорректная платёжная ссылка')
    validate_payment(dict(order, provider_payment_id=payment_id), payment)
    return await commerce.attach_external_payment(order['order_id'], payment_id, url)


async def start_checkout(user_id: int, package_id: str, return_url: str) -> dict:
    settings = get_settings()
    if not settings.payment_allowed(user_id) or settings.payment_provider != 'yookassa':
        raise ProviderError('Пополнение скоро откроется', definitive=True)
    if urlparse(return_url).scheme != 'https' or not urlparse(return_url).netloc:
        raise ProviderError('Не настроен адрес возврата из оплаты', definitive=True)
    order = await commerce.create_external_order(user_id, package_id)
    payload = {
        'amount': {'value': rubles(order['amount_kopecks']), 'currency': 'RUB'},
        'capture': True, 'confirmation': {'type': 'redirect', 'return_url': return_url},
        'description': f"{settings.brand_name[:60]}: доступ к сервису, {order['tokens']} токенов",
        'metadata': {'order_id': order['order_id'], 'user_id': str(user_id), 'package_id': package_id},
    }
    if settings.yookassa_method == 'sbp':
        payload['payment_method_data'] = {'type': 'sbp'}
    elif settings.yookassa_method != 'auto':
        raise ProviderError('Допустимые способы: auto или sbp', definitive=True)
    # NPD receipts are handled through My Tax/operator integration, NOT a fake 54-FZ receipt.
    order = await commerce.save_external_request(order['order_id'], json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return await _create_remote(order)


async def check_order(order_id: str, user_id: int | None = None) -> dict:
    order = await commerce.get_payment(order_id)
    if not order or order['provider'] != 'yookassa' or (user_id is not None and order['user_id'] != user_id):
        raise commerce.PaymentValidationError('Заказ не найден')
    if order['status'] in {'paid', 'refunded', 'refund_pending', 'canceled'}:
        return {'status': order['status'], 'credited': False, 'order_id': order_id, 'tokens': order['tokens']}
    if not order['provider_payment_id']:
        order = await _create_remote(order)
    payment = await _request('GET', '/payments/' + order['provider_payment_id'])
    validate_payment(order, payment)
    if payment.get('status') == 'succeeded' and payment.get('paid') is True:
        result = await commerce.confirm_external_payment(
            order_id, payment['id'], to_kopecks(payment['amount']['value']), 'RUB',
            order['user_id'], paid=True, status='succeeded')
        return dict(result, status='paid')
    if payment.get('status') == 'canceled':
        await commerce.mark_external_canceled(order_id)
        return {'status': 'canceled', 'credited': False, 'order_id': order_id}
    return {'status': 'pending', 'credited': False, 'order_id': order_id}


async def _process_refund(order: dict) -> dict:
    attempt_id = order.get('attempt_id') or order.get('refund_attempt_id')
    refund_id = order.get('provider_refund_id')
    if refund_id:
        refund = await _request('GET', '/refunds/' + refund_id)
        if refund.get('id') != refund_id:
            raise commerce.PaymentValidationError('Идентификатор возврата не совпал')
    else:
        created = datetime.fromisoformat(order.get('attempt_created_at') or order.get('refund_created_at') or order['created_at'])
        if datetime.now(timezone.utc) - created > timedelta(hours=23):
            await commerce.mark_refund_uncertain(attempt_id, 'Истёк срок безопасного повтора; сверить в кабинете ЮKassa')
            return {'status': 'refund_pending', 'amount_kopecks': order['amount_kopecks']}
        body = json.loads(order['refund_request_json']) if order.get('refund_request_json') else {
            'payment_id': order['provider_payment_id'],
            'amount': {'value': rubles(order['amount_kopecks']), 'currency': 'RUB'},
        }
        await commerce.save_refund_request(attempt_id, body)
        refund = await _request('POST', '/refunds', key=attempt_id, payload=body)
        refund_id = refund.get('id')
        if not refund_id:
            raise ProviderError('ЮKassa не вернула номер возврата')
    if refund.get('payment_id') != order['provider_payment_id']:
        raise commerce.PaymentValidationError('Возврат другого платежа')
    amount = refund.get('amount', {})
    if amount.get('currency') != 'RUB' or to_kopecks(amount.get('value', '')) != order['amount_kopecks']:
        raise commerce.PaymentValidationError('Сумма возврата не совпала')
    await commerce.attach_external_refund(attempt_id, refund_id)
    if refund.get('status') == 'succeeded':
        await commerce.finish_refund(attempt_id, True)
        return {'status': 'refunded', 'amount_kopecks': order['amount_kopecks']}
    if refund.get('status') == 'canceled':
        await commerce.finish_refund(attempt_id, False, 'Возврат отменён провайдером')
        return {'status': 'paid', 'amount_kopecks': order['amount_kopecks']}
    return {'status': 'refund_pending', 'amount_kopecks': order['amount_kopecks']}


async def refund_order(order_id: str, admin_id: int, reason: str) -> dict:
    if admin_id not in get_settings().admin_ids:
        raise commerce.CommerceError('Нет доступа')
    current = await commerce.get_payment(order_id)
    if not current or current['provider'] != 'yookassa':
        raise commerce.CommerceError('Этот платёж не относится к ЮKassa')
    order = await commerce.begin_refund(admin_id, order_id, reason)
    order['attempt_created_at'] = datetime.now(timezone.utc).isoformat()
    try:
        return await _process_refund(order)
    except (ProviderError, commerce.PaymentValidationError, ValueError) as exc:
        await commerce.mark_refund_uncertain(order['attempt_id'], type(exc).__name__)
        return {'status': 'refund_pending', 'amount_kopecks': order['amount_kopecks'],
                'error': 'Ответ требует сверки. Повторное списание не выполняется.'}


async def reconciliation_loop(bot) -> None:
    """Polling works without a public webhook URL; runs only with configured keys."""
    while True:
        try:
            settings = get_settings()
            if settings.yookassa_shop_id and settings.yookassa_secret_key:
                for order in await commerce.list_pending_external_payments(limit=100):
                    try:
                        result = await check_order(order['order_id'])
                        if result.get('credited'):
                            try:
                                await bot.send_message(order['user_id'],
                                    f"✅ Оплата подтверждена. Начислено <b>{order['tokens']} токенов</b>.\nБаланс: {result['balance']}\n/tokens — кошелёк")
                            except Exception:
                                logging.warning('Payment credited; customer notification not delivered')
                    except (ProviderError, commerce.CommerceError, ValueError):
                        logging.warning('Payment reconciliation deferred for order %s', order['order_id'])
                    await asyncio.sleep(0.15)
                for order in await commerce.list_pending_external_refunds(limit=100):
                    try:
                        await _process_refund(order)
                    except (ProviderError, commerce.CommerceError, ValueError):
                        logging.warning('Refund reconciliation deferred for order %s', order['order_id'])
        except Exception:
            logging.exception('Payment reconciliation cycle failed')
        await asyncio.sleep(30)
