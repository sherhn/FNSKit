import re
import unittest
from pathlib import Path

from app.documents import DOCUMENTS, GROUPS, BusinessType, Level, documents_for

# Тест запускается и из репозитория, и в образе с примонтированным /docs.
DOCS_CANDIDATES = [Path("/docs/DOCUMENTS.md")] + [
    parent / "docs" / "DOCUMENTS.md" for parent in Path(__file__).resolve().parents
]


class CatalogTest(unittest.TestCase):
    def test_ids_are_unique_and_stable_looking(self) -> None:
        ids = [kind.id for kind in DOCUMENTS]
        self.assertEqual(len(ids), len(set(ids)))
        for document_id in ids:
            self.assertRegex(document_id, r"^[a-z][a-z0-9_]{2,40}$")

    def test_every_document_has_known_group_and_texts(self) -> None:
        group_ids = {group.id for group in GROUPS}
        for kind in DOCUMENTS:
            with self.subTest(document=kind.id):
                self.assertIn(kind.group, group_ids)
                self.assertTrue(kind.business_types)
                for text in (kind.title, kind.purpose, kind.source):
                    self.assertTrue(text.strip())
                    self.assertEqual(text, text.strip())

    def test_titles_are_unique_within_business_type(self) -> None:
        for business_type in BusinessType:
            titles = [kind.title for _, kinds in documents_for(business_type) for kind in kinds]
            self.assertEqual(len(titles), len(set(titles)), business_type)

    def test_sets_cover_the_essentials(self) -> None:
        ip = {kind.id for _, kinds in documents_for(BusinessType.IP) for kind in kinds}
        ooo = {kind.id for _, kinds in documents_for(BusinessType.OOO) for kind in kinds}

        self.assertLessEqual({"egrip_record", "egrip_extract", "ip_tax_regime"}, ip)
        self.assertLessEqual(
            {"egrul_record", "egrul_extract", "charter", "participants_list", "director_appointment"},
            ooo,
        )
        # Документы общества не попадают предпринимателю и наоборот.
        self.assertNotIn("charter", ip)
        self.assertNotIn("egrip_extract", ooo)

    def test_basic_documents_come_first_in_each_group(self) -> None:
        for business_type in BusinessType:
            for group, kinds in documents_for(business_type):
                with self.subTest(business_type=business_type, group=group.id):
                    levels = [kind.level for kind in kinds]
                    self.assertEqual(levels, sorted(levels, key=lambda level: level != Level.BASIC))

    def test_groups_keep_declared_order_and_skip_empty(self) -> None:
        order = [group.id for group in GROUPS]
        for business_type in BusinessType:
            shown = [group.id for group, _ in documents_for(business_type)]
            self.assertEqual(shown, [group_id for group_id in order if group_id in shown])
        ip_groups = [group.id for group, _ in documents_for(BusinessType.IP)]
        self.assertNotIn("corporate", ip_groups)

    def test_private_key_is_never_requested(self) -> None:
        signature = next(kind for kind in DOCUMENTS if kind.id == "signature_certificate")
        self.assertIn("Закрытый ключ не загружайте", signature.source)


class DocsSyncTest(unittest.TestCase):
    def test_every_id_is_described_in_docs(self) -> None:
        docs = next((path for path in DOCS_CANDIDATES if path.exists()), None)
        if docs is None:
            self.skipTest("docs/DOCUMENTS.md недоступен")
        # Идентификаторы документов находятся в первом столбце таблиц.
        # Упоминание базы (`cabinet`) в обычном тексте — не документ.
        described = set(re.findall(r"^\|[^|\n]*\(`([a-z0-9_]+)`\)", docs.read_text(encoding="utf-8"), re.MULTILINE))
        catalog = {kind.id for kind in DOCUMENTS}
        self.assertEqual(catalog - described, set(), "нет в docs/DOCUMENTS.md")
        self.assertEqual(described - catalog, set(), "есть в docs, но нет в каталоге")


if __name__ == "__main__":
    unittest.main()
