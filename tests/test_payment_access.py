"""Private owner checkout must never open public payments or hide missing setup."""
import unittest
from dataclasses import replace

from config import get_settings


class PaymentAccessTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(
            get_settings(), bot_token='123:fake', admin_ids=frozenset({99}),
            payments_enabled=True, payments_admin_only=True,
            payment_provider='yookassa', yookassa_shop_id='123',
            yookassa_secret_key='fake', support_contact='@support',
            seller_name='Test', seller_inn='123456789012',
            terms_url='', privacy_url='', ai_enabled=False)

    def test_explicit_in_bot_documents_allow_public_checkout(self):
        settings = replace(self.settings, payments_admin_only=False, payment_documents_in_bot=True)
        settings.validate_startup()
        self.assertTrue(settings.payment_allowed(100))
        self.assertTrue(settings.payment_allowed(99))
        self.assertEqual(settings.payment_problems(), ['TERMS_URL', 'PRIVACY_URL'])

    def test_private_check_allows_owner_but_not_other_users(self):
        self.settings.validate_startup()
        self.assertTrue(self.settings.payment_allowed(99))
        self.assertFalse(self.settings.payment_allowed(100))
        self.assertEqual(self.settings.payment_problems(), ['TERMS_URL', 'PRIVACY_URL'])

    def test_public_switch_without_documents_fails_closed(self):
        settings = replace(self.settings, payments_admin_only=False)
        with self.assertRaises(ValueError):
            settings.validate_startup()
        self.assertFalse(settings.payment_allowed(99))
        self.assertFalse(settings.payment_allowed(100))

    def test_valid_documents_do_not_remove_private_gate(self):
        settings = replace(self.settings, terms_url='https://example.org/terms',
                           privacy_url='https://example.org/privacy')
        self.assertTrue(settings.payment_allowed(99))
        self.assertFalse(settings.payment_allowed(100))

    def test_disabled_switch_blocks_everyone(self):
        settings = replace(self.settings, payments_enabled=False)
        self.assertFalse(settings.payment_allowed(99))

    def test_private_mode_still_requires_credentials_and_admin(self):
        for changes in ({'yookassa_secret_key': ''}, {'admin_ids': frozenset()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.settings, **changes).validate_startup()
