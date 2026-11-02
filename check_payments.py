"""Read-only payment setup check. Never creates orders or displays credentials."""
import asyncio
import json
import ssl

import aiohttp

from config import get_settings


async def check() -> int:
    settings = get_settings()
    report = {
        'payments_enabled': settings.payments_enabled,
        'payments_admin_only': settings.payments_admin_only,
        'payment_documents_in_bot': settings.payment_documents_in_bot,
        'configured_test_mode': settings.yookassa_test,
        'missing_settings': settings.checkout_problems(),
        'public_launch_missing_settings': settings.payment_problems(require_documents=not settings.payment_documents_in_bot),
        'external_documents_missing': [name for name in settings.payment_problems() if name in ('TERMS_URL', 'PRIVACY_URL')],
        'creates_payments': False,
    }
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        report['error'] = 'API credentials are missing in .env'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(
                'https://api.yookassa.ru/v3/me',
                auth=aiohttp.BasicAuth(settings.yookassa_shop_id, settings.yookassa_secret_key),
                allow_redirects=False,
            ) as response:
                report['http_status'] = response.status
                if response.status != 200:
                    report['error'] = 'Merchant settings could not be verified'
                else:
                    merchant = await response.json()
                    report.update(
                        account_matches=str(merchant.get('account_id')) == settings.yookassa_shop_id,
                        test_mode_matches=merchant.get('test') is settings.yookassa_test,
                        merchant_status=merchant.get('status'),
                        payment_methods=merchant.get('payment_methods'),
                        fiscalization_enabled=merchant.get('fiscalization_enabled'),
                    )
    except (aiohttp.ClientConnectorCertificateError, ssl.SSLError):
        report['error'] = 'TLS certificate verification failed; check the server CA certificates. Do not disable TLS verification.'
    except (aiohttp.ClientError, TimeoutError, ValueError):
        report['error'] = 'The provider could not be reached or returned an invalid response'
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ready = (not report.get('error') and not report['missing_settings']
             and report.get('account_matches') and report.get('test_mode_matches')
             and report.get('merchant_status') == 'enabled')
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(check()))
