# Keycloak ФНСКит: аккаунты, вход, регистрация и двухфакторная защита.
#
# Образ собирается «оптимизированным» (kc.sh build): параметры сборки
# зашиваются заранее, поэтому контейнер стартует быстрее и не перестраивает
# себя на каждом запуске. Digest закрепляет проверенную версию базового образа.
FROM quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244 AS build

# Параметры сборки — менять их можно только пересборкой образа:
#   * postgres — единственная поддерживаемая боевая база;
#   * /auth — Keycloak живёт на том же домене, что и сайт, под этим префиксом;
#   * health — /auth/health/ready для healthcheck на порту управления 9000.
ENV KC_DB=postgres \
    KC_HTTP_RELATIVE_PATH=/auth \
    KC_HEALTH_ENABLED=true

COPY --chown=keycloak:root themes/ /opt/keycloak/themes/
RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244

COPY --from=build /opt/keycloak/ /opt/keycloak/

# Экземпляр один, поэтому кеш локальный: без кластера JGroups, поиска соседей
# через базу и лишних потоков. Это параметр запуска, а не сборки.
ENV KC_CACHE=local
# Realm импортируется при первом старте (--import-realm). Если realm уже есть
# в базе, файл пропускается: дальнейшие правки — через админку или kcadm.sh.
COPY --chown=keycloak:root realm/ /opt/keycloak/data/import/

# Проверка готовности без curl (его в образе нет): bash открывает соединение к
# порту управления сам. /auth/health/ready отвечает 200, когда есть связь с базой.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD ["/bin/bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/9000 && printf 'GET /auth/health/ready HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n' >&3 && grep -q 'HTTP/1.1 200' <&3"]

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start", "--optimized", "--import-realm"]
