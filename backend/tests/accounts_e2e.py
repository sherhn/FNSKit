#!/usr/bin/env python3
"""Сквозная проверка аккаунтов на запущенном локальном стеке.

Проходит путь пользователя так же, как браузер, но без него: формы Keycloak,
обмен кода на токены с PKCE, API кабинета, подключение 2FA, вход с кодом,
блокировка после перебора кодов и сброс пароля по письму (через Mailpit).
Нужен только Python 3.11+ — сторонних пакетов нет.

Запуск (стек поднят по backend/README.md):

    KEYCLOAK_ADMIN_PASSWORD=... python backend/tests/accounts_e2e.py

Тест сам создаёт пользователя через админский API и удаляет его в конце.
Необязательные переменные: FNSKIT_SITE_URL (http://localhost:8082),
KEYCLOAK_ADMIN_URL (http://localhost:8083/auth), MAILPIT_URL
(http://localhost:8025).
"""

import base64
import hashlib
import hmac
import html
import http.cookiejar
import json
import os
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = os.environ.get("FNSKIT_SITE_URL", "http://localhost:8082").rstrip("/")
ADMIN = os.environ.get("KEYCLOAK_ADMIN_URL", "http://localhost:8083/auth").rstrip("/")
ADMIN_PASSWORD = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "")
MAILPIT = os.environ.get("MAILPIT_URL", "http://localhost:8025").rstrip("/")

REALM = f"{SITE}/auth/realms/fnskit"
OIDC = f"{REALM}/protocol/openid-connect"
CLIENT_ID = "fnskit-web"
RETURN_URL = f"{SITE}/kabinet"

passed = 0


def check(condition: bool, title: str, page=None) -> None:
    global passed
    if not condition:
        print(f"  ✗ {title}")
        if isinstance(page, Response):
            print(f"    HTTP {page.status}: {describe(page)}")
        elif page is not None:
            print(f"    {page}")
        raise SystemExit(1)
    passed += 1
    print(f"  ✓ {title}")


# --- HTTP ---------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Response:
    def __init__(self, status: int, headers, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


def http_request(opener, method, url, data=None, headers=None) -> Response:
    payload = None
    if isinstance(data, dict):
        payload = urllib.parse.urlencode(data).encode()
    elif isinstance(data, (bytes, str)):
        payload = data.encode() if isinstance(data, str) else data
    request = urllib.request.Request(url, data=payload, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=30) as response:
            return Response(response.status, response.headers, response.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, error.headers, error.read())


class Browser:
    """Куки и ручные редиректы: возврат на сайт с кодом нужно перехватить."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect
        )

    def go(self, method: str, url: str, data=None) -> Response | str:
        """Страница Keycloak (Response) или адрес возврата на сайт (str)."""
        for _ in range(10):
            response = http_request(self.opener, method, url, data)
            # Keycloak ставит cookie с флагом Secure. Браузеры отправляют их и
            # на http://localhost, а CookieJar — нет; ведём себя как браузер.
            for cookie in self.jar:
                cookie.secure = False
            if response.status not in (301, 302, 303, 307):
                return response
            location = urllib.parse.urljoin(url, response.headers["Location"])
            if location.startswith(RETURN_URL):
                return location
            method, url, data = "GET", location, None
        raise RuntimeError("Слишком много редиректов")


def describe(page: Response) -> str:
    """Заголовок и сообщения страницы Keycloak — чтобы понять, где тест свернул не туда."""
    parts = re.findall(r'<h1[^>]*>(.*?)</h1>|kc-feedback-text">(.*?)<', page.body, re.S)
    text = " | ".join(" ".join(filter(None, part)).strip() for part in parts)
    forms = re.findall(r'<form[^>]*id="([^"]+)"', page.body)
    return f"{html.unescape(text)[:300]} формы: {forms}"


def form_action(page: Response, form_id: str) -> str:
    tag = re.search(rf'<form[^>]*id="{form_id}"[^>]*>', page.body)
    if not tag:
        raise AssertionError(f"На странице нет формы {form_id}")
    return html.unescape(re.search(r'action="([^"]+)"', tag.group(0)).group(1))


def hidden(page: Response, name: str) -> str:
    match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', page.body) or re.search(
        rf'value="([^"]*)"[^>]*name="{name}"', page.body
    )
    if not match:
        raise AssertionError(f"На странице нет поля {name}")
    return html.unescape(match.group(1))


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


_last_step: int | None = None


def totp(secret: str) -> str:
    """Текущий код. Keycloak хранит секрет строкой, в приложение уходит base32
    её байтов. Один код дважды не принимается (otpPolicyCodeReusable=false),
    поэтому для следующего входа ждём новое 30-секундное окно."""
    global _last_step
    while int(time.time()) // 30 == _last_step:
        time.sleep(1)
    counter = _last_step = int(time.time()) // 30
    digest = hmac.new(secret.encode(), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _forget_step() -> None:
    # Код, отклонённый из-за блокировки, не считается использованным.
    global _last_step
    _last_step = None


# --- OIDC -----------------------------------------------------------------------


class Login:
    """Один вход по коду авторизации с PKCE, как у keycloak-js."""

    def __init__(self, browser: Browser, **extra: str) -> None:
        self.browser = browser
        self.verifier = b64url(secrets.token_bytes(48))
        self.state = secrets.token_urlsafe(16)
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": RETURN_URL,
            "response_type": "code",
            "response_mode": "query",
            "scope": "openid",
            "state": self.state,
            "code_challenge": b64url(hashlib.sha256(self.verifier.encode()).digest()),
            "code_challenge_method": "S256",
            **extra,
        }
        self.page = browser.go("GET", f"{OIDC}/auth?{urllib.parse.urlencode(params)}")

    def exchange(self, location: str) -> dict:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        assert query["state"] == [self.state], "state не совпал"
        response = http_request(
            urllib.request.build_opener(),
            "POST",
            f"{OIDC}/token",
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": query["code"][0],
                "redirect_uri": RETURN_URL,
                "code_verifier": self.verifier,
            },
        )
        assert response.status == 200, response.body
        return response.json()


def cabinet_me(token: str) -> Response:
    return http_request(
        urllib.request.build_opener(),
        "GET",
        f"{SITE}/api/cabinet/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    )


# --- Админский API: тестовый пользователь --------------------------------------


class Admin:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener()

    def _token(self) -> str:
        # Токен администратора realm master живёт минуту — берём новый на вызов.
        response = http_request(
            self.opener,
            "POST",
            f"{ADMIN}/realms/master/protocol/openid-connect/token",
            {"grant_type": "password", "client_id": "admin-cli", "username": "admin",
             "password": ADMIN_PASSWORD},
        )
        assert response.status == 200, f"Не удалось войти администратором: {response.body}"
        return response.json()["access_token"]

    def call(self, method: str, path: str, body=None) -> Response:
        data = json.dumps(body) if body is not None else None
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        return http_request(self.opener, method, f"{ADMIN}/admin/realms/fnskit{path}", data, headers)


# --- Сценарий -------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if not ADMIN_PASSWORD:
        print("Задайте KEYCLOAK_ADMIN_PASSWORD (пароль из .env).")
        raise SystemExit(2)

    plain = urllib.request.build_opener(_NoRedirect)
    print("Публикация через nginx")
    for path in ("/auth/", "/auth/admin/master/console/", "/auth/realms/master/account/",
                 "/api/cabinet/docs"):
        check(http_request(plain, "GET", SITE + path).status == 404, f"{path} закрыт (404)")
    discovery = http_request(plain, "GET", f"{REALM}/.well-known/openid-configuration").json()
    check(discovery["issuer"] == REALM, "издатель токенов — публичный адрес realm")
    check(discovery["token_endpoint"].startswith(REALM), "discovery на публичном адресе")

    admin = Admin()
    email = f"e2e-{secrets.token_hex(4)}@example.test"
    password = "E2e-" + secrets.token_urlsafe(12)
    created = admin.call("POST", "/users", {
        "username": email, "email": email, "emailVerified": True, "enabled": True,
        "firstName": "Борис", "lastName": "Проверкин",
        "attributes": {"business_type": ["ooo"], "company_name": ["ООО «Проверка»"]},
        "credentials": [{"type": "password", "value": password, "temporary": False}],
    })
    assert created.status == 201, created.body
    user_id = created.headers["Location"].rsplit("/", 1)[-1]

    try:
        print("Вход по паролю и токены")
        browser = Browser()
        login = Login(browser)
        check(isinstance(login.page, Response) and "kc-form-login" in login.page.body,
              "страница входа Keycloak")
        result = browser.go("POST", form_action(login.page, "kc-form-login"),
                            {"username": email, "password": "wrong-password"})
        check(isinstance(result, Response) and "kc-form-login" in result.body,
              "неверный пароль не пускает")
        result = browser.go("POST", form_action(result, "kc-form-login"),
                            {"username": email, "password": password})
        check(isinstance(result, str), "верный пароль возвращает на /kabinet с кодом")
        tokens = login.exchange(result)
        access = claims(tokens["access_token"])
        check("fnskit-cabinet" in access["aud"] and access["azp"] == CLIENT_ID,
              "access-токен для кабинета, клиент fnskit-web")
        check(access["business_type"] == "ooo" and access["typ"] == "Bearer",
              "в токене форма бизнеса")

        print("API кабинета")
        me = cabinet_me(tokens["access_token"])
        check(me.status == 200 and me.headers["Cache-Control"] == "no-store", "/v1/me → 200, no-store")
        ids = {doc["id"] for group in me.json()["document_groups"] for doc in group["documents"]}
        check("charter" in ids and "egrip_extract" not in ids, "набор документов ООО")
        check(cabinet_me(tokens["id_token"]).status == 401, "ID-токен вместо access → 401")
        head, body, signature = tokens["access_token"].split(".")
        forged = dict(access, business_type="ip", sub="someone-else")
        forged_token = ".".join([head, b64url(json.dumps(forged).encode()), signature])
        check(cabinet_me(forged_token).status == 401, "подменённый токен → 401")
        check(cabinet_me("").status == 401, "без токена → 401")

        print("Ротация refresh-токена")
        refreshed = http_request(urllib.request.build_opener(), "POST", f"{OIDC}/token", {
            "grant_type": "refresh_token", "client_id": CLIENT_ID,
            "refresh_token": tokens["refresh_token"]})
        check(refreshed.status == 200, "refresh-токен обновляет сессию")
        reused = http_request(urllib.request.build_opener(), "POST", f"{OIDC}/token", {
            "grant_type": "refresh_token", "client_id": CLIENT_ID,
            "refresh_token": tokens["refresh_token"]})
        check(reused.status == 400, "повторное использование старого refresh-токена отклонено")

        print("Подключение 2FA")
        setup = Login(browser, kc_action="CONFIGURE_TOTP")
        check(isinstance(setup.page, Response) and "kc-totp-settings-form" in setup.page.body,
              "Keycloak показывает настройку приложения")
        secret = hidden(setup.page, "totpSecret")
        action = form_action(setup.page, "kc-totp-settings-form")
        result = browser.go("POST", action, {"totp": "000000", "totpSecret": secret,
                                             "userLabel": "Тест"})
        check(isinstance(result, Response) and "kc-totp-settings-form" in result.body,
              "неверный код при настройке отклонён")
        result = browser.go("POST", form_action(result, "kc-totp-settings-form"),
                            {"totp": totp(secret), "totpSecret": secret, "userLabel": "Тест"})
        check(isinstance(result, str) and "kc_action_status=success" in result,
              "приложение подключено, возврат с kc_action_status=success")
        setup.exchange(result)

        print("Вход с кодом из приложения")
        browser = Browser()
        login = Login(browser)
        result = browser.go("POST", form_action(login.page, "kc-form-login"),
                            {"username": email, "password": password})
        check(isinstance(result, Response) and 'name="otp"' in result.body,
              "после пароля спрашивают код")
        result = browser.go("POST", form_action(result, "kc-otp-login-form"), {"otp": totp(secret)})
        check(isinstance(result, str), "верный код пускает")
        login.exchange(result)

        print("Перебор кодов")
        browser = Browser()
        login = Login(browser)
        page = browser.go("POST", form_action(login.page, "kc-form-login"),
                          {"username": email, "password": password})
        for attempt in range(5):
            wrong = f"{(int(time.time()) * 7919 + attempt) % 1_000_000:06d}"
            page = browser.go("POST", form_action(page, "kc-otp-login-form"), {"otp": wrong})
            assert isinstance(page, Response)
            time.sleep(1.1)
        status = admin.call("GET", f"/attack-detection/brute-force/users/{user_id}").json()
        check(status["disabled"] is True, "после 5 неверных кодов аккаунт временно заблокирован")
        page = browser.go("POST", form_action(page, "kc-otp-login-form"), {"otp": totp(secret)})
        check(isinstance(page, Response), "во время блокировки не пускает даже с верным кодом")
        _forget_step()
        admin.call("DELETE", f"/attack-detection/brute-force/users/{user_id}")

        print("Сброс пароля по письму")
        browser = Browser()
        login = Login(browser)
        reset_link = html.unescape(re.search(r'href="([^"]*reset-credentials[^"]*)"',
                                             login.page.body).group(1))
        page = browser.go("GET", urllib.parse.urljoin(OIDC, reset_link))
        page = browser.go("POST", form_action(page, "kc-reset-password-form"), {"username": email})
        link = None
        for _ in range(20):
            found = http_request(urllib.request.build_opener(), "GET",
                                 f"{MAILPIT}/api/v1/search?query=" + urllib.parse.quote(f"to:{email}"))
            messages = [m for m in found.json().get("messages", [])
                        if "парол" in m["Subject"].lower() or "password" in m["Subject"].lower()]
            if messages:
                text = http_request(urllib.request.build_opener(), "GET",
                                    f"{MAILPIT}/api/v1/message/{messages[0]['ID']}").json()["Text"]
                link = re.search(r"https?://\S+action-token\S+", text).group(0)
                break
            time.sleep(0.5)
        check(link is not None and link.startswith(REALM), "письмо со ссылкой сброса пришло")
        page = browser.go("GET", link)
        if isinstance(page, Response) and 'name="otp"' in page.body:
            # Ссылка из письма не заменяет второй фактор: код спрашивают и тут.
            check(True, "при сбросе пароля тоже спрашивают код из приложения")
            page = browser.go("POST", form_action(page, "kc-otp-login-form"), {"otp": totp(secret)})
        check(isinstance(page, Response) and 'name="password-new"' in page.body,
              "форма нового пароля", page)
        new_password = "E2e-" + secrets.token_urlsafe(12)
        result = browser.go("POST", form_action(page, "kc-passwd-update-form"),
                            {"password-new": new_password, "password-confirm": new_password})
        check(isinstance(result, str), "новый пароль сохранён, возврат на сайт")

        browser = Browser()
        login = Login(browser)
        page = browser.go("POST", form_action(login.page, "kc-form-login"),
                          {"username": email, "password": password})
        check(isinstance(page, Response) and "kc-form-login" in page.body, "старый пароль больше не подходит")
    finally:
        admin.call("DELETE", f"/users/{user_id}")

    print(f"\nВсе проверки пройдены: {passed}.")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(f"  ✗ {error}")
        sys.exit(1)
