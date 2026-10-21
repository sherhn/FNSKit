"""Каркас HTML-писем кабинета — тот же вид, что у писем Keycloak
(backend/keycloak/themes/fnskit/email/html/template.ftl): бумажный фон, белая
карточка с лаймовой полосой, чернильная кнопка-«таблетка».

Вёрстка почтовая — таблицы и инлайновые стили: почтовые клиенты, особенно
Outlook, не понимают современный CSS. Все подставляемые значения экранируются.
"""

from html import escape

FONT = "Manrope, 'Segoe UI', Arial, Helvetica, sans-serif"

TONES = {
    # Фон и цвет текста плашки состояния: спокойно, скоро, срочно, просрочено.
    "ok": ("#DDD9FF", "#171815"),
    "soon": ("#E9F9B8", "#171815"),
    "urgent": ("#FFDFA8", "#171815"),
    "expired": ("#FEE2E2", "#B91C1C"),
}


def button(href: str, label: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 22px;">'
        '<tr><td align="center" bgcolor="#171815" style="background:#171815;border-radius:999px;">'
        f'<a href="{escape(href, quote=True)}" style="display:inline-block;padding:15px 30px;font-family:{FONT};'
        'font-size:15px;font-weight:800;line-height:1;color:#FFFFFF;text-decoration:none;border-radius:999px;">'
        f"{escape(label)}</a></td></tr></table>"
    )


def status_box(heading: str, lines: list[str], tone: str) -> str:
    """Плашка с главным фактом письма: что за документ и сколько осталось."""
    background, color = TONES[tone]
    rows = "".join(
        f'<div style="margin-top:4px;font-size:13px;line-height:1.5;color:{color};opacity:.75;">{escape(line)}</div>'
        for line in lines
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 22px;">'
        f'<tr><td bgcolor="{background}" style="background:{background};border-radius:20px;padding:18px 22px;'
        f'font-family:{FONT};color:{color};">'
        f'<div style="font-size:17px;font-weight:800;line-height:1.3;letter-spacing:-0.02em;">{escape(heading)}</div>'
        f"{rows}</td></tr></table>"
    )


def paragraph(text: str, muted: bool = False) -> str:
    style = "margin:0 0 16px;"
    if muted:
        style = "margin:0 0 10px;font-size:13px;color:#5C5C58;"
    return f'<p style="{style}">{escape(text)}</p>'


def render(title: str, inner_html: str, site_url: str) -> str:
    logo = (
        '<td style="padding-right:12px;vertical-align:middle;">'
        f'<img src="{escape(site_url, quote=True)}/images/apple-touch-icon.png" width="40" height="40" alt="" '
        'style="display:block;width:40px;height:40px;border-radius:12px;border:0;"></td>'
        if site_url
        else ""
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light">
<title>{escape(title)}</title>
<style>
body{{margin:0;padding:0;background:#F5F4EF}}
a{{color:#7C68FF}}
@media (max-width:600px){{.card{{padding:28px 22px !important}}}}
</style>
</head>
<body style="margin:0;padding:0;background:#F5F4EF;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F5F4EF" style="background:#F5F4EF;">
<tr><td align="center" style="padding:32px 16px;">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px;">
<tr><td style="padding:0 8px 20px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>{logo}
<td style="vertical-align:middle;font-family:{FONT};font-size:20px;font-weight:900;letter-spacing:-0.07em;color:#171815;">ФНСКит</td>
</tr></table>
</td></tr>
<tr><td class="card" bgcolor="#FFFFFF" style="background:#FFFFFF;border:1px solid #E4E3DD;border-top:6px solid #C8FF32;border-radius:28px;padding:36px 36px 28px;font-family:{FONT};font-size:15px;line-height:1.65;color:#171815;">
<h1 style="margin:0 0 18px;font-family:{FONT};font-size:26px;line-height:1.15;font-weight:900;letter-spacing:-0.05em;color:#171815;">{escape(title)}</h1>
{inner_html}
</td></tr>
<tr><td style="padding:20px 12px 0;font-family:{FONT};font-size:12px;line-height:1.6;color:#8A8A85;">
Письмо отправлено автоматически, отвечать на него не нужно.<br>
ФНСКит не является официальным сервисом ФНС и не заменяет консультацию бухгалтера или юриста.
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""
