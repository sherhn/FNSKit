"""Напоминания о сроках документов.

Письмо уходит, когда до срока остаётся 30, 7 и 1 день и в день окончания —
по одному на порог. Чтобы не слать повторно, у файла хранится последний
отправленный порог (last_reminder): письмо нужно, только если текущий порог
меньше. Смена даты начинает отсчёт заново, но порог, в котором дата уже
оказалась, считается пройденным — иначе указанная «на следующей неделе» дата
сразу вызвала бы письмо.

В письме нет содержимого документов: только название, имя файла и дата.
"""

import asyncio
import logging
import smtplib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from email.message import EmailMessage

from app import email_layout
from app.config import Settings
from app.documents import KINDS_BY_ID, DateKind, due_date
from app.store import DocumentStore, StoredDocument

logger = logging.getLogger("fnskit.cabinet.reminders")

# От большего к меньшему.
THRESHOLDS = (30, 7, 1, 0)
MOSCOW = timezone(timedelta(hours=3))


def today() -> date:
    """Сегодняшняя дата по Москве: сроки в документах считаются по ней."""
    return datetime.now(UTC).astimezone(MOSCOW).date()


def threshold_for(days_left: int) -> int | None:
    """Наименьший порог, в который уже вошли: 5 дней → 7, 40 дней → нет."""
    reached = [threshold for threshold in THRESHOLDS if days_left <= threshold]
    return min(reached) if reached else None


def should_send(days_left: int, last: int | None) -> bool:
    threshold = threshold_for(days_left)
    return threshold is not None and (last is None or threshold < last)


def plural_days(count: int) -> str:
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        return f"{count} день"
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return f"{count} дня"
    return f"{count} дней"


_MONTHS = ("января февраля марта апреля мая июня июля августа сентября октября ноября декабря").split()


def long_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def build_message(settings: Settings, to: str, document: StoredDocument, days_left: int) -> EmailMessage:
    kind = KINDS_BY_ID[document.kind]
    due = due_date(kind, document.doc_date)
    assert due is not None
    issued = kind.date_kind is DateKind.ISSUED
    title = kind.title

    if days_left <= 0:
        subject = f"ФНСКит: «{title}» {'устарела' if issued else 'больше не действует'}"
        lead = (
            f"Документу «{title}» ({document.filename}) уже больше {kind.fresh_days} дней — "
            "для банка или ФНС нужна свежая версия."
            if issued
            else f"Срок действия документа «{title}» ({document.filename}) закончился {long_date(due)}."
        )
    else:
        left = plural_days(days_left)
        subject = (
            f"ФНСКит: «{title}» — до обновления {left}"
            if issued
            else f"ФНСКит: у документа «{title}» срок заканчивается через {left}"
        )
        lead = (
            f"Выписка «{document.filename}» перестанет считаться свежей {long_date(due)} (через {left}). "
            "Закажите новую заранее, если она понадобится."
            if issued
            else f"Срок действия документа «{title}» ({document.filename}) заканчивается "
            f"{long_date(due)} — через {left}. Продлите или замените его заранее."
        )

    site = settings.public_url.rstrip("/")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"ФНСКит <{settings.smtp_from}>"
    message["To"] = to
    message.set_content(
        f"{lead}\n\n"
        f"Документы и сроки: {site}/account\n\n"
        "Вы получили это письмо, потому что в кабинете ФНСКит указан срок документа. "
        "Отключить напоминания можно на вкладке «Аккаунт» в кабинете.\n"
        "ФНСКит не является официальным сервисом ФНС и не заменяет консультацию бухгалтера или юриста.\n"
    )

    # HTML-версия в стиле сайта; текстовая выше остаётся для клиентов без HTML.
    if days_left <= 0:
        heading = "Выписка устарела" if issued else "Срок документа закончился"
        tone = "expired"
    else:
        heading = "Выписка скоро устареет" if issued else "Срок документа заканчивается"
        tone = "urgent" if days_left <= 7 else "soon"
    status = (
        f"Устарела {long_date(due)}" if issued and days_left <= 0
        else f"Закончился {long_date(due)}" if days_left <= 0
        else f"{'Свежая до' if issued else 'Действует до'} {long_date(due)} — осталось {plural_days(days_left)}"
    )
    inner = (
        email_layout.status_box(title, [document.filename, status], tone)
        + email_layout.paragraph(
            "Закажите новую выписку заранее, если она понадобится банку или ФНС."
            if issued
            else "Продлите или замените документ заранее, чтобы не остановить работу с банком или ФНС."
        )
        + email_layout.button(f"{site}/account", "Открыть кабинет")
        + email_layout.paragraph(
            "Вы получили это письмо, потому что в кабинете указан срок документа. "
            "Напоминания можно отключить на вкладке «Аккаунт».",
            muted=True,
        )
    )
    message.add_alternative(email_layout.render(heading, inner, site), subtype="html")
    return message


def send_smtp(settings: Settings, message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
        if settings.smtp_starttls:
            client.starttls()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        client.send_message(message)


async def run_once(
    store: DocumentStore,
    settings: Settings,
    send: Callable[[Settings, EmailMessage], None] = send_smtp,
    now: date | None = None,
) -> int:
    """Один проход: рассылает то, что подошло по порогам. Возвращает число писем."""
    current = now or today()
    sent = 0
    for document, email in await store.reminder_candidates():
        kind = KINDS_BY_ID.get(document.kind)
        due = due_date(kind, document.doc_date) if kind else None
        if due is None:
            continue
        days_left = (due - current).days
        if not should_send(days_left, document.last_reminder):
            continue
        try:
            # Письмо собираем внутри try: адрес, который не годится в заголовок
            # To, не должен остановить рассылку остальным.
            message = build_message(settings, email, document, days_left)
            await asyncio.to_thread(send, settings, message)
        except Exception:
            # Порог не отмечаем: следующий проход попробует ещё раз.
            logger.exception("Не удалось отправить напоминание по документу %s", document.id)
            continue
        await store.mark_reminded(document.id, threshold_for(days_left))
        sent += 1
    return sent
