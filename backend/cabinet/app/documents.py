"""Базовый набор документов ИП и ООО — то, что будет храниться в кабинете.

Почему в наборе именно эти документы и откуда взяты сведения, описано в
docs/DOCUMENTS.md; меняете список здесь — поправьте и его.

Идентификаторы документов стабильны: на них будут ссылаться загруженные
файлы, поэтому существующий id нельзя переименовывать или переиспользовать
для другого документа — только добавлять новые.
"""

from dataclasses import dataclass
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

DOCUMENTS: tuple[DocumentKind, ...] = (
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
