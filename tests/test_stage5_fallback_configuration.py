import unittest
from unittest.mock import patch

from pydantic import ValidationError

from api.core.config import Settings, get_settings


class GenerationConfigurationTests(unittest.TestCase):
    def test_deadlines_must_be_finite_positive_and_bounded(self):
        for name, maximum in (("generation_timeout_seconds", 120), ("generation_total_timeout_seconds", 240)):
            for value in (0, -1, float("inf"), float("nan"), maximum + 1):
                with self.subTest(name=name, value=value), self.assertRaises(ValidationError):
                    Settings(**{name: value})
        settings = Settings()
        self.assertEqual(settings.generation_timeout_seconds, 20)
        self.assertEqual(settings.generation_total_timeout_seconds, 40)
        self.assertTrue(settings.generation_fallback_enabled)

    def test_environment_configures_fallback_without_loading_local_secrets(self):
        get_settings.cache_clear()
        try:
            with patch("api.core.config.load_dotenv"), patch.dict("os.environ", {
                "GENERATION_FALLBACK_ENABLED": "false",
                "GENERATION_TIMEOUT_SECONDS": "2.5",
                "GENERATION_TOTAL_TIMEOUT_SECONDS": "5",
            }, clear=True):
                settings = get_settings()
            self.assertFalse(settings.generation_fallback_enabled)
            self.assertEqual(settings.generation_timeout_seconds, 2.5)
            self.assertEqual(settings.generation_total_timeout_seconds, 5)
        finally:
            get_settings.cache_clear()
