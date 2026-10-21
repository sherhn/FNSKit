<#--
  Каркас всех HTML-писем. Вёрстка почтовая: таблицы и инлайновые стили, потому
  что почтовые клиенты (особенно Outlook) не понимают современный CSS.

  Логотип берётся с самого сайта. Адрес сайта выводим из ссылки в письме
  (…/auth/realms/…): она всегда строится от публичного адреса. В письмах без
  ссылки (уведомления о событиях) остаётся только слово «ФНСКит».
-->
<#assign siteUrl = (link!"")?keep_before("/auth/")>
<#assign font = "Manrope, 'Segoe UI', Arial, Helvetica, sans-serif">

<#macro emailLayout title="">
<!doctype html>
<html lang="${locale.language}" dir="${(ltr)?then('ltr','rtl')}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light">
  <title>${title}</title>
  <style>
    body { margin: 0; padding: 0; background: #F5F4EF; }
    p { margin: 0 0 16px; }
    a { color: #7C68FF; }
    @media (max-width: 600px) { .card { padding: 28px 22px !important; } }
  </style>
</head>
<body style="margin:0;padding:0;background:#F5F4EF;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#F5F4EF" style="background:#F5F4EF;">
  <tr>
    <td align="center" style="padding:32px 16px;">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px;">
        <tr>
          <td style="padding:0 8px 20px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <#if siteUrl?has_content>
                <td style="padding-right:12px;vertical-align:middle;">
                  <img src="${siteUrl}/images/apple-touch-icon.png" width="40" height="40" alt="" style="display:block;width:40px;height:40px;border-radius:12px;border:0;">
                </td>
                </#if>
                <td style="vertical-align:middle;font-family:${font};font-size:20px;font-weight:900;letter-spacing:-0.07em;color:#171815;">ФНСКит</td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td class="card" bgcolor="#FFFFFF" style="background:#FFFFFF;border:1px solid #E4E3DD;border-top:6px solid #C8FF32;border-radius:28px;padding:36px 36px 28px;font-family:${font};font-size:15px;line-height:1.65;color:#171815;">
            <#if title?has_content>
            <h1 style="margin:0 0 18px;font-family:${font};font-size:26px;line-height:1.15;font-weight:900;letter-spacing:-0.05em;color:#171815;">${title}</h1>
            </#if>
            <#nested>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 12px 0;font-family:${font};font-size:12px;line-height:1.6;color:#8A8A85;">
            Письмо отправлено автоматически, отвечать на него не нужно.<br>
            ФНСКит не является официальным сервисом ФНС и не заменяет консультацию бухгалтера или юриста.
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
</#macro>

<#-- Главная кнопка письма: чернильная «таблетка». -->
<#macro button href label>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 22px;">
  <tr>
    <td align="center" bgcolor="#171815" style="background:#171815;border-radius:999px;">
      <a href="${href}" style="display:inline-block;padding:15px 30px;font-family:${font};font-size:15px;font-weight:800;line-height:1;color:#FFFFFF;text-decoration:none;border-radius:999px;">${label}</a>
    </td>
  </tr>
</table>
</#macro>

<#-- Подпись под кнопкой: срок действия и сама ссылка на случай, если кнопка не открывается. -->
<#macro linkNote expiration>
<p style="margin:0 0 6px;font-size:13px;color:#5C5C58;">${msg("fnskitLinkNote", expiration)}</p>
<p style="margin:0 0 20px;font-size:11px;line-height:1.5;word-break:break-all;"><a href="${link}" style="color:#8A8A85;">${link}</a></p>
</#macro>
