import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import commerce
import db
import yookassa_provider as gateway
from config import get_settings


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, 'DB_NAME', str(Path(self.tmp.name) / 'test.db'))
        self.db_patch.start()
        self.settings = replace(get_settings(), payments_enabled=True, payment_provider='yookassa',
            yookassa_shop_id='123', yookassa_secret_key='test_fake', yookassa_test=True,
            yookassa_method='auto', support_contact='@support', seller_name='Тест', seller_inn='123456789012',
            terms_url='https://example.com/terms', privacy_url='https://example.com/privacy', admin_ids=frozenset({99}))
        self.settings_patch = patch.object(gateway, 'get_settings', return_value=self.settings)
        self.settings_patch.start()
        await db.init_db()
        await commerce.init_commerce()
        await db.upsert_user(1, 'Тест')

    async def asyncTearDown(self):
        self.settings_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def payment(self, order, **overrides):
        result = {'id': 'remote-123', 'status': 'pending', 'paid': False,
            'amount': {'value': '100.00', 'currency': 'RUB'}, 'test': True,
            'recipient': {'account_id': '123'}, 'metadata': {'order_id': order['order_id'], 'user_id': '1'},
            'confirmation': {'confirmation_url': 'https://yoomoney.ru/checkout/test'}}
        result.update(overrides)
        return result

    async def order(self):
        order = await commerce.create_external_order(1, '100')
        await commerce.save_external_request(order['order_id'], {'amount': {'value': '100.00', 'currency': 'RUB'}})
        return await commerce.get_payment(order['order_id'])

    async def test_create_verifies_before_persisting_remote_binding(self):
        order = await self.order()
        wrong = self.payment(order, recipient={'account_id': 'other-shop'})
        with patch.object(gateway, '_request', AsyncMock(return_value=wrong)):
            with self.assertRaises(commerce.PaymentValidationError):
                await gateway._create_remote(order)
        self.assertIsNone((await commerce.get_payment(order['order_id']))['provider_payment_id'])

    async def test_private_checkout_blocks_nonadmin_before_order_or_network(self):
        settings = replace(self.settings, payments_admin_only=True, terms_url='', privacy_url='')
        with patch.object(gateway, 'get_settings', return_value=settings), \
                patch.object(gateway, '_request', AsyncMock()) as request:
            with self.assertRaises(gateway.ProviderError):
                await gateway.start_checkout(1, '100', 'https://t.me/test_bot')
            request.assert_not_called()
        self.assertEqual(await commerce.list_payments(user_id=1), [])

    async def test_private_mode_also_blocks_retry_of_nonadmin_unbound_order(self):
        order = await self.order()
        with patch.object(gateway, 'get_settings', return_value=replace(self.settings, payments_admin_only=True)), \
                patch.object(gateway, '_request', AsyncMock()) as request:
            with self.assertRaises(gateway.ProviderError):
                await gateway.check_order(order['order_id'], 1)
            request.assert_not_called()

    async def test_private_owner_checkout_uses_real_provider_flag_and_amount(self):
        settings = replace(self.settings, payments_admin_only=True, admin_ids=frozenset({1}),
                           terms_url='', privacy_url='', yookassa_test=False)
        async def response(method, path, **kwargs):
            order = (await commerce.list_payments(user_id=1))[0]
            self.assertEqual(kwargs['payload']['amount'], {'value': '100.00', 'currency': 'RUB'})
            return self.payment(order, test=False)
        with patch.object(gateway, 'get_settings', return_value=settings), \
                patch.object(gateway, '_request', side_effect=response):
            order = await gateway.start_checkout(1, '100', 'https://t.me/test_bot')
        self.assertEqual(order['provider_payment_id'], 'remote-123')
        self.assertEqual(await db.get_tokens(1), 0)

    async def test_provider_identity_amount_test_and_owner_validation(self):
        order = await self.order()
        order['provider_payment_id'] = 'remote-123'
        payment = self.payment(order)
        gateway.validate_payment(order, payment)
        for overrides in [ {'test': False}, {'test': None}, {'id': 'other'},
            {'amount': {'value': '99.99', 'currency': 'RUB'}},
            {'amount': {'value': '100.00', 'currency': 'USD'}},
            {'metadata': {'order_id': order['order_id'], 'user_id': '2'}} ]:
            with self.subTest(overrides=overrides), self.assertRaises(commerce.PaymentValidationError):
                gateway.validate_payment(order, dict(payment, **overrides))

    async def test_pending_return_does_not_credit(self):
        order = await self.order()
        await commerce.attach_external_payment(order['order_id'], 'remote-123')
        with patch.object(gateway, '_request', AsyncMock(return_value=self.payment(order))):
            result = await gateway.check_order(order['order_id'], 1)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(await db.get_tokens(1), 0)

    async def test_duplicate_remote_success_credits_exactly_once(self):
        order = await self.order()
        await commerce.attach_external_payment(order['order_id'], 'remote-123')
        mock = AsyncMock(return_value=self.payment(order, status='succeeded', paid=True))
        with patch.object(gateway, '_request', mock):
            results = await asyncio.gather(*(gateway.check_order(order['order_id'], 1) for _ in range(5)))
        self.assertEqual(sum(bool(r.get('credited')) for r in results), 1)
        self.assertEqual(await db.get_tokens(1), 100)

    async def test_other_user_cannot_query_order(self):
        order = await self.order()
        with patch.object(gateway, '_request', AsyncMock()) as request:
            with self.assertRaises(commerce.PaymentValidationError):
                await gateway.check_order(order['order_id'], 2)
            request.assert_not_called()

    async def test_create_timeout_replays_exact_persisted_request(self):
        async def timeout(*args, **kwargs):
            raise gateway.ProviderError('uncertain')
        with patch.object(gateway, '_request', side_effect=timeout):
            with self.assertRaises(gateway.ProviderError):
                await gateway.start_checkout(1, '100', 'https://t.me/test_bot')
        order = (await commerce.list_pending_external_payments())[0]
        self.assertTrue(order['request_json'])
        response = self.payment(order)
        with patch.object(gateway, '_request', AsyncMock(return_value=response)) as request:
            await gateway._create_remote(order)
        self.assertEqual(request.call_args.kwargs['key'], order['order_id'])
        self.assertEqual(request.call_args.kwargs['payload'], json.loads(order['request_json']))

    async def funded_order(self):
        order = await self.order()
        await commerce.attach_external_payment(order['order_id'], 'remote-123')
        await commerce.confirm_external_payment(order['order_id'], 'remote-123', 10000, 'RUB', 1, paid=True, status='succeeded')
        return order

    async def test_refund_persists_request_before_network_and_settles_once(self):
        order = await self.funded_order()
        async def request(method, path, *, payload=None, key=None):
            pending = (await commerce.list_pending_external_refunds())[0]
            self.assertEqual(json.loads(pending['refund_request_json']), payload)
            self.assertEqual(key, pending['attempt_id'])
            return {'id': 'refund-1', 'payment_id': 'remote-123', 'status': 'succeeded', 'amount': {'value':'100.00','currency':'RUB'}}
        with patch.object(gateway, '_request', side_effect=request):
            result = await gateway.refund_order(order['order_id'], 99, 'Возврат по обращению')
        self.assertEqual(result['status'], 'refunded')
        self.assertEqual(await db.get_tokens(1), 0)
        self.assertEqual((await commerce.get_payment(order['order_id']))['status'], 'refunded')

    async def test_uncertain_refund_keeps_balance_pending_and_recovers(self):
        order = await self.funded_order()
        with patch.object(gateway, '_request', AsyncMock(side_effect=gateway.ProviderError('timeout'))):
            result = await gateway.refund_order(order['order_id'], 99, 'Возврат по обращению')
        self.assertEqual(result['status'], 'refund_pending')
        self.assertEqual(await db.get_tokens(1), 100)
        pending = (await commerce.list_pending_external_refunds())[0]
        with patch.object(gateway, '_request', AsyncMock(return_value={
            'id': 'refund-1', 'payment_id': 'remote-123', 'status':'succeeded', 'amount':{'value':'100.00','currency':'RUB'}})):
            await gateway._process_refund(pending)
        self.assertEqual(await db.get_tokens(1), 0)

    async def test_bad_refund_is_not_bound_or_debited(self):
        order = await self.funded_order()
        response = {'id': 'refund-wrong', 'payment_id': 'other-payment', 'status':'succeeded', 'amount':{'value':'100.00','currency':'RUB'}}
        with patch.object(gateway, '_request', AsyncMock(return_value=response)):
            result = await gateway.refund_order(order['order_id'], 99, 'Возврат по обращению')
        self.assertEqual(result['status'], 'refund_pending')
        self.assertIsNone((await commerce.list_pending_external_refunds())[0]['provider_refund_id'])
        self.assertEqual(await db.get_tokens(1), 100)

    async def test_refund_rejects_non_admin(self):
        with self.assertRaises(commerce.CommerceError):
            await gateway.refund_order('irrelevant', 1, 'test')

    def test_money_precision(self):
        self.assertEqual(gateway.to_kopecks('499.00'), 49900)
        for value in ('NaN', 'Infinity', '-1', '0.001', 'x'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gateway.to_kopecks(value)
