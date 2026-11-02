import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import main


async def build():
    user = dict(telegram_id=200, tokens=0, birth_date='2000-01-01', birth_time='12:00',
                birth_place='Москва', subscription_until=None, notify_time='09:00')
    target = SimpleNamespace(from_user=SimpleNamespace(id=200))
    cards = []
    with patch.dict(os.environ, {'BRAND_NAME': 'ТарологИИя', 'PAYMENTS_ENABLED': 'true',
            'PAYMENTS_ADMIN_ONLY': 'false', 'PAYMENT_DOCUMENTS_IN_BOT': 'true',
            'YOOKASSA_SHOP_ID': 'preview', 'YOOKASSA_SECRET_KEY': 'preview',
            'SELLER_NAME': 'Test', 'SELLER_INN': '123456789012', 'SUPPORT_CONTACT': '@support'}), \
            patch.object(main, 'get_user', AsyncMock(return_value=user)), \
            patch.object(main, 'render', AsyncMock()) as render:
        for title, function in [('Главное меню', main.send_main_menu),
                                ('Кошелёк', main.send_tokens_menu),
                                ('Подписка', main.send_subscription_menu)]:
            await function(target)
            args = render.call_args.args
            cards.append(dict(title=title, text=args[1], keyboard=args[2].model_dump(exclude_none=True)))
        reply = main.main_reply_keyboard().model_dump(exclude_none=True)
    (root / 'artifacts/interface-preview.json').write_text(json.dumps(dict(cards=cards, reply=reply), ensure_ascii=False), encoding='utf-8')


asyncio.run(build())
