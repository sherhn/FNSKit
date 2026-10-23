import base64
import unittest

from cryptography.exceptions import InvalidTag

from app.files import (
    CONTENT_TYPES,
    DOCUMENT_FILES,
    STORAGE_FILES,
    TOKEN_PATTERN,
    FileCipher,
    UnsupportedFile,
    clean_filename,
    content_disposition,
    detect_content_type,
    document_aad,
    new_link_token,
    token_hash,
)
from support import make_cipher

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
TIFF_LE = b"II*\x00\x08\x00\x00\x00"
TIFF_BE = b"MM\x00*\x00\x00\x00\x08"
DOCX = b"PK\x03\x04\x14\x00\x06\x00"
XLSX = DOCX
XML = b"\xef\xbb\xbf  " + '<?xml version="1.0" encoding="UTF-8"?><Доверенность/>'.encode()
DER = b"\x30\x82\x05\x00\x06\x09"
PEM = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


class DetectContentTypeTest(unittest.TestCase):
    def test_supported_types(self) -> None:
        cases = {
            "выписка.pdf": (PDF, "application/pdf"),
            "скан.JPG": (JPEG, "image/jpeg"),
            "скан.jpeg": (JPEG, "image/jpeg"),
            "фото.png": (PNG, "image/png"),
            "лист.tif": (TIFF_LE, "image/tiff"),
            "лист.TIFF": (TIFF_BE, "image/tiff"),
            "политика.docx": (DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "мчд.xml": (XML, "application/xml"),
            "мчд.xml.sig": (DER, "application/pkcs7-signature"),
            "подпись.p7s": (DER, "application/pkcs7-signature"),
            "сертификат.cer": (DER, "application/pkix-cert"),
            "сертификат.crt": (PEM, "application/pkix-cert"),
        }
        for name, (content, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(detect_content_type(name, content), expected)

    def test_pdf_header_may_follow_junk(self) -> None:
        self.assertEqual(detect_content_type("a.pdf", b"\x00" * 100 + PDF), "application/pdf")

    def test_unknown_extension(self) -> None:
        for name in ("page.html", "image.svg", "script.js", "archive.zip", "noext", "file.exe"):
            with self.subTest(name=name):
                with self.assertRaises(UnsupportedFile):
                    detect_content_type(name, PDF)

    def test_content_must_match_extension(self) -> None:
        # HTML под видом PDF или картинки не пройдёт: проверяется содержимое.
        html = b"<html><script>alert(1)</script></html>"
        for name in ("a.pdf", "a.jpg", "a.png", "a.tif", "a.docx", "a.cer"):
            with self.subTest(name=name):
                with self.assertRaises(UnsupportedFile):
                    detect_content_type(name, html)

    def test_empty_file(self) -> None:
        with self.assertRaises(UnsupportedFile):
            detect_content_type("a.pdf", b"")


class StorageFilesTest(unittest.TestCase):
    def test_every_allowed_extension_has_a_content_type(self) -> None:
        for allowed in (DOCUMENT_FILES, STORAGE_FILES):
            self.assertLessEqual(set(allowed.extensions), set(CONTENT_TYPES))

    def test_storage_accepts_the_agreed_formats(self) -> None:
        cases = {
            "договор.pdf": (PDF, "application/pdf"),
            "скан.jpeg": (JPEG, "image/jpeg"),
            "скан.JPG": (JPEG, "image/jpeg"),
            "фото.png": (PNG, "image/png"),
            "лист.tiff": (TIFF_LE, "image/tiff"),
            "лист.tif": (TIFF_BE, "image/tiff"),
            "акт.docx": (DOCX, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "сверка.xlsx": (XLSX, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "упд.xml": (XML, "application/xml"),
            "договор.pdf.sig": (DER, "application/pkcs7-signature"),
            "подпись.p7s": (DER, "application/pkcs7-signature"),
        }
        for name, (content, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(detect_content_type(name, content, STORAGE_FILES), expected)

    def test_storage_rejects_everything_else(self) -> None:
        # Архивы, исполняемые файлы и форматы с макросами, а также сертификат,
        # который живёт в наборе документов.
        for name in ("archive.zip", "setup.exe", "run.bat", "old.doc", "old.xls", "macro.docm",
                     "macro.xlsm", "page.html", "image.svg", "photo.gif", "cert.cer", "noext"):
            with self.subTest(name=name):
                with self.assertRaises(UnsupportedFile):
                    detect_content_type(name, PDF, STORAGE_FILES)

    def test_storage_checks_content_too(self) -> None:
        html = b"<html><script>alert(1)</script></html>"
        for name in ("a.pdf", "a.jpg", "a.png", "a.tif", "a.docx", "a.xlsx", "a.sig"):
            with self.subTest(name=name):
                with self.assertRaises(UnsupportedFile):
                    detect_content_type(name, html, STORAGE_FILES)

    def test_excel_is_not_a_document_of_the_set(self) -> None:
        with self.assertRaises(UnsupportedFile):
            detect_content_type("сверка.xlsx", XLSX)

    def test_hint_matches_the_section(self) -> None:
        with self.assertRaisesRegex(UnsupportedFile, "XLSX"):
            detect_content_type("a.zip", PDF, STORAGE_FILES)
        with self.assertRaisesRegex(UnsupportedFile, r"\.cer") as raised:
            detect_content_type("a.zip", PDF)
        self.assertNotIn("XLSX", str(raised.exception))


class FilenameTest(unittest.TestCase):
    def test_paths_and_control_characters_are_removed(self) -> None:
        self.assertEqual(clean_filename("C:\\Users\\anna\\Устав.PDF"), "Устав.pdf")
        self.assertEqual(clean_filename("../../etc/passwd.pdf"), "passwd.pdf")
        self.assertEqual(clean_filename("вы\x00пис\nка.pdf"), "выписка.pdf")
        self.assertEqual(clean_filename('a<b>:"c|d?*.pdf'), "a_b___c_d__.pdf")

    def test_empty_stem_gets_default(self) -> None:
        self.assertEqual(clean_filename(".pdf"), "document.pdf")
        self.assertEqual(clean_filename("   "), "document")

    def test_long_names_keep_extension(self) -> None:
        name = clean_filename("я" * 400 + ".pdf")
        self.assertEqual(len(name), 150)
        self.assertTrue(name.endswith(".pdf"))

    def test_content_disposition(self) -> None:
        header = content_disposition("Выписка ЕГРЮЛ.pdf")
        self.assertTrue(header.startswith('attachment; filename="'))
        self.assertIn("filename*=UTF-8''%D0%92%D1%8B%D0%BF%D0%B8%D1%81%D0%BA%D0%B0%20", header)
        self.assertNotIn("\n", content_disposition("a\nb.pdf"))


class CipherTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        cipher = make_cipher()
        aad = document_aad("doc-1", "owner-1")
        sealed = cipher.seal(PDF, aad)
        self.assertNotIn(b"%PDF", sealed.ciphertext)
        self.assertEqual(cipher.open(sealed, aad), PDF)

    def test_nonce_is_unique(self) -> None:
        cipher = make_cipher()
        aad = document_aad("doc-1", "owner-1")
        self.assertNotEqual(cipher.seal(PDF, aad).nonce, cipher.seal(PDF, aad).nonce)

    def test_ciphertext_is_bound_to_document_and_owner(self) -> None:
        cipher = make_cipher()
        sealed = cipher.seal(PDF, document_aad("doc-1", "owner-1"))
        for aad in (document_aad("doc-2", "owner-1"), document_aad("doc-1", "owner-2")):
            with self.subTest(aad=aad):
                with self.assertRaises(InvalidTag):
                    cipher.open(sealed, aad)

    def test_wrong_key(self) -> None:
        sealed = make_cipher().seal(PDF, b"aad")
        other = FileCipher(base64.b64encode(b"\x01" * 32).decode(), "k1")
        with self.assertRaises(InvalidTag):
            other.open(sealed, b"aad")

    def test_key_must_be_32_bytes_of_base64(self) -> None:
        for key in ("", "not base64!", base64.b64encode(b"short").decode()):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    FileCipher(key, "k1")


class LinkTokenTest(unittest.TestCase):
    def test_token_shape_and_hash(self) -> None:
        token = new_link_token()
        self.assertRegex(token, TOKEN_PATTERN)
        self.assertNotEqual(token, new_link_token())
        self.assertEqual(len(token_hash(token)), 32)


if __name__ == "__main__":
    unittest.main()
