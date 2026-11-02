import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import app_services
import db
from horoscope import generate_daily_reading


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, 'DB_NAME', str(Path(self.tmp.name) / 'test.db'))
        self.db_patch.start()
        await app_services.init_app()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_consent_requires_explicit_acceptance(self):
        self.assertFalse(await app_services.has_consent(12))
        await app_services.accept_consent(12)
        self.assertTrue(await app_services.has_consent(12))
        self.assertFalse(await app_services.has_consent(13))

    async def test_daily_profile_logic_preserved_without_ai(self):
        user = {'birth_date': '1994-05-12', 'birth_time': '12:30', 'birth_place':'Москва, Россия'}
        today = date(2026, 9, 9)
        original = generate_daily_reading(user, today)
        with patch.dict('os.environ', {'AI_ENABLED': 'false'}):
            actual = await app_services.get_daily_reading(user, today)
        self.assertEqual({k: actual[k] for k in original}, original)
        self.assertEqual(actual['generation_source'], 'algorithm')
        self.assertEqual(original, generate_daily_reading(user, today))
        self.assertNotEqual(original, generate_daily_reading(dict(user, birth_place='Казань, Россия'), today))
        self.assertNotEqual(original, generate_daily_reading(dict(user, birth_time='13:30'), today))
        self.assertNotEqual(original, generate_daily_reading(user, date(2026, 9, 10)))

    def test_ai_output_must_be_complete_and_match_schema(self):
        values = {field: 'Спокойно выбери один посильный шаг.' for field in app_services.AI_FIELDS}
        response = {'status':'completed', 'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(values)}]}]}
        self.assertEqual(app_services.extract_ai_fields(response), values)
        with self.assertRaises(ValueError):
            app_services.extract_ai_fields(dict(response, status='incomplete'))
        bad = {'status':'completed', 'output':[{'type':'message','content':[{'type':'output_text','text':'{"unexpected": "test"}'}]}]}
        with self.assertRaises(ValueError):
            app_services.extract_ai_fields(bad)
