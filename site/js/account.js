// Кабинет компании.
//
// Вход — через Keycloak на том же домене (/auth/): keycloak-js получает токены
// по коду авторизации с PKCE и держит их только в памяти страницы. Профиль,
// набор документов, файлы и сроки отдаёт cabinet API (/api/cabinet), статус
// двухфакторной защиты — Account API самого Keycloak. Подключение и отключение
// 2FA, смену пароля и данных выполняет Keycloak на своих страницах («действия
// по запросу приложения», kc_action), после чего возвращает пользователя сюда.
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

  // Кнопки и поля: одна форма («таблетка») на всё, вид задаёт src/tailwind.css.
  const BTN_PRIMARY = "btn btn-sm btn-primary";
  const BTN_SECONDARY = "btn btn-sm btn-secondary";
  const BTN_GHOST = "btn btn-sm btn-ghost";
  const BTN_DANGER = "btn btn-sm btn-danger";
  const BTN_ON_DARK = "btn btn-sm btn-on-dark";

  const CHIP_TONES = {
    ok: "chip bg-paper text-ink/60",
    soon: "chip bg-lime/60 text-ink",
    urgent: "chip bg-[#FFDFA8] text-ink",
    expired: "chip bg-red-50 text-red-700"
  };

  let keycloak = null;
  let pendingAction = null;

  function showState(id) {
    for (const state of STATES) $(state).classList.toggle("hidden", state !== id);
    const signedIn = id === "stateUser";
    $("logoutButton").classList.toggle("hidden", !signedIn);
    $("headerHome").classList.toggle("hidden", signedIn);
  }

  function showMessage(node, text, tone) {
    node.textContent = text;
    node.className = tone === "error"
      ? "mt-6 rounded-2xl bg-red-50 px-5 py-3 text-sm font-bold text-red-700"
      : tone === "neutral"
        ? "mt-6 rounded-2xl border border-ink/10 bg-white px-5 py-3 text-sm font-bold text-ink/70"
        : "mt-6 rounded-2xl bg-lime px-5 py-3 text-sm font-bold text-ink";
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

  // Ошибка действия: сессия истекла — на экран входа, иначе текст.
  function reportFileError(error, fallback) {
    if (error instanceof SessionExpired) {
      showGuest("Сессия истекла. Войдите снова.");
      return;
    }
    showMessage($("userMessage"), error.message || fallback, "error");
  }

  // --- Состояние и форматирование -------------------------------------------

  const cabinet = { profile: null, groups: [], files: [], usage: null, reminders: null };
  // Документ, для которого открыт выбор файла; файл с открытой панелью ссылок
  // и файл, у которого правится дата.
  let uploadKind = null;
  let sharingFileId = null;
  let editingDateFileId = null;
  let showSituational = false;
  // Только что созданная ссылка: её адрес показываем один раз.
  let createdLink = null;

  const bytesFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
  const dayMonth = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" });
  const fullDate = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" });

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} Б`;
    if (bytes < 1024 * 1024) return `${bytesFormat.format(bytes / 1024)} КБ`;
    return `${bytesFormat.format(bytes / (1024 * 1024))} МБ`;
  }

  // «2026-10-27» без сдвига часового пояса.
  function parseDay(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(year, month - 1, day);
  }

  function pluralDays(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return `${count} день`;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} дня`;
    return `${count} дней`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, className, onClick) {
    const node = element("button", className, label);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  // Состояние срока файла: цвет плашки и текст. Без даты — null.
  function deadlineStatus(file, doc) {
    if (!file.doc_date || file.days_left === null || !doc || !doc.date_kind) return null;
    const left = file.days_left;
    const tone = left < 0 ? "expired" : left <= 7 ? "urgent" : left <= 30 ? "soon" : "ok";
    let text;
    if (doc.date_kind === "issued") {
      const issued = `Выписка от ${dayMonth.format(parseDay(file.doc_date))}`;
      text = left < 0
        ? `${issued} · устарела`
        : left === 0 ? `${issued} · сегодня последний день` : `${issued} · свежая ещё ${pluralDays(left)}`;
    } else {
      const due = fullDate.format(parseDay(file.due_date));
      text = left < 0
        ? `Срок закончился ${due}`
        : left === 0 ? "Срок заканчивается сегодня" : `До ${due} · ${pluralDays(left)}`;
    }
    return { tone, text };
  }

  function docsById() {
    const map = new Map();
    for (const group of cabinet.groups) for (const doc of group.documents) map.set(doc.id, doc);
    return map;
  }

  // --- Профиль ---------------------------------------------------------------

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

  // --- Ссылки на файл --------------------------------------------------------

  function createdLinkBlock(link) {
    const block = element("div", "mt-4");
    const field = element("input", "field field-sm");
    field.readOnly = true;
    field.value = link.url;
    field.setAttribute("aria-label", "Ссылка на файл");
    field.addEventListener("focus", () => field.select());
    const copy = button("Скопировать", BTN_PRIMARY, async () => {
      try {
        await navigator.clipboard.writeText(link.url);
        copy.textContent = "Скопировано";
      } catch {
        field.focus();
      }
    });
    const row = element("div", "flex flex-col gap-2 sm:flex-row sm:items-center");
    field.className = "field field-sm min-w-0 flex-1";
    row.append(field, copy);
    block.append(
      row,
      element(
        "p",
        "mt-2 text-xs font-semibold leading-relaxed text-ink/60",
        `Действует до ${dayMonth.format(new Date(link.expires_at))}. Сохраните её сейчас: показать ещё раз нельзя, только создать новую.`
      )
    );
    return block;
  }

  function sharePanel(file) {
    const panel = element("div", "mt-4 rounded-3xl bg-[#DDD9FF] p-5");
    panel.append(element(
      "p",
      "text-xs font-semibold leading-relaxed text-ink/70",
      "Любой, у кого есть ссылка, сможет скачать файл без входа, пока она действует. Отозвать её можно в любой момент."
    ));

    const row = element("div", "mt-4 flex flex-wrap items-center gap-2");
    const days = element("select", "field field-sm w-auto");
    days.setAttribute("aria-label", "Срок действия ссылки");
    for (const [value, label] of [["1", "на 1 день"], ["7", "на 7 дней"], ["30", "на 30 дней"]]) {
      const option = element("option", "", label);
      option.value = value;
      option.selected = value === "7";
      days.append(option);
    }
    const create = button("Создать ссылку", BTN_PRIMARY, async () => {
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
      const list = element("ul", "mt-4 space-y-2");
      for (const link of file.links) {
        const item = element("li", "flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-white/70 py-1.5 pl-4 pr-1.5 text-xs font-semibold");
        const downloads = link.download_count ? ` · скачиваний: ${link.download_count}` : " · ещё не скачивали";
        item.append(element("span", "", `Ссылка до ${dayMonth.format(new Date(link.expires_at))}${downloads}`));
        item.append(button("Отозвать", BTN_DANGER, async () => {
          try {
            const response = await authorizedFetch(`${CABINET_API}/v1/links/${link.id}`, { method: "DELETE" });
            if (!response.ok && response.status !== 404) throw await failure(response, "Не удалось отозвать ссылку.");
            if (createdLink && createdLink.link.id === link.id) createdLink = null;
            showMessage($("userMessage"), "Ссылка отозвана: по ней больше нельзя скачать файл.", "neutral");
            await refreshFiles();
          } catch (error) {
            reportFileError(error, "Не удалось отозвать ссылку.");
          }
        }));
        list.append(item);
      }
      panel.append(list);
    }
    return panel;
  }

  // --- Срок документа ----------------------------------------------------------

  async function saveDate(file, value) {
    try {
      const response = await authorizedFetch(`${CABINET_API}/v1/documents/${file.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ doc_date: value || null })
      });
      if (!response.ok) throw await failure(response, "Не удалось сохранить дату.");
      editingDateFileId = null;
      showMessage(
        $("userMessage"),
        value
          ? ((cabinet.reminders && cabinet.reminders.available && cabinet.reminders.email_reminders)
            ? "Дата сохранена. Напомним о сроке письмом."
            : "Дата сохранена.")
          : "Дата убрана.",
        "success"
      );
      await refreshFiles();
    } catch (error) {
      reportFileError(error, "Не удалось сохранить дату.");
    }
  }

  function dateEditor(file, doc) {
    const wrap = element("div", "mt-3 flex flex-wrap items-center gap-2");
    wrap.append(element("label", "w-full text-xs font-extrabold text-ink/55", doc.date_label));
    const input = element("input", "field field-sm w-auto");
    input.type = "date";
    input.value = file.doc_date || "";
    input.min = "2000-01-01";
    input.max = "2100-12-31";
    input.setAttribute("aria-label", doc.date_label);
    wrap.append(
      input,
      button("Сохранить", BTN_PRIMARY, () => {
        if (!input.value) {
          input.focus();
          return;
        }
        saveDate(file, input.value);
      }),
      button("Отмена", BTN_GHOST, () => {
        editingDateFileId = null;
        renderDocuments();
      })
    );
    if (file.doc_date) wrap.append(button("Убрать дату", BTN_DANGER, () => saveDate(file, null)));
    return wrap;
  }

  function dateLine(file, doc) {
    if (editingDateFileId === file.id) return dateEditor(file, doc);
    const line = element("div", "mt-3 flex flex-wrap items-center gap-2");
    const status = deadlineStatus(file, doc);
    if (status) {
      line.append(
        element("span", CHIP_TONES[status.tone], status.text),
        button("Изменить", BTN_GHOST, () => {
          editingDateFileId = file.id;
          renderDocuments();
        })
      );
    } else {
      line.append(button(
        doc.date_kind === "issued" ? "Указать дату выписки" : "Указать срок действия",
        BTN_GHOST,
        () => {
          editingDateFileId = file.id;
          renderDocuments();
        }
      ));
    }
    return line;
  }

  // --- Документы ---------------------------------------------------------------

  function fileRow(file, doc) {
    const row = element("li", "rounded-3xl bg-paper px-4 py-4 sm:px-5");
    const head = element("div", "flex flex-wrap items-center justify-between gap-3");
    const info = element("div", "min-w-0");
    info.append(element("p", "truncate text-sm font-extrabold", file.filename));
    const links = file.links.length ? ` · ссылок: ${file.links.length}` : "";
    info.append(element(
      "p",
      "mt-0.5 text-xs font-semibold text-ink/45",
      `${formatBytes(file.size_bytes)} · загружен ${dayMonth.format(new Date(file.created_at))}${links}`
    ));

    const actions = element("div", "flex flex-wrap items-center gap-1.5");
    actions.append(
      button("Скачать", BTN_SECONDARY, () => downloadFile(file)),
      button(sharingFileId === file.id ? "Скрыть" : "Поделиться", BTN_SECONDARY, () => {
        sharingFileId = sharingFileId === file.id ? null : file.id;
        createdLink = null;
        renderDocuments();
      }),
      button("Удалить", BTN_DANGER, () => deleteFile(file))
    );
    head.append(info, actions);
    row.append(head);
    if (doc && doc.date_kind) row.append(dateLine(file, doc));
    if (sharingFileId === file.id) row.append(sharePanel(file));
    return row;
  }

  function documentRow(doc, files) {
    const item = element("li", "px-5 py-5 sm:px-7");

    const head = element("div", "flex flex-wrap items-start justify-between gap-x-4 gap-y-3");
    const title = element("div", "min-w-0 flex-1");
    const name = element("p", "text-base font-extrabold tracking-[-.02em]", doc.title);
    if (doc.level === "situational") {
      name.append(" ", element("span", "ml-1 align-middle text-[10px] font-extrabold uppercase tracking-[.1em] text-ink/35", "по ситуации"));
    }
    title.append(name);
    if (!files.length) title.append(element("p", "mt-1 text-sm font-medium text-ink/45", "Не загружен"));

    const upload = button(files.length ? "Ещё файл" : "Загрузить", files.length ? BTN_SECONDARY : BTN_PRIMARY, () => chooseFile(doc.id));
    upload.dataset.kind = doc.id;
    head.append(title, upload);
    item.append(head);

    if (files.length) {
      const list = element("ul", "mt-4 space-y-2");
      for (const file of files) list.append(fileRow(file, doc));
      item.append(list);
    }

    // Подробности скрыты: список остаётся лёгким, а «где взять» — в одном клике.
    const details = element("details", "group mt-3");
    details.append(element(
      "summary",
      "inline-flex cursor-pointer list-none items-center gap-1.5 text-xs font-extrabold text-violet [&::-webkit-details-marker]:hidden",
      "Что это и где взять"
    ));
    const body = element("div", "mt-3 max-w-2xl space-y-2 text-sm font-medium leading-relaxed text-ink/60");
    body.append(element("p", "", doc.purpose));
    const source = element("p", "text-xs font-semibold text-ink/50");
    source.append(element("span", "font-extrabold text-ink/70", "Где взять: "), doc.source);
    body.append(source);
    if (doc.note) body.append(element("p", "text-xs font-bold text-violet", doc.note));
    details.append(body);
    item.append(details);
    return item;
  }

  function renderDeadlines() {
    const docs = docsById();
    const dated = cabinet.files
      .filter((file) => file.days_left !== null && file.days_left <= 30)
      .sort((a, b) => a.days_left - b.days_left);
    $("deadlines").classList.toggle("hidden", dated.length === 0);
    const list = $("deadlinesList");
    list.replaceChildren();
    for (const file of dated) {
      const doc = docs.get(file.kind);
      const status = deadlineStatus(file, doc);
      if (!status) continue;
      const row = element("li", "flex flex-wrap items-center justify-between gap-2 rounded-2xl bg-paper px-5 py-3");
      const label = element("div", "min-w-0");
      label.append(
        element("p", "text-sm font-extrabold", doc ? doc.title : file.filename),
        element("p", "truncate text-xs font-semibold text-ink/45", file.filename)
      );
      row.append(label, element("span", CHIP_TONES[status.tone], status.text));
      list.append(row);
    }
  }

  function renderDocuments() {
    const { profile, groups, files, usage } = cabinet;
    const list = $("documentsList");
    list.replaceChildren();
    const hasSet = groups.length > 0;
    $("documentsEmpty").classList.toggle("hidden", hasSet);
    $("documentsProgress").classList.toggle("hidden", !hasSet);
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
    let hidden = 0;
    const shownKinds = new Set();
    for (const group of groups) {
      const visible = [];
      for (const doc of group.documents) {
        const docFiles = byKind.get(doc.id) || [];
        shownKinds.add(doc.id);
        if (doc.level === "basic") {
          basic += 1;
          if (docFiles.length) basicLoaded += 1;
        }
        // Документы «по ситуации» не загромождают список, пока вы их не загрузили.
        if (doc.level === "situational" && !showSituational && !docFiles.length) {
          hidden += 1;
          continue;
        }
        visible.push([doc, docFiles]);
      }
      if (!visible.length) continue;
      const section = element("section");
      section.append(element("h3", "px-2 text-xs font-extrabold uppercase tracking-[.14em] text-ink/40", group.title));
      const items = element("ul", "mt-3 divide-y divide-ink/10 overflow-hidden rounded-[2rem] border border-ink/10 bg-white/90 shadow-[0_8px_24px_rgba(23,24,21,.04)]");
      for (const [doc, docFiles] of visible) items.append(documentRow(doc, docFiles));
      section.append(items);
      list.append(section);
    }

    // Файлы прежней формы бизнеса (был ИП, стало ООО) не пропадают из вида.
    const orphans = files.filter((file) => !shownKinds.has(file.kind));
    if (orphans.length) {
      const section = element("section");
      section.append(element("h3", "px-2 text-xs font-extrabold uppercase tracking-[.14em] text-ink/40", "Из прежнего набора"));
      const items = element("ul", "mt-3 space-y-2 rounded-[2rem] border border-ink/10 bg-white/90 p-4");
      for (const file of orphans) items.append(fileRow(file, null));
      section.append(items);
      list.append(section);
    }

    const toggle = $("situationalToggle");
    const totalSituational = groups.reduce(
      (sum, group) => sum + group.documents.filter((doc) => doc.level === "situational").length,
      0
    );
    toggle.classList.toggle("hidden", !hasSet || totalSituational === 0 || (!showSituational && hidden === 0));
    toggle.textContent = showSituational
      ? "Скрыть документы по ситуации"
      : `Показать документы по ситуации (${hidden})`;

    if (hasSet) {
      $("documentsCount").textContent = `Базовых загружено: ${basicLoaded} из ${basic}`;
      $("documentsBar").style.width = `${basic ? Math.round((basicLoaded / basic) * 100) : 0}%`;
    }
    if (usage) {
      $("storageUsage").classList.remove("hidden");
      $("storageUsage").textContent =
        `Файлы хранятся зашифрованными, подписанные PDF — без изменений. Занято ${formatBytes(usage.size_bytes)} из ${formatBytes(usage.max_size_bytes)}, один файл — до ${formatBytes(usage.max_file_bytes)}.`;
    }
    renderDeadlines();
  }

  // --- Напоминания --------------------------------------------------------------

  function renderReminders() {
    const reminders = cabinet.reminders;
    const card = $("remindersCard");
    card.classList.toggle("hidden", !reminders || !reminders.available);
    if (!reminders || !reminders.available) return;
    const enabled = reminders.email_reminders;
    $("remindersSwitch").setAttribute("aria-checked", String(enabled));
    $("remindersHint").textContent = enabled
      ? `Если у документа указан срок, напишем на ${reminders.email || "вашу почту"} за 30, 7 и 1 день до него и в день окончания.`
      : "Напоминания выключены: срок по-прежнему виден в кабинете, но письма не придут.";
  }

  async function toggleReminders() {
    const next = $("remindersSwitch").getAttribute("aria-checked") !== "true";
    $("remindersSwitch").disabled = true;
    try {
      const response = await authorizedFetch(`${CABINET_API}/v1/reminders`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email_reminders: next })
      });
      if (!response.ok) throw await failure(response, "Не удалось изменить настройку.");
      cabinet.reminders = await response.json();
      renderReminders();
    } catch (error) {
      reportFileError(error, "Не удалось изменить настройку.");
    } finally {
      $("remindersSwitch").disabled = false;
    }
  }

  // --- Загрузка данных -----------------------------------------------------------

  async function loadFiles() {
    const response = await authorizedFetch(`${CABINET_API}/v1/documents`);
    if (!response.ok) throw await failure(response, "Не удалось получить список файлов.");
    const data = await response.json();
    cabinet.files = data.files;
    cabinet.usage = data.usage;
    cabinet.reminders = data.reminders;
  }

  async function refreshFiles() {
    await loadFiles();
    renderDocuments();
    renderReminders();
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
    renderReminders();
  }

  // --- Загрузка, скачивание, удаление ----------------------------------------------

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
    const trigger = document.querySelector(`button[data-kind="${CSS.escape(kind)}"]`);
    if (trigger) {
      trigger.disabled = true;
      trigger.textContent = "Загружаем…";
    }
    try {
      const query = new URLSearchParams({ kind, filename: file.name });
      const response = await authorizedFetch(`${CABINET_API}/v1/documents?${query}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file
      });
      if (!response.ok) throw await failure(response, "Не удалось загрузить файл.");
      const stored = await response.json();
      // Документу со сроком сразу предлагаем указать дату — из этого и
      // складываются напоминания.
      const doc = docsById().get(kind);
      if (doc && doc.date_kind) editingDateFileId = stored.id;
      showMessage(
        $("userMessage"),
        doc && doc.date_kind
          ? `Файл «${file.name}» загружен. Укажите дату — напомним о сроке.`
          : `Файл «${file.name}» загружен.`,
        "success"
      );
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
      if (editingDateFileId === file.id) editingDateFileId = null;
      showMessage($("userMessage"), `Файл «${file.filename}» удалён.`, "neutral");
      await refreshFiles();
    } catch (error) {
      reportFileError(error, "Не удалось удалить файл.");
    }
  }

  // --- Двухфакторная защита ---------------------------------------------------------

  function otpDeviceItem(credential) {
    const item = element("li", "flex items-center justify-between gap-3 rounded-2xl bg-white/10 py-2 pl-5 pr-2");
    const label = element("div", "min-w-0");
    label.append(element("p", "truncate text-sm font-extrabold", credential.userLabel || "Приложение-аутентификатор"));
    if (credential.createdDate) {
      label.append(element(
        "p",
        "text-xs font-semibold text-white/50",
        `Подключено ${new Date(credential.createdDate).toLocaleDateString("ru-RU")}`
      ));
    }
    item.append(label, button("Отключить", BTN_ON_DARK, () => runAction(`delete_credential:${credential.id}`)));
    return item;
  }

  function renderOtp(credentials) {
    const enabled = credentials.length > 0;
    const status = $("otpStatus");
    status.textContent = enabled ? "Включена" : "Выключена";
    status.className = enabled ? "chip bg-lime text-ink" : "chip bg-white/10 text-white";
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

  // --- Действия Keycloak --------------------------------------------------------------

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

  // --- Вкладки --------------------------------------------------------------------------

  const TABS = [
    { tab: "tabDocuments", panel: "panelDocuments", hash: "#documents" },
    { tab: "tabActions", panel: "panelActions", hash: "#actions" },
    { tab: "tabAccount", panel: "panelAccount", hash: "#account" }
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

  function tabFromHash() {
    const index = TABS.findIndex((entry) => entry.hash === location.hash);
    return index === -1 ? 0 : index;
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

  // --- Запуск ----------------------------------------------------------------------------

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
    // Вернулись из Keycloak после действия с аккаунтом — показываем результат
    // там, где он виден: на вкладке «Аккаунт».
    selectTab(pendingAction ? 2 : tabFromHash());
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
    $("remindersSwitch").addEventListener("click", toggleReminders);
    $("situationalToggle").addEventListener("click", () => {
      showSituational = !showSituational;
      renderDocuments();
    });
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
      location.replace(`${origin}${location.pathname}${location.search}${location.hash}`);
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
