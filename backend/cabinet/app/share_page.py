"""Страница «Скачать», на которую ведёт ссылка /s/<токен>.

Сам файл отдаётся только после нажатия кнопки (POST), а не по открытию адреса:
мессенджеры и почтовые клиенты «разворачивают» ссылки обычным GET-запросом, и
такой запрос списал бы скачивание у ссылки с лимитом. Страница не требует
JavaScript и стоит на тех же шрифтах, что и сайт. Токен остаётся только в
адресе: поэтому у страницы запрещён Referer и индексация.
"""

from datetime import datetime, timedelta, timezone
from html import escape

from app.reminders import long_date, plural_days
from app.store import ShareLink, StoredDocument

MOSCOW = timezone(timedelta(hours=3))


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} КБ".replace(".", ",")
    return f"{size / (1024 * 1024):.1f} МБ".replace(".", ",")


def plural_downloads(count: int) -> str:
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        return f"{count} скачивание"
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return f"{count} скачивания"
    return f"{count} скачиваний"


def validity_note(link: ShareLink, now: datetime | None = None) -> str:
    current = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    expires = link.expires_at.astimezone(MOSCOW)
    days = max(0, (expires.date() - current.date()).days)
    note = f"Ссылка действует до {long_date(expires.date())}"
    note += " (сегодня последний день)" if days == 0 else f" — ещё {plural_days(days)}"
    if link.max_downloads is not None:
        left = max(0, link.max_downloads - link.download_count)
        note += f". Осталось {plural_downloads(left)}"
    return note + "."


_STYLE = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background-color:#F5F4EF;
background-image:radial-gradient(rgba(23,24,21,.06) .7px,transparent .7px);background-size:9px 9px;
color:#171815;font-family:"Manrope",Arial,sans-serif;-webkit-font-smoothing:antialiased}
.card{width:100%;max-width:520px;padding:40px 36px 32px;border:1px solid rgba(23,24,21,.1);border-radius:32px;
background:#fff;box-shadow:0 28px 80px rgba(23,24,21,.12)}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:28px;text-decoration:none;color:inherit}
.brand img{width:40px;height:40px;display:block}
.brand span{font-size:18px;font-weight:900;letter-spacing:-.07em}
.kicker{margin:0;font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;color:#7C68FF}
h1{margin:12px 0 0;font-size:30px;line-height:1.1;font-weight:900;letter-spacing:-.05em;word-break:break-word}
.meta{margin:14px 0 0;font-size:14px;font-weight:700;color:rgba(23,24,21,.5)}
.note{margin:22px 0 0;padding:14px 18px;border-radius:20px;background:#DDD9FF;font-size:13px;font-weight:700;line-height:1.55}
button{display:inline-flex;align-items:center;justify-content:center;gap:12px;width:100%;margin-top:26px;padding:17px 24px;
border:0;border-radius:999px;background:#171815;color:#fff;font:800 15px "Manrope",Arial,sans-serif;cursor:pointer;
transition:transform .15s,box-shadow .15s}
button:hover{transform:translateY(-2px);box-shadow:0 12px 32px rgba(23,24,21,.08)}
button:focus-visible{outline:3px solid rgba(124,104,255,.45);outline-offset:3px}
button i{display:grid;place-items:center;width:26px;height:26px;border-radius:50%;background:#C8FF32;color:#171815;
font-style:normal;font-weight:900}
.foot{margin:24px 0 0;font-size:12px;font-weight:600;line-height:1.55;color:rgba(23,24,21,.45)}
@media (max-width:480px){.card{padding:32px 22px 26px;border-radius:26px}h1{font-size:25px}}
"""


def render_landing(document: StoredDocument, link: ShareLink) -> str:
    name = escape(document.filename)
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<meta name="theme-color" content="#171815">
<title>Скачать документ — ФНСКит</title>
<link rel="icon" href="/favicon.ico" type="image/x-icon" sizes="any">
<link rel="icon" type="image/svg+xml" href="/images/logo.svg">
<link rel="stylesheet" href="/fonts.css">
<style>{_STYLE}</style>
</head>
<body>
<main class="card">
  <a class="brand" href="/" aria-label="ФНСКит"><img src="/images/logo.svg" alt=""><span>ФНСКит</span></a>
  <p class="kicker">Вам отправили документ</p>
  <h1>{name}</h1>
  <p class="meta">{format_size(document.size_bytes)}</p>
  <p class="note">{escape(validity_note(link))}</p>
  <form method="post">
    <button type="submit">Скачать файл <i aria-hidden="true">&gt;</i></button>
  </form>
  <p class="foot">Файл хранится в зашифрованном виде и отдаётся без изменений. ФНСКит не является официальным сервисом ФНС.</p>
</main>
</body>
</html>
"""
