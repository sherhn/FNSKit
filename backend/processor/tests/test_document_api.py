import io
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("PROCESSING_TIMEOUT_SECONDS", "10")

from fastapi.testclient import TestClient
from PIL import Image
from starlette.requests import ClientDisconnect, Request

from app import main


requires_tiffcp = unittest.skipUnless(shutil.which("tiffcp"), "для сборки TIFF нужен tiffcp")

CONSENT = {"personal_data_consent": "true", "consent_version": main.personal_data_consent_version}


def _png(color: str = "white", size: tuple[int, int] = (64, 80)) -> bytes:
    encoded = io.BytesIO()
    Image.new("RGB", size, color).save(encoded, format="PNG")
    return encoded.getvalue()


def _forbid_body_read():
    """Любое чтение тела запроса роняет тест: form(), body() и json() читают
    его через Request.stream."""
    return patch.object(
        Request,
        "stream",
        side_effect=AssertionError("тело запроса прочитано до лимитера"),
    )


def _workspace_spy():
    """Подменяет create_workspace и запоминает созданные рабочие папки."""
    created: list[Path] = []
    original = main.create_workspace

    def spy() -> Path:
        created.append(original())
        return created[-1]

    return created, patch("app.main.create_workspace", spy)


class RateLimitTests(unittest.TestCase):
    """Ручка анонимная, а Starlette кладёт загруженные файлы во временный файл на
    диске процессора ещё при чтении формы. Поэтому лимитер срабатывает до
    первого прочитанного байта тела, и этот порядок закреплён тестом."""

    def test_rate_limit_is_applied_before_the_body_is_read(self) -> None:
        with (
            patch.object(main.limiter, "enabled", True),
            patch.object(main.settings, "rate_limit_tiff", "1/minute"),
            TestClient(main.app) as client,
        ):
            try:
                first = client.post("/v1/tiff", data={"profile": "premium", **CONSENT})
                with _forbid_body_read():
                    second = client.post("/v1/tiff", data={"profile": "premium", **CONSENT})
            finally:
                main.limiter.reset()

        self.assertEqual(first.status_code, 422)
        self.assertEqual(second.status_code, 429)


class ConsentTests(unittest.TestCase):
    def _post(self, client: TestClient, **data):
        return client.post(
            "/v1/tiff",
            data={"profile": "vat", "output_format": "pdf", **data},
            files={"files": ("scan.png", _png(), "image/png")},
        )

    def test_request_without_consent_is_refused_and_leaves_nothing_behind(self) -> None:
        created, spy = _workspace_spy()
        with spy, TestClient(main.app) as client:
            missing = self._post(client)
            unticked = self._post(client, personal_data_consent="false", consent_version=main.personal_data_consent_version)

        for response in (missing, unticked):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"], "Нужно отметить согласие на обработку персональных данных.")
        self.assertEqual(len(created), 2)
        self.assertFalse(any(path.exists() for path in created), "остался рабочий каталог")

    def test_stale_consent_version_is_refused(self) -> None:
        with TestClient(main.app) as client:
            response = self._post(client, personal_data_consent="true", consent_version="2020-01-01.1")

        self.assertEqual(response.status_code, 422)

    def test_consent_with_the_current_version_is_accepted(self) -> None:
        with TestClient(main.app) as client:
            response = self._post(client, **CONSENT)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content[:5], b"%PDF-")


class FormTests(unittest.TestCase):
    def test_workspace_is_removed_when_parameters_are_invalid(self) -> None:
        created, spy = _workspace_spy()
        with spy, TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "premium", **CONSENT},
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Профиль должен быть vat, registration или custom.")
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists(), "остался рабочий каталог")

    def test_request_without_files_is_refused(self) -> None:
        with TestClient(main.app) as client:
            # Без файлов TestClient отправляет urlencoded-форму, а не multipart.
            response = client.post("/v1/tiff", data={"profile": "vat", **CONSENT})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Добавьте хотя бы один файл.")

    def test_aborted_upload_is_reported_and_leaves_nothing_behind(self) -> None:
        async def aborted_stream(self):
            # Клиент прислал начало поля и пропал: обрыв посреди загрузки.
            boundary = self.headers["content-type"].split("boundary=")[1]
            yield f'--{boundary}\r\nContent-Disposition: form-data; name="profile"\r\n\r\nva'.encode()
            raise ClientDisconnect()

        created, spy = _workspace_spy()
        with spy, patch.object(Request, "stream", aborted_stream), TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "vat", **CONSENT},
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(any(path.exists() for path in created), "остался рабочий каталог")

    def test_several_files_and_their_order_are_read_from_the_form(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={
                    "profile": "vat",
                    "output_format": "pdf",
                    "page_order": '[{"source_index": 1, "page_number": 1}, {"source_index": 0, "page_number": 1}]',
                    **CONSENT,
                },
                files=[
                    ("files", ("first.png", _png("white"), "image/png")),
                    ("files", ("second.png", _png("black"), "image/png")),
                ],
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-page-count"], "2")
        self.assertNotIn("x-remaining-uses", response.headers)
        self.assertEqual(response.content[:5], b"%PDF-")

    @requires_tiffcp
    def test_custom_profile_parameters_are_read_from_the_form(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "custom", "dpi": "150", "color_mode": "bw", "compression": "g4", **CONSENT},
                files={"files": ("scan.png", _png(size=(300, 400)), "image/png")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        with Image.open(io.BytesIO(response.content), formats=["TIFF"]) as tiff:
            self.assertEqual(tiff.mode, "1")
            self.assertEqual(tiff.tag_v2[259], 4)
            self.assertEqual(float(tiff.tag_v2[282]), 150)

    def test_malformed_dpi_is_refused_for_the_custom_profile(self) -> None:
        with TestClient(main.app) as client:
            response = client.post(
                "/v1/tiff",
                data={"profile": "custom", "dpi": "abc", "color_mode": "bw", "compression": "g4", **CONSENT},
                files={"files": ("scan.png", _png(), "image/png")},
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("разрешение", response.json()["detail"])


class ServiceTests(unittest.TestCase):
    def test_removed_routes_are_gone(self) -> None:
        with TestClient(main.app) as client:
            demo = client.post("/v1/demo", data={"profile": "vat", **CONSENT})

        self.assertEqual(demo.status_code, 404)

    def test_readiness_depends_only_on_tiffcp(self) -> None:
        with patch("app.main.shutil.which", return_value="/usr/bin/tiffcp"), TestClient(main.app) as client:
            ready = client.get("/health/ready")
        with patch("app.main.shutil.which", return_value=None), TestClient(main.app) as client:
            not_ready = client.get("/health/ready")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(not_ready.status_code, 503)

    def test_cors_preflight_from_local_development_is_allowed(self) -> None:
        """Локально страница и процессор на разных origin: без предзапроса
        браузер не отправит форму."""
        with TestClient(main.app) as client:
            response = client.options(
                "/v1/tiff",
                headers={
                    "Origin": "http://localhost:63342",
                    "Access-Control-Request-Method": "POST",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:63342")

    def test_openapi_describes_the_hand_read_form(self) -> None:
        with TestClient(main.app) as client:
            schema = client.get("/openapi.json").json()

        operation = schema["paths"]["/v1/tiff"]["post"]
        self.assertFalse([item for item in operation.get("parameters", []) if item["in"] == "header"])
        form = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
        self.assertEqual(form["required"], ["profile", "files", "personal_data_consent", "consent_version"])
        self.assertEqual(form["properties"]["consent_version"]["enum"], [main.personal_data_consent_version])
        self.assertNotIn("401", operation["responses"])
        self.assertNotIn("/v1/demo", schema["paths"])


if __name__ == "__main__":
    unittest.main()
