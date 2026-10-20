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
  const RETURN_URL = `${location.origin}/account`;

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

  async function authorizedFetch(url, options = {}) {
    try {
      // Токен живёт 5 минут; обновляем заранее, чтобы он не истёк в пути.
      await keycloak.updateToken(30);
    } catch {
      throw new SessionExpired();
    }
    const response = await fetch(url, {
      ...options,
      // Без Accept: application/json адрес Account API отдаёт HTML консоли
      // аккаунта, а не JSON.
      headers: { Accept: "application/json", ...options.headers, Authorization: `Bearer ${keycloak.token}` },
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

  async function failure(response, fallback) {
    const error = new Error(await readDetail(response, fallback));
    error.status = response.status;
    return error;
  }

  // Ошибка действия с файлом: сессия истекла — на экран входа, иначе текст.
  function reportFileError(error, fallback) {
    if (error instanceof SessionExpired) {
      showGuest("Сессия истекла. Войдите снова.");
      return;
    }
    showMessage($("userMessage"), error.message || fallback, "error");
  }

  // --- Профиль и документы -------------------------------------------------

  const cabinet = { profile: null, groups: [], files: [], usage: null };
  // Документ, для которого открыт выбор файла, и файл с открытой панелью ссылок.
  let uploadKind = null;
  let sharingFileId = null;
  // Только что созданная ссылка: её адрес показываем один раз.
  let createdLink = null;
  const bytesFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
  const dateFormat = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" });

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} Б`;
    if (bytes < 1024 * 1024) return `${bytesFormat.format(bytes / 1024)} КБ`;
    return `${bytesFormat.format(bytes / (1024 * 1024))} МБ`;
  }

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

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function smallButton(label, onClick, tone = "default") {
    const button = element(
      "button",
      tone === "danger"
        ? "rounded-full border border-ink/10 bg-white px-3 py-1.5 text-xs font-extrabold text-red-700 transition hover:border-red-300 disabled:opacity-40"
        : "rounded-full border border-ink/15 bg-white px-3 py-1.5 text-xs font-extrabold transition hover:border-ink disabled:opacity-40",
      label
    );
    button.type = "button";
    button.addEventListener("click", onClick);
    return button;
  }

  function createdLinkBlock(link) {
    const block = element("div", "mt-3");
    const field = element("input", "w-full rounded-xl border border-ink/15 bg-white px-3 py-2 text-xs font-bold");
    field.readOnly = true;
    field.value = link.url;
    field.setAttribute("aria-label", "Ссылка на файл");
    field.addEventListener("focus", () => field.select());
    const copy = smallButton("Скопировать", async () => {
      try {
        await navigator.clipboard.writeText(link.url);
        copy.textContent = "Скопировано";
      } catch {
        field.focus();
      }
    });
    const footer = element("div", "mt-2 flex flex-wrap items-center justify-between gap-2");
    footer.append(
      element(
        "span",
        "text-xs font-bold text-ink/60",
        `Действует до ${dateFormat.format(new Date(link.expires_at))}. Сохраните её сейчас: показать её ещё раз нельзя, только создать новую.`
      ),
      copy
    );
    block.append(field, footer);
    return block;
  }

  function sharePanel(file) {
    const panel = element("div", "mt-3 rounded-2xl bg-[#DDD9FF] p-4");
    panel.append(element(
      "p",
      "text-xs font-semibold leading-relaxed",
      "Любой, у кого есть ссылка, сможет скачать файл без входа, пока она действует. Отозвать её можно в любой момент."
    ));

    const row = element("div", "mt-3 flex flex-wrap items-center gap-2");
    const days = element("select", "rounded-xl border border-ink/15 bg-white px-3 py-2 text-xs font-bold");
    days.setAttribute("aria-label", "Срок действия ссылки");
    for (const [value, label] of [["1", "на 1 день"], ["7", "на 7 дней"], ["30", "на 30 дней"]]) {
      const option = element("option", "", label);
      option.value = value;
      option.selected = value === "7";
      days.append(option);
    }
    const create = smallButton("Создать ссылку", async () => {
      create.disabled = true;
      try {
        const response = await authorizedFetch(`${CABINET_API}/v1/documents/${file.id}/links`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ days: Number(days.value) })
        });
        if (!response.ok) throw await failure(response, "Не удалось создать ссылку.");
        createdLink = { fileId: file.id, link: await response.json() };
        await refreshFiles();
      } catch (error) {
        create.disabled = false;
        reportFileError(error, "Не удалось создать ссылку.");
      }
    });
    row.append(days, create);
    panel.append(row);
    if (createdLink && createdLink.fileId === file.id) panel.append(createdLinkBlock(createdLink.link));

    if (file.links.length) {
      const list = element("ul", "mt-3 space-y-1.5");
      for (const link of file.links) {
        const item = element("li", "flex flex-wrap items-center justify-between gap-2 rounded-xl bg-white/70 px-3 py-2 text-xs font-semibold");
        const downloads = link.download_count ? ` · скачиваний: ${link.download_count}` : " · ещё не скачивали";
        item.append(element("span", "", `Ссылка до ${dateFormat.format(new Date(link.expires_at))}${downloads}`));
        item.append(smallButton("Отозвать", async () => {
          try {
            const response = await authorizedFetch(`${CABINET_API}/v1/links/${link.id}`, { method: "DELETE" });
            if (!response.ok && response.status !== 404) throw await failure(response, "Не удалось отозвать ссылку.");
            if (createdLink && createdLink.link.id === link.id) createdLink = null;
            showMessage($("userMessage"), "Ссылка отозвана: по ней больше нельзя скачать файл.", "neutral");
            await refreshFiles();
          } catch (error) {
            reportFileError(error, "Не удалось отозвать ссылку.");
          }
        }, "danger"));
        list.append(item);
      }
      panel.append(list);
    }
    return panel;
  }

  function fileRow(file) {
    const row = element("li", "rounded-xl border border-ink/10 bg-white px-3 py-2.5");
    const head = element("div", "flex flex-wrap items-center justify-between gap-2");
    const info = element("div", "min-w-0");
    info.append(element("p", "truncate text-sm font-extrabold", file.filename));
    const links = file.links.length ? ` · активных ссылок: ${file.links.length}` : "";
    info.append(element(
      "p",
      "text-xs font-semibold text-ink/45",
      `${formatBytes(file.size_bytes)} · загружен ${dateFormat.format(new Date(file.created_at))}${links}`
    ));

    const actions = element("div", "flex flex-wrap gap-1.5");
    actions.append(
      smallButton("Скачать", () => downloadFile(file)),
      smallButton(sharingFileId === file.id ? "Скрыть" : "Поделиться", () => {
        sharingFileId = sharingFileId === file.id ? null : file.id;
        createdLink = null;
        renderDocuments();
      }),
      smallButton("Удалить", () => deleteFile(file), "danger")
    );
    head.append(info, actions);
    row.append(head);
    if (sharingFileId === file.id) row.append(sharePanel(file));
    return row;
  }

  function documentItem(doc, files) {
    const item = element("li", "p-4 sm:p-5");

    const head = element("div", "flex flex-wrap items-start justify-between gap-2");
    head.append(element("p", "font-extrabold tracking-[-.02em]", doc.title));
    const badges = element("div", "flex shrink-0 gap-1.5");
    if (files.length) {
      badges.append(element("span", "rounded-full bg-ink px-2.5 py-1 text-[10px] font-extrabold uppercase tracking-[.1em] text-lime", "Загружен"));
    }
    badges.append(element(
      "span",
      doc.level === "basic"
        ? "rounded-full bg-lime px-2.5 py-1 text-[10px] font-extrabold uppercase tracking-[.1em]"
        : "rounded-full bg-[#DDD9FF] px-2.5 py-1 text-[10px] font-extrabold uppercase tracking-[.1em]",
      doc.level === "basic" ? "Базовый" : "По ситуации"
    ));
    head.append(badges);

    const source = element("p", "mt-2 text-xs font-semibold leading-relaxed text-ink/50");
    source.append(element("span", "font-extrabold text-ink/70", "Где взять: "), doc.source);
    item.append(head, element("p", "mt-2 text-sm font-medium leading-relaxed text-ink/60", doc.purpose), source);
    if (doc.note) item.append(element("p", "mt-2 text-xs font-bold leading-relaxed text-violet", doc.note));

    if (files.length) {
      const list = element("ul", "mt-3 space-y-2");
      for (const file of files) list.append(fileRow(file));
      item.append(list);
    }
    const upload = element(
      "button",
      "mt-3 rounded-full bg-ink px-4 py-2 text-xs font-extrabold text-white transition hover:-translate-y-0.5 hover:shadow-card disabled:opacity-40",
      files.length ? "Загрузить ещё файл" : "Загрузить"
    );
    upload.type = "button";
    upload.dataset.kind = doc.id;
    upload.addEventListener("click", () => chooseFile(doc.id));
    item.append(upload);
    return item;
  }

  function renderDocuments() {
    const { profile, groups, files, usage } = cabinet;
    const list = $("documentsList");
    list.replaceChildren();
    const hasSet = groups.length > 0;
    $("documentsEmpty").classList.toggle("hidden", hasSet);
    $("documentsCount").classList.toggle("hidden", !hasSet);
    $("documentsTitle").textContent = profile.business_type === "ip"
      ? "Документы ИП"
      : profile.business_type === "ooo" ? "Документы ООО" : "Документы";

    const byKind = new Map();
    for (const file of files) {
      if (!byKind.has(file.kind)) byKind.set(file.kind, []);
      byKind.get(file.kind).push(file);
    }

    let basic = 0;
    let basicLoaded = 0;
    const shownKinds = new Set();
    for (const group of groups) {
      const section = element("section");
      section.append(element("h3", "text-xs font-extrabold uppercase tracking-[.14em] text-ink/45", group.title));
      const items = element("ul", "mt-3 divide-y divide-ink/10 rounded-2xl border border-ink/10 bg-paper/60");
      for (const doc of group.documents) {
        const docFiles = byKind.get(doc.id) || [];
        items.append(documentItem(doc, docFiles));
        shownKinds.add(doc.id);
        if (doc.level === "basic") {
          basic += 1;
          if (docFiles.length) basicLoaded += 1;
        }
      }
      section.append(items);
      list.append(section);
    }

    // Файлы прежней формы бизнеса (был ИП, стало ООО) не пропадают из вида.
    const orphans = files.filter((file) => !shownKinds.has(file.kind));
    if (orphans.length) {
      const section = element("section");
      section.append(element("h3", "text-xs font-extrabold uppercase tracking-[.14em] text-ink/45", "Из прежнего набора"));
      const items = element("ul", "mt-3 space-y-2");
      for (const file of orphans) items.append(fileRow(file));
      section.append(items);
      list.append(section);
    }

    if (hasSet) $("documentsCount").textContent = `Загружено базовых: ${basicLoaded} из ${basic}`;
    if (usage) {
      $("storageUsage").classList.remove("hidden");
      $("storageUsage").textContent =
        `Занято ${formatBytes(usage.size_bytes)} из ${formatBytes(usage.max_size_bytes)} · файлов ${usage.count} из ${usage.max_count} · один файл — до ${formatBytes(usage.max_file_bytes)}`;
    }
  }

  async function loadFiles() {
    const response = await authorizedFetch(`${CABINET_API}/v1/documents`);
    if (!response.ok) throw await failure(response, "Не удалось получить список файлов.");
    const data = await response.json();
    cabinet.files = data.files;
    cabinet.usage = data.usage;
  }

  async function refreshFiles() {
    await loadFiles();
    renderDocuments();
  }

  async function loadCabinet() {
    const response = await authorizedFetch(`${CABINET_API}/v1/me`);
    if (!response.ok) {
      throw await failure(
        response,
        response.status === 403 ? "Доступ к кабинету закрыт." : "Кабинет не ответил. Попробуйте через пару минут."
      );
    }
    const data = await response.json();
    cabinet.profile = data.profile;
    cabinet.groups = data.document_groups;
    renderProfile(data.profile);
    await loadFiles();
    renderDocuments();
  }

  // --- Загрузка, скачивание, удаление -----------------------------------------

  function chooseFile(kind) {
    uploadKind = kind;
    const input = $("fileInput");
    input.value = "";
    input.click();
  }

  async function uploadFile(file) {
    const kind = uploadKind;
    uploadKind = null;
    if (!kind || !file) return;
    const limit = cabinet.usage ? cabinet.usage.max_file_bytes : Infinity;
    if (file.size > limit) {
      showMessage($("userMessage"), `Файл больше ${formatBytes(limit)}. Сожмите скан или разбейте документ на части.`, "error");
      return;
    }
    const button = document.querySelector(`button[data-kind="${CSS.escape(kind)}"]`);
    if (button) {
      button.disabled = true;
      button.textContent = "Загружаем…";
    }
    try {
      const query = new URLSearchParams({ kind, filename: file.name });
      const response = await authorizedFetch(`${CABINET_API}/v1/documents?${query}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file
      });
      if (!response.ok) throw await failure(response, "Не удалось загрузить файл.");
      showMessage($("userMessage"), `Файл «${file.name}» загружен.`, "success");
      await refreshFiles();
    } catch (error) {
      reportFileError(error, "Не удалось загрузить файл.");
      renderDocuments();
    }
  }

  async function downloadFile(file) {
    try {
      const response = await authorizedFetch(`${CABINET_API}/v1/documents/${file.id}/content`, {
        headers: { Accept: "*/*" }
      });
      if (!response.ok) throw await failure(response, "Не удалось скачать файл.");
      const url = URL.createObjectURL(await response.blob());
      const link = element("a");
      link.href = url;
      link.download = file.filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (error) {
      reportFileError(error, "Не удалось скачать файл.");
    }
  }

  async function deleteFile(file) {
    const links = file.links.length ? " Ссылки на него перестанут работать." : "";
    if (!window.confirm(`Удалить «${file.filename}» из кабинета?${links}`)) return;
    try {
      const response = await authorizedFetch(`${CABINET_API}/v1/documents/${file.id}`, { method: "DELETE" });
      if (!response.ok && response.status !== 404) throw await failure(response, "Не удалось удалить файл.");
      if (sharingFileId === file.id) sharingFileId = null;
      showMessage($("userMessage"), `Файл «${file.filename}» удалён.`, "neutral");
      await refreshFiles();
    } catch (error) {
      reportFileError(error, "Не удалось удалить файл.");
    }
  }

  // --- Вкладки ----------------------------------------------------------------

  const TABS = [
    { tab: "tabDocuments", panel: "panelDocuments", hash: "#documents" },
    { tab: "tabActions", panel: "panelActions", hash: "#actions" }
  ];

  function selectTab(index, focus = false) {
    TABS.forEach((entry, position) => {
      const selected = position === index;
      const tab = $(entry.tab);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      tab.className = selected
        ? "rounded-full bg-ink px-5 py-2.5 text-sm font-extrabold text-white transition"
        : "rounded-full px-5 py-2.5 text-sm font-bold text-ink/60 transition hover:text-ink";
      $(entry.panel).classList.toggle("hidden", !selected);
      if (selected && focus) tab.focus();
    });
    history.replaceState(null, "", `${location.pathname}${TABS[index].hash}`);
  }

  function bindTabs() {
    TABS.forEach((entry, index) => {
      const tab = $(entry.tab);
      tab.addEventListener("click", () => selectTab(index));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
        event.preventDefault();
        const step = event.key === "ArrowRight" ? 1 : -1;
        selectTab((index + step + TABS.length) % TABS.length, true);
      });
    });
    $("fileInput").addEventListener("change", (event) => uploadFile(event.target.files[0]));
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
    // Вкладка живёт в адресе (#actions), чтобы на неё можно было сослаться.
    // После возврата из Keycloak адрес уже без неё — открываются документы.
    selectTab(location.hash === "#actions" ? 1 : 0);
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
    bindTabs();
  }

  // Keycloak возвращает только на адреса, зарегистрированные в realm, а они
  // построены от публичного адреса сайта (FNSKIT_SITE_URL). Открой сайт под
  // другим именем — 127.0.0.1 вместо localhost, второй домен — и вход
  // получил бы 400. Публичный адрес знает сам Keycloak: это origin издателя.
  async function canonicalOrigin() {
    const response = await fetch(`${AUTH_URL}/realms/${REALM}/.well-known/openid-configuration`, {
      credentials: "omit",
      cache: "no-store"
    });
    if (!response.ok) throw new Error(`discovery: ${response.status}`);
    const { issuer } = await response.json();
    return new URL(issuer).origin;
  }

  // keycloak-js ждёт ответа скрытого фрейма без ограничения по времени. Если
  // Keycloak вместо возврата на сайт покажет во фрейме ошибку, страница
  // навсегда осталась бы на «Проверяем вход…».
  function withTimeout(promise, ms) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms))
    ]);
  }

  async function start() {
    let Keycloak;
    let origin;
    try {
      [{ default: Keycloak }, origin] = await Promise.all([import(KEYCLOAK_MODULE), canonicalOrigin()]);
    } catch {
      showError();
      return;
    }
    if (origin !== location.origin) {
      location.replace(`${origin}${location.pathname}${location.search}`);
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
      authenticated = await withTimeout(keycloak.init({
        onLoad: "check-sso",
        silentCheckSsoRedirectUri: `${location.origin}/silent-check-sso`,
        redirectUri: RETURN_URL,
        pkceMethod: "S256",
        // Опрос сессии через iframe раз в 5 секунд не нужен: токен и так
        // обновляется перед каждым запросом.
        checkLoginIframe: false
      }), 15000);
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
