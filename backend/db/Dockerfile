# Postgres ФНСКит: один экземпляр на базы Keycloak и кабинета. Роли и базы
# создаёт скрипт из init/ при первом старте пустого тома.
# Digest закрепляет проверенную версию базового образа.
FROM postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24

# Скрипт должен остаться с LF и быть исполняемым: иначе entrypoint запустит его
# как обычный файл и упрётся в «bad interpreter». LF закреплён в .gitattributes.
COPY --chmod=0755 init/ /docker-entrypoint-initdb.d/
