import unittest
from pathlib import Path

from app import main


def _site_file(relative: str) -> Path | None:
    """Файл сайта ищется вверх от теста: в репозитории это ../../site, а в
    образе процессора сайта нет, пока его не смонтировать в /site."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "site" / relative
        if candidate.is_file():
            return candidate
    return None


class ConsentVersionTests(unittest.TestCase):
    """Версию текста согласия шлёт редактор, а сверяет процессор. Разойдутся —
    каждый запрос на создание файла получит 422, поэтому совпадение проверяется
    здесь, а не на живых пользователях."""

    def test_editor_sends_the_version_the_processor_expects(self) -> None:
        editor = _site_file("js/editor.js")
        if editor is None:
            self.skipTest("рядом нет site/: запустите тесты из репозитория или смонтируйте его в /site")

        self.assertIn(
            f'const consentVersion = "{main.personal_data_consent_version}";',
            editor.read_text(encoding="utf-8"),
        )

    def test_consent_page_exists(self) -> None:
        if _site_file("js/editor.js") is None:
            self.skipTest("рядом нет site/")
        self.assertIsNotNone(_site_file("consent.html"))


if __name__ == "__main__":
    unittest.main()
