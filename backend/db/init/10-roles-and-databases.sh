#!/bin/sh
# Первый старт пустого тома: две базы, две роли.
#
# Keycloak и кабинет делят один экземпляр Postgres, чтобы не держать два
# процесса в памяти, но видят только каждый свою базу: у ролей разные пароли,
# а подключаться к чужой базе (CONNECT) у них нет права. Суперпользователь
# postgres нужен только этому скрипту и обслуживанию через docker exec.
#
# Скрипт выполняет entrypoint официального образа ровно один раз, пока каталог
# данных пуст. Пароли из .env при следующих запусках уже ничего не меняют:
# сменить пароль роли можно только внутри базы (ALTER ROLE).
set -eu

: "${KEYCLOAK_DB_PASSWORD:?Не задан KEYCLOAK_DB_PASSWORD}"
: "${CABINET_DB_PASSWORD:?Не задан CABINET_DB_PASSWORD}"

# Пароли идут переменными psql (:'имя'), а не подстановкой в SQL: любые
# символы в пароле безопасны.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v keycloak_password="$KEYCLOAK_DB_PASSWORD" \
    -v cabinet_password="$CABINET_DB_PASSWORD" <<'SQL'
CREATE ROLE keycloak LOGIN PASSWORD :'keycloak_password';
CREATE ROLE cabinet LOGIN PASSWORD :'cabinet_password';

CREATE DATABASE keycloak OWNER keycloak;
CREATE DATABASE cabinet OWNER cabinet;

REVOKE CONNECT ON DATABASE keycloak FROM PUBLIC;
REVOKE CONNECT ON DATABASE cabinet FROM PUBLIC;
SQL
