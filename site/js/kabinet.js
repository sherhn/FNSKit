// Кабинет компании.
//
// Вход — через Keycloak на том же домене (/auth/): keycloak-js получает токены
// по коду авторизации с PKCE и держит их только в памяти страницы. Профиль и
// набор документов отдаёт cabinet API (/api/cabinet), статус двухфакторной
// защиты — Account API самого Keycloak. Подключение и отключение 2FA, смену
// пароля и данных выполняет Keycloak на своих страницах («действия по запросу
// приложения», kc_action), после чего возвращает пользователя сюда.
(() => {
  const AUTH_URL = `${location.origin}/auth`;
  const REALM = "fnskit";
  const CLIENT_ID = "fnskit-web";
  const KEYCLOAK_MODULE = "/vendor/keycloak-js-26.2.4.mjs";
  const CABINET_API = "/api/cabinet";
  const ACCOUNT_API = `${AUTH_URL}/realms/${REALM}/account`;
  const RETURN_URL = `${location.origin}/kabinet`;

  const $ = (id) => document.getElementById(id);
  const STATES = ["stateLoading", "stateGuest", "stateUser", "stateError"];

  // Что сказать, когда Keycloak вернул пользователя после действия.
  const ACTION_MESSAGES = {
    CONFIGURE_TOTP: {
      success: "Двухфакторная защита включена: при входе спросим код из приложения.",
      cancelled: "Подключение приложения отменено."
    },
    delete_credential: {
      success: "Приложение-аутентификатор отключено. Вход снова только по паролю.",
      cancelled: "Приложение осталось подключённым."
    },
    UPDATE_PASSWORD: {
      success: "Пароль изменён.",
      cancelled: "Пароль остался прежним."
    },
    UPDATE_PROFILE: {
      success: "Данные сохранены.",
      cancelled: "Данные не изменились."
    }
  };

  let keycloak = null;
  let pendingAction = null;

  function showState(id) {
    for (const state of STATES) $(state).classList.toggle("hidden", state !== id);
    const signedIn = id === "stateUser";
    $("logoutButton").classList.toggle("hidden", !signedIn);
    $("headerHome").classList.toggle("hidden", signedIn);
  }

  function showMessage(element, text, tone) {
    element.textContent = text;
    element.className = tone === "error"
      ? "mt-6 rounded-xl bg-red-50 px-4 py-3 text-sm font-bold text-red-700"
      : tone === "neutral"
        ? "mt-6 rounded-xl border border-ink/10 bg-white px-4 py-3 text-sm font-bold text-ink/70"
        : "mt-6 rounded-xl bg-lime px-4 py-3 text-sm font-bold text-ink";
  }

  function showError(text) {
    if (text) $("errorText").textContent = text;
    showState("stateError");
  }

  function showGuest(message) {
    if (message) showMessage($("guestMessage"), message, "neutral");
    showState("stateGuest");
  }

  // --- Запросы с токеном ---------------------------------------------------

  class SessionExpired extends Error {}

  async function authorizedFetch(url) {
    try {
      // Токен живёт 5 минут; обновляем заранее, чтобы он не истёк в пути.
      await keycloak.updateToken(30);
    } catch {
      throw new SessionExpired();
    }
    const response = await fetch(url, {
      // Без Accept: application/json адрес Account API отдаёт HTML консоли
      // аккаунта, а не JSON.
      headers: { Authorization: `Bearer ${keycloak.token}`, Accept: "application/json" },
      credentials: "omit",
      cache: "no-store"
    });
    if (response.status === 401) throw new SessionExpired();
    return response;
  }

  async function readDetail(response, fallback) {
    try {
      const body = await response.json();
      if (typeof body.detail === "string") return body.detail;
    } catch {
      // Тело не JSON — например, страница nginx. Показываем общий текст.
    }
    return fallback;
  }

  // --- Профиль и документы -------------------------------------------------

  function renderProfile(profile) {
    const name = [profile.first_name, profile.last_name].filter(Boolean).join(" ");
    $("userGreeting").textContent = profile.first_name ? `Здравствуйте, ${profile.first_name}.` : "Здравствуйте.";
    $("userKicker").textContent = profile.business_type_label
      ? `Кабинет компании · ${profile.business_type === "ip" ? "ИП" : "ООО"}`
      : "Кабинет компании";
    $("userSubtitle").textContent = [profile.company_name, profile.email].filter(Boolean).join(" · ");

    $("profileName").textContent = name || "—";
    $("profileEmail").textContent = profile.email || "—";
    $("profileBusinessType").textContent = profile.business_type_label || "Не указана";
    $("profileCompany").textContent = profile.company_name || "—";
  }

  function pluralDocuments(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return `${count} документ`;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} документа`;
    return `${count} документов`;
  }

  function documentItem(doc) {
    const item = document.createElement("li");
    item.className = "p-4 sm:p-5";

    const head = document.createElement("div");
    head.className = "flex flex-wrap items-start justify-between gap-2";
    const title = document.createElement("p");
    title.className = "font-extrabold tracking-[-.02em]";
    title.textContent = doc.title;
    const badge = document.createElement("span");
    badge.className = doc.level === "basic"
      ? "shrink-0 rounded-full bg-lime px-2.5 py-1 text-[10px] font-extrabold uppercase tracking-[.1em]"
      : "shrink-0 rounded-full bg-[#DDD9FF] px-2.5 py-1 text-[10px] font-extrabold uppercase tracking-[.1em]";
    badge.textContent = doc.level === "basic" ? "Базовый" : "По ситуации";
    head.append(title, badge);

    const purpose = document.createElement("p");
    purpose.className = "mt-2 text-sm font-medium leading-relaxed text-ink/60";
    purpose.textContent = doc.purpose;

    const source = document.createElement("p");
    source.className = "mt-2 text-xs font-semibold leading-relaxed text-ink/50";
    const sourceLabel = document.createElement("span");
    sourceLabel.className = "font-extrabold text-ink/70";
    sourceLabel.textContent = "Где взять: ";
    source.append(sourceLabel, doc.source);

    item.append(head, purpose, source);
    if (doc.note) {
      const note = document.createElement("p");
      note.className = "mt-2 text-xs font-bold leading-relaxed text-violet";
      note.textContent = doc.note;
      item.append(note);
    }
    return item;
  }

  function renderDocuments(profile, groups) {
    const list = $("documentsList");
    list.replaceChildren();
    const hasSet = groups.length > 0;
    $("documentsEmpty").classList.toggle("hidden", hasSet);
    $("documentsCount").classList.toggle("hidden", !hasSet);
    $("documentsTitle").textContent = profile.business_type === "ip"
      ? "Документы ИП"
      : profile.business_type === "ooo" ? "Документы ООО" : "Документы";
    if (!hasSet) return;

    let total = 0;
    let basic = 0;
    for (const group of groups) {
      const section = document.createElement("section");
      const heading = document.createElement("h3");
      heading.className = "text-xs font-extrabold uppercase tracking-[.14em] text-ink/45";
      heading.textContent = group.title;
      const items = document.createElement("ul");
      items.className = "mt-3 divide-y divide-ink/10 rounded-2xl border border-ink/10 bg-paper/60";
      for (const doc of group.documents) {
        items.append(documentItem(doc));
        total += 1;
        if (doc.level === "basic") basic += 1;
      }
      section.append(heading, items);
      list.append(section);
    }
    $("documentsCount").textContent = `${pluralDocuments(total)} · базовых ${basic}`;
  }

  async function loadCabinet() {
    const response = await authorizedFetch(`${CABINET_API}/v1/me`);
    if (!response.ok) {
      const fallback = response.status === 403
        ? "Доступ к кабинету закрыт."
        : "Кабинет не ответил. Попробуйте через пару минут.";
      const error = new Error(await readDetail(response, fallback));
      error.status = response.status;
      throw error;
    }
    const data = await response.json();
    renderProfile(data.profile);
    renderDocuments(data.profile, data.document_groups);
  }

  // --- Двухфакторная защита -------------------------------------------------

  function otpDeviceItem(credential) {
    const item = document.createElement("li");
    item.className = "flex items-center justify-between gap-3 rounded-2xl bg-white/10 px-4 py-3";
    const label = document.createElement("div");
    label.className = "min-w-0";
    const name = document.createElement("p");
    name.className = "truncate text-sm font-extrabold";
    name.textContent = credential.userLabel || "Приложение-аутентификатор";
    label.append(name);
    if (credential.createdDate) {
      const created = document.createElement("p");
      created.className = "text-xs font-semibold text-white/50";
      created.textContent = `Подключено ${new Date(credential.createdDate).toLocaleDateString("ru-RU")}`;
      label.append(created);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "shrink-0 rounded-full border border-white/20 px-3 py-2 text-xs font-extrabold transition hover:border-lime hover:text-lime";
    remove.textContent = "Отключить";
    remove.addEventListener("click", () => runAction(`delete_credential:${credential.id}`));
    item.append(label, remove);
    return item;
  }

  function renderOtp(credentials) {
    const enabled = credentials.length > 0;
    const status = $("otpStatus");
    status.textContent = enabled ? "Включена" : "Выключена";
    status.className = enabled
      ? "mt-4 inline-flex items-center gap-2 rounded-full bg-lime px-3 py-1.5 text-xs font-extrabold text-ink"
      : "mt-4 inline-flex items-center gap-2 rounded-full bg-white/10 px-3 py-1.5 text-xs font-extrabold text-white";
    $("otpHint").textContent = enabled
      ? "При входе после пароля спросим шестизначный код из приложения."
      : "Добавьте к паролю код из приложения-аутентификатора, например Google Authenticator. Даже если пароль утечёт, без телефона в кабинет не войти.";

    const devices = $("otpDevices");
    devices.replaceChildren(...credentials.map(otpDeviceItem));
    devices.classList.toggle("hidden", !enabled);
    $("otpEnableButton").classList.toggle("hidden", enabled);
  }

  async function loadOtp() {
    const response = await authorizedFetch(`${ACCOUNT_API}/credentials?type=otp`);
    if (!response.ok) throw new Error(`Account API: ${response.status}`);
    const types = await response.json();
    const otp = Array.isArray(types) ? types.find((type) => type.type === "otp") : null;
    const metadata = otp && Array.isArray(otp.userCredentialMetadatas) ? otp.userCredentialMetadatas : [];
    renderOtp(metadata.map((entry) => entry.credential).filter((credential) => credential && credential.id));
  }

  // --- Действия -------------------------------------------------------------

  function runAction(action) {
    keycloak.login({ action, redirectUri: RETURN_URL });
  }

  function reportAction() {
    if (!pendingAction) return;
    const { status, action } = pendingAction;
    pendingAction = null;
    const key = Object.keys(ACTION_MESSAGES).find((name) => action && action.startsWith(name));
    const message = $("userMessage");
    if (status === "error") {
      showMessage(message, "Действие не выполнено. Попробуйте ещё раз.", "error");
    } else if (key && ACTION_MESSAGES[key][status]) {
      showMessage(message, ACTION_MESSAGES[key][status], status === "success" ? "success" : "neutral");
    }
  }

  async function showUser() {
    try {
      await loadCabinet();
    } catch (error) {
      if (error instanceof SessionExpired) {
        showGuest("Сессия истекла. Войдите снова.");
        return;
      }
      if (error.status === 403) {
        showGuest(error.message);
        return;
      }
      showError(error.message);
      return;
    }
    showState("stateUser");
    reportAction();

    try {
      await loadOtp();
    } catch (error) {
      if (error instanceof SessionExpired) {
        showGuest("Сессия истекла. Войдите снова.");
        return;
      }
      $("otpStatus").textContent = "Статус недоступен";
      $("otpHint").textContent = "Не удалось узнать, подключено ли приложение. Управлять входом можно в настройках аккаунта.";
    }
  }

  function bindButtons() {
    $("loginButton").addEventListener("click", () => keycloak.login({ redirectUri: RETURN_URL }));
    $("registerButton").addEventListener("click", () => keycloak.register({ redirectUri: RETURN_URL }));
    $("logoutButton").addEventListener("click", () => keycloak.logout({ redirectUri: RETURN_URL }));
    $("otpEnableButton").addEventListener("click", () => runAction("CONFIGURE_TOTP"));
    $("passwordButton").addEventListener("click", () => runAction("UPDATE_PASSWORD"));
    $("profileButton").addEventListener("click", () => runAction("UPDATE_PROFILE"));
    $("businessTypeButton").addEventListener("click", () => runAction("UPDATE_PROFILE"));
    $("retryButton").addEventListener("click", () => location.reload());
  }

  async function start() {
    let Keycloak;
    try {
      ({ default: Keycloak } = await import(KEYCLOAK_MODULE));
    } catch {
      showError("Не удалось загрузить модуль входа. Обновите страницу.");
      return;
    }

    keycloak = new Keycloak({ url: AUTH_URL, realm: REALM, clientId: CLIENT_ID });
    keycloak.onActionUpdate = (status, action) => {
      pendingAction = { status, action };
    };
    // Сессия в Keycloak закончилась (истекла или выход в другой вкладке).
    keycloak.onAuthRefreshError = () => showGuest("Сессия истекла. Войдите снова.");
    bindButtons();

    let authenticated = false;
    try {
      authenticated = await keycloak.init({
        onLoad: "check-sso",
        silentCheckSsoRedirectUri: `${location.origin}/silent-check-sso`,
        redirectUri: RETURN_URL,
        pkceMethod: "S256",
        // Опрос сессии через iframe раз в 5 секунд не нужен: токен и так
        // обновляется перед каждым запросом.
        checkLoginIframe: false
      });
    } catch {
      showError();
      return;
    }

    if (authenticated) {
      await showUser();
    } else {
      showGuest();
    }
  }

  start();
})();
