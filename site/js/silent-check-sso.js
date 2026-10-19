// Тихая проверка входа (keycloak-js, silentCheckSsoRedirectUri). Keycloak
// возвращает скрытый фрейм сюда с результатом в адресе, а страница передаёт
// адрес родителю — только своему origin. Отдельным файлом, а не встроенным
// скриптом: CSP сайта встроенные скрипты запрещает.
parent.postMessage(location.href, location.origin);
