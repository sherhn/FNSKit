import asyncio
import unittest
from datetime import date, timedelta
from unittest import mock

from app import reminders
from app.documents import KINDS_BY_ID, due_date
from app.files import document_aad
from app.store import NewDocument
from support import MemoryStore, make_cipher, make_settings

TODAY = date(2026, 10, 20)
OWNER = "owner-1"


def text_of(message) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


def html_of(message) -> str:
    return message.get_body(preferencelist=("html",)).get_content()


class ThresholdTest(unittest.TestCase):
    def test_threshold_for(self) -> None:
        cases = {45: None, 31: None, 30: 30, 15: 30, 8: 30, 7: 7, 2: 7, 1: 1, 0: 0, -5: 0}
        for days_left, expected in cases.items():
            with self.subTest(days_left=days_left):
                self.assertEqual(reminders.threshold_for(days_left), expected)

    def test_should_send_only_when_threshold_is_new(self) -> None:
        self.assertTrue(reminders.should_send(30, None))
        self.assertFalse(reminders.should_send(25, 30))  # 30-дневное уже отправлено
        self.assertTrue(reminders.should_send(7, 30))
        self.assertFalse(reminders.should_send(5, 7))
        self.assertTrue(reminders.should_send(0, 1))
        self.assertFalse(reminders.should_send(-3, 0))  # просрочку напоминаем один раз
        self.assertFalse(reminders.should_send(60, None))

    def test_plural_and_date(self) -> None:
        self.assertEqual([reminders.plural_days(n) for n in (1, 2, 5, 11, 21, 30)],
                         ["1 день", "2 дня", "5 дней", "11 дней", "21 день", "30 дней"])
        self.assertEqual(reminders.long_date(date(2026, 1, 5)), "5 января 2026")


class DueDateTest(unittest.TestCase):
    def test_issued_documents_expire_after_fresh_days(self) -> None:
        extract = KINDS_BY_ID["egrip_extract"]
        self.assertEqual(due_date(extract, date(2026, 10, 1)), date(2026, 10, 31))

    def test_expiring_documents_use_the_date_itself(self) -> None:
        cert = KINDS_BY_ID["signature_certificate"]
        self.assertEqual(due_date(cert, date(2027, 3, 1)), date(2027, 3, 1))

    def test_documents_without_dates(self) -> None:
        self.assertIsNone(due_date(KINDS_BY_ID["charter"], date(2027, 3, 1)))
        self.assertIsNone(due_date(KINDS_BY_ID["signature_certificate"], None))


class RunOnceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = MemoryStore()
        self.settings = make_settings()
        self.sent: list = []
        self.cipher = make_cipher()

    def send(self, settings, message) -> None:
        self.sent.append(message)

    async def add(self, kind: str, doc_date: date | None, last=None, owner=OWNER) -> str:
        document_id = f"doc-{len(self.store.documents)}"
        await self.store.add_document(
            NewDocument(document_id, owner, kind, "сертификат.cer", "application/pkix-cert", 10, "0" * 64,
                        self.cipher.seal(b"x", document_aad(document_id, owner))),
            100, 10**9,
        )
        await self.store.set_doc_date(owner, document_id, doc_date, last)
        return document_id

    async def run_once(self, today=TODAY) -> int:
        return await reminders.run_once(self.store, self.settings, self.send, today)

    async def test_sends_once_per_threshold(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        document_id = await self.add("signature_certificate", TODAY + timedelta(days=30))

        self.assertEqual(await self.run_once(), 1)
        self.assertEqual(await self.run_once(), 0)
        self.assertEqual(await self.run_once(TODAY + timedelta(days=10)), 0)
        self.assertEqual(await self.run_once(TODAY + timedelta(days=23)), 1)  # осталось 7
        self.assertEqual(await self.run_once(TODAY + timedelta(days=29)), 1)  # остался 1
        self.assertEqual(await self.run_once(TODAY + timedelta(days=30)), 1)  # день срока
        self.assertEqual(await self.run_once(TODAY + timedelta(days=40)), 0)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.store.documents[document_id].last_reminder, 0)

    async def test_message_content(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        await self.add("signature_certificate", TODAY + timedelta(days=7))
        await self.run_once()
        message = self.sent[0]
        self.assertEqual(message["To"], "owner@example.test")
        self.assertIn("Сертификат электронной подписи", message["Subject"])
        self.assertIn("7 дней", message["Subject"])
        body = text_of(message)
        self.assertIn("27 октября 2026", body)
        self.assertIn("/account", body)
        self.assertIn("сертификат.cer", body)

    async def test_html_version_matches_site_style(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        await self.add("signature_certificate", TODAY + timedelta(days=7))
        await self.run_once()
        message = self.sent[0]
        self.assertTrue(message.is_multipart())
        html = html_of(message)
        self.assertIn("Срок документа заканчивается", html)
        self.assertIn("Сертификат электронной подписи", html)
        self.assertIn("сертификат.cer", html)
        self.assertIn("осталось 7 дней", html)
        self.assertIn("http://localhost:8082/account", html)
        self.assertIn("/images/apple-touch-icon.png", html)
        # Цвета и «таблетки» сайта: лаймовая полоса, чернильная кнопка, бумажный фон.
        for token in ("#C8FF32", "#171815", "#F5F4EF", "border-radius:999px"):
            self.assertIn(token, html)
        self.assertIn("#FFDFA8", html)  # срочно: до 7 дней

    async def test_html_tone_follows_urgency(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        await self.add("signature_certificate", TODAY + timedelta(days=30))
        await self.add("power_of_attorney", TODAY - timedelta(days=1))
        await self.run_once()
        soon = next(m for m in self.sent if "Сертификат" in m["Subject"])
        expired = next(m for m in self.sent if "Доверенности" in m["Subject"])
        self.assertIn("#E9F9B8", html_of(soon))
        self.assertIn("#FEE2E2", html_of(expired))
        self.assertIn("Срок документа закончился", html_of(expired))

    async def test_html_escapes_values(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        document_id = await self.add("signature_certificate", TODAY + timedelta(days=3))
        stored = self.store.documents[document_id]
        self.store.documents[document_id] = stored.__class__(
            **{**stored.__dict__, "filename": 'a&b"<script>.cer'}
        )
        await self.run_once()
        html = html_of(self.sent[0])
        self.assertNotIn("<script>", html)
        self.assertIn("a&amp;b&quot;&lt;script&gt;.cer", html)

    async def test_issued_extract_goes_stale(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        await self.add("egrip_extract", TODAY - timedelta(days=25))  # устареет через 5 дней
        self.assertEqual(await self.run_once(), 1)
        self.assertIn("5 дней", self.sent[0]["Subject"])
        self.assertIn("свежей", text_of(self.sent[0]))

    async def test_expired_message(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        await self.add("power_of_attorney", TODAY - timedelta(days=2))
        await self.run_once()
        self.assertIn("больше не действует", self.sent[0]["Subject"])

    async def test_respects_opt_out_and_missing_email(self) -> None:
        await self.add("signature_certificate", TODAY + timedelta(days=3))
        self.assertEqual(await self.run_once(), 0)  # адреса нет
        await self.store.set_email_reminders(OWNER, "owner@example.test", False)
        self.assertEqual(await self.run_once(), 0)  # отключено
        await self.store.set_email_reminders(OWNER, "owner@example.test", True)
        self.assertEqual(await self.run_once(), 1)

    async def test_ignores_documents_without_dates_or_far_deadlines(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        await self.add("signature_certificate", None)
        await self.add("signature_certificate", TODAY + timedelta(days=200))
        self.assertEqual(await self.run_once(), 0)

    async def test_failed_send_is_retried(self) -> None:
        await self.store.touch_contact(OWNER, "owner@example.test")
        document_id = await self.add("signature_certificate", TODAY + timedelta(days=5))

        def broken(settings, message) -> None:
            raise OSError("smtp down")

        with self.assertLogs("fnskit.cabinet.reminders", level="ERROR"):
            self.assertEqual(await reminders.run_once(self.store, self.settings, broken, TODAY), 0)
        self.assertIsNone(self.store.documents[document_id].last_reminder)
        self.assertEqual(await self.run_once(), 1)

    async def test_each_user_gets_own_mail(self) -> None:
        await self.store.touch_contact(OWNER, "a@example.test")
        await self.store.touch_contact("owner-2", "b@example.test")
        await self.add("signature_certificate", TODAY + timedelta(days=5))
        await self.add("signature_certificate", TODAY + timedelta(days=5), owner="owner-2")
        self.assertEqual(await self.run_once(), 2)
        self.assertEqual({m["To"] for m in self.sent}, {"a@example.test", "b@example.test"})

    async def test_bad_address_does_not_stop_others(self) -> None:
        # Адрес с переводом строки в заголовок To не годится — письмо этому
        # владельцу не уходит, но остальным рассылка продолжается.
        await self.store.touch_contact(OWNER, "a@example.test\nBcc: spy@example.test")
        await self.store.touch_contact("owner-2", "b@example.test")
        await self.add("signature_certificate", TODAY + timedelta(days=5))
        await self.add("signature_certificate", TODAY + timedelta(days=5), owner="owner-2")
        with self.assertLogs("fnskit.cabinet.reminders", level="ERROR"):
            self.assertEqual(await self.run_once(), 1)
        self.assertEqual([m["To"] for m in self.sent], ["b@example.test"])


if __name__ == "__main__":
    unittest.main()
