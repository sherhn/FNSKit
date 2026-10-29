"""Базовый набор документов ИП и ООО — то, что будет храниться в кабинете.

Почему в наборе именно эти документы и откуда взяты сведения, описано в
docs/DOCUMENTS.md; меняете список здесь — поправьте и его.

Идентификаторы документов стабильны: на них будут ссылаться загруженные
файлы, поэтому существующий id нельзя переименовывать или переиспользовать
для другого документа — только добавлять новые.
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum


class BusinessType(StrEnum):
    IP = "ip"
    OOO = "ooo"


BUSINESS_TYPE_LABELS: dict[BusinessType, str] = {
    BusinessType.IP: "Индивидуальный предприниматель",
    BusinessType.OOO: "ООО",
}


class Level(StrEnum):
    # Есть почти у каждого ИП или ООО — кабинет показывает это как чек-лист.
    BASIC = "basic"
    # Нужен не всем: доверенности, лицензии, копия паспорта.
    SITUATIONAL = "situational"


class DateKind(StrEnum):
    # Дата окончания: подпись, доверенность, лицензия, аренда.
    EXPIRES = "expires"
    # Дата выдачи: выписка считается свежей ограниченное число дней.
    ISSUED = "issued"


@dataclass(frozen=True)
class DocumentGroup:
    id: str
    title: str


@dataclass(frozen=True)
class DocumentKind:
    id: str
    group: str
    title: str
    # Что это и зачем нужно — одной-двумя фразами.
    purpose: str
    # Где взять или как получить.
    source: str
    level: Level
    business_types: frozenset[BusinessType]
    # Предупреждение: срок действия, персональные данные.
    note: str | None = None
    # Срок, по которому кабинет напоминает. У большинства документов его нет.
    date_kind: DateKind | None = None
    date_label: str | None = None
    # Для ISSUED: сколько дней документ считается свежим.
    fresh_days: int | None = None


IP = frozenset({BusinessType.IP})
OOO = frozenset({BusinessType.OOO})
BOTH = frozenset({BusinessType.IP, BusinessType.OOO})

# Порядок групп — порядок показа в кабинете.
GROUPS: tuple[DocumentGroup, ...] = (
    DocumentGroup("registration", "Регистрация и учёт"),
    DocumentGroup("corporate", "Устав и управление"),
    DocumentGroup("tax", "Налоги и учёт"),
    DocumentGroup("authority", "Подпись и полномочия"),
    DocumentGroup("operations", "Адрес, банк и лицензии"),
    DocumentGroup("identity", "Личные документы"),
)

_BASE_DOCUMENTS: tuple[DocumentKind, ...] = (
    # --- Регистрация и учёт ---
    DocumentKind(
        id="egrip_record",
        group="registration",
        title="Лист записи ЕГРИП о регистрации",
        purpose=(
            "Подтверждает регистрацию ИП и ОГРНИП. С 2017 года выдаётся вместо "
            "свидетельства; у зарегистрированных раньше — свидетельство о госрегистрации."
        ),
        source="Приходит от ФНС на почту после регистрации. Если потерялся — запросите в инспекции.",
        level=Level.BASIC,
        business_types=IP,
    ),
    DocumentKind(
        id="egrul_record",
        group="registration",
        title="Лист записи ЕГРЮЛ о создании",
        purpose=(
            "Подтверждает регистрацию общества и ОГРН. С 2017 года выдаётся вместо "
            "свидетельства; у обществ, созданных раньше, — свидетельство. Листы записи "
            "об изменениях храните здесь же."
        ),
        source="Приходит от ФНС на почту после регистрации.",
        level=Level.BASIC,
        business_types=OOO,
    ),
    DocumentKind(
        id="egrip_extract",
        group="registration",
        title="Выписка из ЕГРИП",
        purpose=(
            "Актуальные сведения о предпринимателе: ОГРНИП, ИНН, виды деятельности. "
            "Её просят банк и контрагенты."
        ),
        source=(
            "Бесплатно на egrul.nalog.ru: PDF с усиленной подписью ФНС равнозначен "
            "бумажной выписке."
        ),
        level=Level.BASIC,
        business_types=IP,
        note="Сведения меняются — формируйте свежую выписку перед отправкой.",
    ),
    DocumentKind(
        id="egrul_extract",
        group="registration",
        title="Выписка из ЕГРЮЛ",
        purpose=(
            "Актуальные сведения об обществе: ОГРН, ИНН и КПП, директор, участники, "
            "виды деятельности."
        ),
        source=(
            "Бесплатно на egrul.nalog.ru: PDF с усиленной подписью ФНС равнозначен "
            "бумажной выписке."
        ),
        level=Level.BASIC,
        business_types=OOO,
        note="Сведения меняются — формируйте свежую выписку перед отправкой.",
    ),
    DocumentKind(
        id="rosstat_codes",
        group="registration",
        title="Уведомление о кодах статистики",
        purpose="Коды ОКПО и другие — их указывают в договорах, отчётности и банковских анкетах.",
        source="Бесплатно на websbor.rosstat.gov.ru по ИНН или ОГРН.",
        level=Level.BASIC,
        business_types=BOTH,
    ),
    DocumentKind(
        id="ip_inn_certificate",
        group="registration",
        title="Свидетельство ИНН",
        purpose=(
            "ИНН предпринимателя — это ИНН физлица. Номер есть в выписке, но "
            "свидетельство иногда просят отдельно."
        ),
        source="Бумажное у большинства есть; электронное — в личном кабинете налогоплательщика.",
        level=Level.SITUATIONAL,
        business_types=IP,
    ),
    DocumentKind(
        id="ooo_tax_registration",
        group="registration",
        title="Свидетельство о постановке на налоговый учёт",
        purpose="ИНН и КПП общества. Номера есть и в выписке, но документ иногда просят отдельно.",
        source="Выдаётся при постановке на учёт; повторно — по запросу в инспекции.",
        level=Level.SITUATIONAL,
        business_types=OOO,
    ),
    DocumentKind(
        id="msp_register",
        group="registration",
        title="Сведения из реестра МСП",
        purpose=(
            "Подтверждают категорию — микро- или малое предприятие: это нужно для "
            "льгот, господдержки и закупок."
        ),
        source="Бесплатно на rmsp.nalog.ru. В реестр ФНС вносит сама, заявление не нужно.",
        level=Level.SITUATIONAL,
        business_types=BOTH,
    ),
    # --- Устав и управление (ООО) ---
    DocumentKind(
        id="charter",
        group="corporate",
        title="Устав",
        purpose=(
            "Действующая редакция с отметкой ФНС. Если общество работает по типовому "
            "уставу, своего устава нет — номер типового устава указан в выписке ЕГРЮЛ."
        ),
        source="Выдаётся ФНС при регистрации и при изменениях.",
        level=Level.BASIC,
        business_types=OOO,
    ),
    DocumentKind(
        id="founding_decision",
        group="corporate",
        title="Решение о создании ООО",
        purpose="Решение единственного учредителя или протокол собрания учредителей.",
        source="Готовится при создании общества.",
        level=Level.BASIC,
        business_types=OOO,
    ),
    DocumentKind(
        id="participants_list",
        group="corporate",
        title="Список участников ООО",
        purpose=(
            "Общество обязано вести его по ст. 31.1 закона об ООО: сведения о каждом "
            "участнике и его доле."
        ),
        source="Ведёт директор; обновляйте при каждом изменении долей.",
        level=Level.BASIC,
        business_types=OOO,
    ),
    DocumentKind(
        id="director_appointment",
        group="corporate",
        title="Решение о назначении директора и приказ",
        purpose="Подтверждают полномочия руководителя — их почти всегда просит банк.",
        source="Решение участника или протокол собрания и приказ о вступлении в должность.",
        level=Level.BASIC,
        business_types=OOO,
    ),
    # --- Налоги и учёт ---
    DocumentKind(
        id="ip_tax_regime",
        group="tax",
        title="Документ о налоговом режиме",
        purpose=(
            "Уведомление о переходе на УСН (форма 26.2-1) с отметкой инспекции, патент "
            "или справка о постановке на учёт плательщика НПД. На общей системе "
            "отдельного документа нет."
        ),
        source=(
            "Хранится с момента перехода. Если отметки нет — инспекция выдаст "
            "информационное письмо о применении УСН по запросу."
        ),
        level=Level.BASIC,
        business_types=IP,
    ),
    DocumentKind(
        id="ooo_tax_regime",
        group="tax",
        title="Документ о налоговом режиме",
        purpose=(
            "Уведомление о переходе на УСН (форма 26.2-1) с отметкой инспекции. На общей "
            "системе отдельного документа нет."
        ),
        source=(
            "Хранится с момента перехода. Если отметки нет — инспекция выдаст "
            "информационное письмо о применении УСН по запросу."
        ),
        level=Level.BASIC,
        business_types=OOO,
    ),
    DocumentKind(
        id="accounting_policy",
        group="tax",
        title="Учётная политика",
        purpose="Обязательна для организаций (ст. 8 закона о бухучёте): как общество ведёт учёт.",
        source="Утверждается приказом руководителя.",
        level=Level.BASIC,
        business_types=OOO,
    ),
    # --- Подпись и полномочия ---
    DocumentKind(
        id="signature_certificate",
        group="authority",
        title="Сертификат электронной подписи",
        purpose=(
            "Открытая часть сертификата (.cer): по ней видно владельца и срок действия, "
            "чтобы вовремя продлить подпись."
        ),
        source=(
            "Выгружается из КриптоПро или с носителя. Закрытый ключ не загружайте "
            "никуда — ни к нам, ни в другие сервисы."
        ),
        level=Level.BASIC,
        business_types=BOTH,
        note="Срок действия сертификата ограничен — следите за датой окончания.",
    ),
    DocumentKind(
        id="power_of_attorney",
        group="authority",
        title="Доверенности, в том числе МЧД",
        purpose=(
            "Нужны, если за вас действует бухгалтер или сотрудник. Чтобы подписывать "
            "электронные документы от имени бизнеса, сотруднику нужна машиночитаемая "
            "доверенность (МЧД)."
        ),
        source=(
            "МЧД создаётся и проверяется в сервисе ФНС «Создание и проверка "
            "доверенности в электронной форме»."
        ),
        level=Level.SITUATIONAL,
        business_types=BOTH,
        note="У доверенности есть срок действия — следите за ним.",
    ),
    # --- Адрес, банк и лицензии ---
    DocumentKind(
        id="bank_details",
        group="operations",
        title="Карточка с реквизитами",
        purpose=(
            "Реквизиты расчётного счёта, ИНН, ОГРН или ОГРНИП и адрес на одном листе — "
            "для договоров и счетов."
        ),
        source="Составляется самостоятельно; реквизиты счёта даёт банк.",
        level=Level.BASIC,
        business_types=BOTH,
    ),
    DocumentKind(
        id="legal_address",
        group="operations",
        title="Документы на юридический адрес",
        purpose=(
            "Договор аренды, гарантийное письмо собственника или документ о "
            "собственности — подтверждают адрес из ЕГРЮЛ."
        ),
        source="Оформляются с собственником помещения.",
        level=Level.BASIC,
        business_types=OOO,
    ),
    DocumentKind(
        id="premises",
        group="operations",
        title="Документы на помещение",
        purpose="Нужны, если вы работаете не по месту прописки: офис, склад, точка продаж.",
        source="Договор аренды или документ о собственности.",
        level=Level.SITUATIONAL,
        business_types=IP,
    ),
    DocumentKind(
        id="licenses",
        group="operations",
        title="Лицензии и разрешения",
        purpose="Нужны, если деятельность лицензируется.",
        source="Выдаёт лицензирующий орган; сведения о лицензии есть и в его реестре.",
        level=Level.SITUATIONAL,
        business_types=BOTH,
    ),
    # --- Личные документы ---
    DocumentKind(
        id="passport_ip",
        group="identity",
        title="Паспорт предпринимателя",
        purpose="Банк и контрагенты просят его, чтобы проверить личность.",
        source="Копия разворота с фото и страницы с регистрацией.",
        level=Level.SITUATIONAL,
        business_types=IP,
        note="Это персональные данные: держите копию в кабинете, только если она нужна под рукой.",
    ),
    DocumentKind(
        id="passport_director",
        group="identity",
        title="Паспорт руководителя",
        purpose="Банк и контрагенты просят его, чтобы проверить личность директора.",
        source="Копия разворота с фото и страницы с регистрацией.",
        level=Level.SITUATIONAL,
        business_types=OOO,
        note="Это персональные данные: держите копию в кабинете, только если она нужна под рукой.",
    ),
)


# Какие документы имеют срок. Выписку банки обычно просят не старше 30 дней —
# это значение по умолчанию, а не требование закона: у конкретного банка оно
# может быть другим.
_DATE_RULES: dict[str, dict] = {
    "egrip_extract": {"date_kind": DateKind.ISSUED, "date_label": "Дата формирования выписки", "fresh_days": 30},
    "egrul_extract": {"date_kind": DateKind.ISSUED, "date_label": "Дата формирования выписки", "fresh_days": 30},
    "signature_certificate": {"date_kind": DateKind.EXPIRES, "date_label": "Сертификат действует до"},
    "power_of_attorney": {"date_kind": DateKind.EXPIRES, "date_label": "Доверенность действует до"},
    "licenses": {"date_kind": DateKind.EXPIRES, "date_label": "Лицензия действует до"},
    "ip_tax_regime": {"date_kind": DateKind.EXPIRES, "date_label": "Патент действует до"},
    "legal_address": {"date_kind": DateKind.EXPIRES, "date_label": "Договор действует до"},
    "premises": {"date_kind": DateKind.EXPIRES, "date_label": "Договор действует до"},
}

DOCUMENTS: tuple[DocumentKind, ...] = tuple(
    replace(kind, **_DATE_RULES[kind.id]) if kind.id in _DATE_RULES else kind
    for kind in _BASE_DOCUMENTS
)
KINDS_BY_ID: dict[str, DocumentKind] = {kind.id: kind for kind in DOCUMENTS}


@dataclass(frozen=True)
class PackagePreset:
    """Типичный набор документов под запрос: из него кабинет предлагает,
    какие файлы положить в архив. Это подсказка, а не требование — у банка или
    контрагента список может быть другим, файлы пользователь выбирает сам."""

    id: str
    title: str
    purpose: str
    # Документы обеих форм бизнеса; для ИП и ООО остаются только свои.
    kinds: tuple[str, ...]


# Порядок документов в наборе — порядок файлов в архиве и в описи.
PACKAGE_PRESETS: tuple[PackagePreset, ...] = (
    PackagePreset(
        id="bank_account",
        title="Открытие счёта в банке",
        purpose=(
            "Регистрация, полномочия руководителя, адрес и паспорт — с этим банки "
            "открывают расчётный счёт. Точный список уточните в своём банке."
        ),
        kinds=(
            "egrip_record", "egrip_extract", "ip_tax_regime", "passport_ip",
            "egrul_record", "egrul_extract", "charter", "founding_decision", "participants_list",
            "director_appointment", "legal_address", "passport_director",
        ),
    ),
    PackagePreset(
        id="counterparty",
        title="Проверка контрагентом",
        purpose=(
            "Регистрация, полномочия подписанта и реквизиты — так контрагент "
            "проверяет вас перед договором."
        ),
        kinds=(
            "egrip_record", "egrip_extract", "ip_inn_certificate",
            "egrul_record", "egrul_extract", "charter", "director_appointment", "ooo_tax_registration",
            "power_of_attorney", "bank_details",
        ),
    ),
    PackagePreset(
        id="authority",
        title="Подтверждение полномочий",
        purpose="Кто подписывает документы и на каком основании — для банка, ФНС или оператора ЭДО.",
        kinds=(
            "egrip_extract", "egrul_extract", "director_appointment", "power_of_attorney",
            "signature_certificate", "passport_ip", "passport_director",
        ),
    ),
    PackagePreset(
        id="support",
        title="Господдержка и закупки",
        purpose="Категория МСП, коды статистики и реквизиты — для льгот, господдержки и закупок.",
        kinds=(
            "egrip_extract", "egrul_extract", "msp_register", "rosstat_codes", "charter",
            "director_appointment", "licenses", "bank_details",
        ),
    ),
)


def presets_for(business_type: BusinessType) -> list[PackagePreset]:
    """Наборы для формы бизнеса: в каждом — только документы её каталога."""
    return [
        replace(preset, kinds=tuple(k for k in preset.kinds if business_type in KINDS_BY_ID[k].business_types))
        for preset in PACKAGE_PRESETS
    ]


def due_date(kind: DocumentKind, doc_date: date | None) -> date | None:
    """Когда документ перестаёт быть действующим или свежим."""
    if doc_date is None or kind.date_kind is None:
        return None
    if kind.date_kind is DateKind.ISSUED:
        return doc_date + timedelta(days=kind.fresh_days or 30)
    return doc_date


def documents_for(business_type: BusinessType) -> list[tuple[DocumentGroup, list[DocumentKind]]]:
    """Набор для формы бизнеса по группам; в группе сначала базовые документы."""
    level_order = {Level.BASIC: 0, Level.SITUATIONAL: 1}
    result: list[tuple[DocumentGroup, list[DocumentKind]]] = []
    for group in GROUPS:
        kinds = [
            kind
            for kind in DOCUMENTS
            if kind.group == group.id and business_type in kind.business_types
        ]
        kinds.sort(key=lambda kind: level_order[kind.level])
        if kinds:
            result.append((group, kinds))
    return result
