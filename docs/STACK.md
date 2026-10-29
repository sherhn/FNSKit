# Технологический стек ФНСКит

Фактическое описание технологий, библиотек, рантаймов и компонентов инфраструктуры проекта.

---

## 1. Общая архитектура

Проект состоит из клиентской части (статический сайт и редактор), веб-сервера / reverse proxy (Nginx) и изолированных бэкенд-сервисов в контейнерах Docker:

```
[ Клиент (Браузер) ]
         │ (HTTP / HTTPS)
         ▼
     [ Traefik (внешний reverse proxy / Dokploy) ]
         │ (HTTP, порт 80 в docker-сети)
         ▼
[ nginx (контейнер site) ] ─── раздача статики (HTML, CSS, JS, WOFF2)
         │
         ├─── /api/processor/*  ──► [ processor (FastAPI, :8080) ]
         ├─── /api/cabinet/*    ──► [ cabinet (FastAPI, :8080) ] ──► [ cabinet-db (PostgreSQL 17) ]
         │                                                    └──► [ S3 (шифротекст файлов) ]
         └─── /auth/*           ──► [ keycloak (:8080) ]         ──► [ keycloak-db (PostgreSQL 17) ]
```

---

## 2. Фронтенд (`site/`)

### Разметка и архитектура страниц
* **Тип приложения:** Многостраничное статическое приложение (MPA, Multi-Page Application) без SPA-фреймворков.
* **Формат:** Статические файлы HTML5 (`site/*.html`).
* **Маршрутизация:** Чистые URL-адреса без расширений (реализованы на уровне Nginx: `/editor-dlya-fns`, `/account`, `/privacy` с 301-редиректом с `.html`).
* **Метаданные:** OpenGraph, Twitter Cards, PWA-манифест (`site.webmanifest`), `robots.txt`.

### Стилизация и типографика
* **Tailwind CSS:** Версия `3.4.19`. Сборка через CLI (`@tailwindcss/cli`), конфигурация в `tailwind.config.js`. Классы извлекаются сканированием `./*.html` и `./js/**/*.js`.
* **Пользовательские стили:**
  * `site/style.css` — типографика, оформление статей (`.article-*`), правовых разделов (`.legal-*`) и общих компонентов;
  * `site/mobile.css` — адаптивные правки и сенсорные области для мобильных устройств;
  * `site/fonts.css` — объявление локальных шрифтов.
* **Шрифты:** Вариативный шрифт **Manrope** (ось весов 200–800) в формате WOFF2 (`site/fonts/manrope-variable.woff2`). Внешние CDN-шрифты не используются.

### JavaScript и сборка
* **Язык:** Нативный JavaScript (Vanilla JS, стандарт ES2020+), без транспиляции в старые стандарты.
* **Сборка и минификация:** Node.js-скрипт `site/scripts/build-js.mjs` на базе **Terser** (`5.49.0`):
  * Двухпроходное сжатие (`compress: { passes: 2 }`);
  * Манглинг идентификаторов (`mangle: true`);
  * Вырезание комментариев, генерация файлов в `site/dist/js/`.
* **Рантайм сборки:** Node.js 22 Alpine (в build-стадии Dockerfile).

### Клиентские библиотеки
Копируются скриптом сборки из `node_modules` в `site/dist/vendor/` и подключаются локально:
* **PDF.js** (`pdfjs-dist` `6.1.200`): рендеринг и предпросмотр страниц PDF в браузере с использованием Web Worker (`pdf.worker.mjs`).
* **UTIF.js** (`utif` `3.1.0`): декодирование и отрисовка TIFF-файлов в браузере на элементах `<canvas>`.
* **keycloak-js** (`26.2.4`): официальный адаптер Keycloak для аутентификации (OpenID Connect / OAuth 2.0) и фоновой проверки сессии через iframe (`silent-check-sso.html`).

---

## 3. Веб-сервер и обратный прокси (`nginx`)

* **Базовый образ:** `nginx:1.30-alpine`.
* **Режим работы:** Единственный публичный сервис приложения, работающий за внешним прокси Traefik.
* **Сборка (Multi-stage Dockerfile):**
  * Стадия 1 (`node:22-alpine`): установка зависимостей (`npm ci`), сборка CSS через Tailwind и минификация JS через Terser;
  * Стадия 2 (`nginx:1.30-alpine`): копирование артефактов из `dist/` и статических файлов, запуск Nginx.
* **Функции Nginx (`site/nginx.conf`):**
  * **Безопасность (CSP):** `default-src 'self'; script-src 'self'; connect-src 'self'; worker-src 'self' blob:; img-src 'self' data: blob:`. Внешние источники скриптов, стилей и шрифтов запрещены.
  * **Заголовки безопасности:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Strict-Transport-Security`.
  * **Лимиты и защита:**
    * `limit_req_zone` для API (10 r/s) и форм авторизации Keycloak (30 r/m);
    * `limit_conn_zone` для загрузок (не более 2 параллельных загрузок с одного IP, не более 6 суммарно по сайту);
    * `client_max_body_size`: 64 КБ глобально, 110 МБ для `/api/processor/`, 21 МБ для `/api/cabinet/`.
  * **Проксирование:**
    * `/api/processor/*` ──► `http://processor:8080/*` (с потоковой передачей `proxy_request_buffering off`);
    * `/api/cabinet/*` ──► `http://cabinet:8080/*`;
    * `/s/*` ──► `http://cabinet:8080/v1/shared/*` (скачивание расшаренных документов по токену);
    * `/auth/*` ──► `http://keycloak:8080/*` (блокируя доступ к `/auth/admin` и master realm снаружи).

---

## 4. Бэкенд-сервисы (`backend/`)

### Сервис обработки (`processor`)
* **Среда выполнения:** Python 3.12+ (Alpine/Debian в Docker).
* **Фреймворк:** FastAPI `0.139.2`, ASGI-сервер Uvicorn `0.51.0`.
* **Работа с документами и графикой:**
  * **Pillow** (`12.3.0`): растровые преобразования, конвертация цветовых моделей (оттенки серого, 1-битный бинаризованный монохром), сжатие (LZW, CCITT Group 4, PackBits);
  * **PyMuPDF / fitz** (`1.28.0`): извлечение страниц из исходных PDF, сборка итогового потокового image-only PDF с разрешением 300 DPI.
* **Вспомогательные библиотеки:**
  * `slowapi` (`0.1.10`) — rate limiting;
  * `python-multipart` (`0.0.32`) — обработка потоковой загрузки файлов `multipart/form-data`;
  * `pydantic-settings` (`2.14.2`) — конфигурация через переменные окружения;
  * `httpx` (`0.28.1`) — HTTP-клиент.
* **Хранение данных:** Stateless. База данных отсутствует, загруженные файлы сохраняются исключительно во временную директорию на диске (`processor-workspace:/tmp`) на время обработки запроса и очищаются фоновым процессом.

### Сервис кабинета (`cabinet`)
* **Среда выполнения:** Python 3.12+ в Docker.
* **Фреймворк:** FastAPI `0.139.2`, Uvicorn `0.51.0`.
* **Библиотеки:**
  * `asyncpg` (`0.32.0`) — асинхронный драйвер PostgreSQL;
  * `PyJWT[crypto]` (`2.15.1`) и `cryptography` (`50.0.2`) — валидация JWT-токенов Keycloak;
  * `pydantic-settings` (`2.14.2`), `httpx` (`0.28.1`).
* **База данных (`cabinet-db`):** PostgreSQL `17-alpine`. Хранит метаданные постоянных документов, хеши токенов общего доступа, сроки и очередь объектов на удаление.
* **Хранилище файлов:** любой S3-совместимый бакет, клиент свой (`app/objects.py`, подпись AWS SigV4 поверх `httpx`, без boto3). В бакете только шифротекст AES-256-GCM. Локально — **RustFS** `1.0.4` (сервис `s3` в `docker-compose.yml`, консоль на `localhost:9001`).

### Идентификация (`keycloak`)
* **Сервис:** Keycloak `26` (Quarkus runtime).
* **Протоколы:** OpenID Connect, OAuth 2.0.
* **База данных (`keycloak-db`):** PostgreSQL `17-alpine`.
* **Темизация:** Кастомная тема в `backend/keycloak/themes/fnskit/`.

---

## 5. Инфраструктура и развёртывание

* **Оркестрация:** Docker Compose (`backend/docker-compose.yml`, `backend/docker-compose.prod.yml`).
* **Сеть:** Внутренние сети Docker (`backend`, `default`) и внешняя сеть `dokploy-network` для взаимодействия с внешним обратным прокси (Traefik).
* **Контейнерные реестры:** GitHub Container Registry (`ghcr.io/sherhn/fnskit-*`) для готовых продакшен-образов.
