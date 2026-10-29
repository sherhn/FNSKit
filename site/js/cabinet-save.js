// «Сохранить в кабинет» в редакторе: готовый TIFF или PDF уходит в кабинет
// компании, в выбранный документ набора или в «Другие документы».
//
// Вход — тот же Keycloak, что и в кабинете (keycloak-js, код авторизации с
// PKCE), но клиент входа загружается только по нажатию кнопки: просто
// открытый редактор в Keycloak не ходит. Если пользователь не вошёл, файл
// ждёт в IndexedDB этого браузера, пока он входит: Keycloak возвращает на ту
// же страницу редактора, и сохранение продолжается с того же места. Запись
// удаляется после сохранения, отмены или через полчаса.
(() => {
  const AUTH_URL = `${location.origin}/auth`;
  const REALM = "fnskit";
  const CLIENT_ID = "fnskit-web";
  const KEYCLOAK_MODULE = "/vendor/keycloak-js-26.2.4.mjs";
  const CABINET_API = "/api/cabinet";
  const PAGE_URL = `${location.origin}${location.pathname}`;
  const PENDING_DATABASE = "fnskit-cabinet-save";
  const PENDING_STORE = "pending";
  const PENDING_ID = "result";
  const PENDING_MAX_AGE = 30 * 60 * 1000;
  const DEFAULT_KIND = "other";
  // Кабинет принимает только то, что может отдать байт в байт и проверить по
  // первым байтам; ZIP со страницами в их число не входит.
  const SAVABLE_FORMATS = new Set(["TIFF", "PDF", "PDF/A"]);

  // Кнопки и список — как у остальных элементов боковой панели редактора.
  const BTN = "inline-flex items-center justify-center rounded-xl px-4 py-3 text-sm font-extrabold leading-none transition disabled:cursor-not-allowed disabled:opacity-40";

  let keycloak = null;
  let current = null; // { file, filename }
  let block = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatBytes(bytes) {
    const format = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
    if (bytes < 1024 * 1024) return `${format.format(bytes / 1024)} КБ`;
    return `${format.format(bytes / (1024 * 1024))} МБ`;
  }

  // --- Отложенный файл на время входа ----------------------------------------

  function openPending() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(PENDING_DATABASE, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(PENDING_STORE, { keyPath: "id" });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function pendingTransaction(mode, action) {
    const database = await openPending();
    try {
      return await new Promise((resolve, reject) => {
        const transaction = database.transaction(PENDING_STORE, mode);
        const request = action(transaction.objectStore(PENDING_STORE));
        transaction.oncomplete = () => resolve(request && request.result);
        transaction.onerror = () => reject(transaction.error);
      });
    } finally {
      database.close();
    }
  }

  const stashResult = (file, filename) =>
    pendingTransaction("readwrite", (store) => store.put({ id: PENDING_ID, file, filename, page: location.pathname, createdAt: Date.now() }));
  const readPending = () => pendingTransaction("readonly", (store) => store.get(PENDING_ID));
  const dropPending = () => pendingTransaction("readwrite", (store) => store.delete(PENDING_ID)).catch(() => {});

  // --- Вход -------------------------------------------------------------------

  function withTimeout(promise, ms) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms))
    ]);
  }

  // Keycloak возвращает только на адреса из настроек клиента, а они построены
  // от публичного адреса сайта. Под другим именем (127.0.0.1 вместо
  // localhost) вход не сработает — это и проверяем заранее.
  async function canonicalOrigin() {
    const response = await fetch(`${AUTH_URL}/realms/${REALM}/.well-known/openid-configuration`, {
      credentials: "omit",
      cache: "no-store"
    });
    if (!response.ok) throw new Error(`discovery: ${response.status}`);
    const { issuer } = await response.json();
    return new URL(issuer).origin;
  }

  async function signedIn() {
    if (keycloak) return keycloak.authenticated === true;
    const [{ default: Keycloak }, origin] = await Promise.all([import(KEYCLOAK_MODULE), canonicalOrigin()]);
    if (origin !== location.origin) {
      throw new Error(`Сохранять в кабинет можно, открыв редактор по адресу ${origin}.`);
    }
    keycloak = new Keycloak({ url: AUTH_URL, realm: REALM, clientId: CLIENT_ID });
    return withTimeout(
      keycloak.init({
        onLoad: "check-sso",
        silentCheckSsoRedirectUri: `${location.origin}/silent-check-sso`,
        redirectUri: PAGE_URL,
        pkceMethod: "S256",
        checkLoginIframe: false
      }),
      15000
    );
  }

  class SessionExpired extends Error {}

  async function cabinetFetch(path, options = {}) {
    try {
      await keycloak.updateToken(30);
    } catch {
      throw new SessionExpired();
    }
    const response = await fetch(`${CABINET_API}${path}`, {
      ...options,
      headers: { Accept: "application/json", ...options.headers, Authorization: `Bearer ${keycloak.token}` },
      credentials: "omit",
      cache: "no-store"
    });
    if (response.status === 401) throw new SessionExpired();
    return response;
  }

  async function failure(response, fallback) {
    try {
      const body = await response.json();
      if (typeof body.detail === "string") return new Error(body.detail);
    } catch {
      // Не JSON — например, страница nginx.
    }
    return new Error(fallback);
  }

  // --- Интерфейс ----------------------------------------------------------------

  function ensureBlock() {
    if (block) return block;
    const anchor = document.getElementById("createMessage");
    if (!anchor) return null;
    const root = element("div", "mt-3 hidden rounded-xl border border-white/10 bg-white/5 p-3");
    root.id = "cabinetSave";
    const body = element("div");
    const status = element("p", "mt-2 hidden text-[11px] font-semibold leading-relaxed", "");
    status.setAttribute("aria-live", "polite");
    root.append(body, status);
    anchor.after(root);
    block = { root, body, status };
    return block;
  }

  function setStatus(text, tone) {
    const { status } = ensureBlock();
    status.replaceChildren(text);
    status.className = `mt-2 text-[11px] font-semibold leading-relaxed ${
      tone === "error" ? "text-red-300" : tone === "success" ? "text-lime" : "text-white/55"
    }`;
    status.classList.toggle("hidden", !text);
  }

  function showOffer() {
    const { root, body } = ensureBlock();
    const save = element("button", `${BTN} w-full justify-between bg-white text-ink hover:bg-lime`, "Сохранить в кабинет");
    const chevron = element("span", "action-chevron action-chevron--centered", ">");
    chevron.setAttribute("aria-hidden", "true");
    save.append(chevron);
    save.type = "button";
    save.addEventListener("click", () => startSaving(save));
    body.replaceChildren(
      save,
      element("p", "mt-2 text-[10px] font-semibold leading-relaxed text-white/40", "Файл будет храниться в кабинете компании зашифрованным — его можно скачать, отправить ссылкой или положить в пакет.")
    );
    setStatus("");
    root.classList.remove("hidden");
  }

  async function startSaving(trigger) {
    trigger.disabled = true;
    setStatus("Проверяем вход…");
    try {
      if (!(await signedIn())) {
        await stashResult(current.file, current.filename);
        setStatus("Открываем вход в аккаунт…");
        await keycloak.login({ redirectUri: PAGE_URL });
        return;
      }
      await showPicker();
    } catch (error) {
      trigger.disabled = false;
      setStatus(error.message === "timeout" ? "Сервис входа не ответил. Попробуйте ещё раз." : error.message || "Не удалось проверить вход.", "error");
    }
  }

  async function showPicker() {
    setStatus("Загружаем документы кабинета…");
    const [meResponse, filesResponse] = await Promise.all([cabinetFetch("/v1/me"), cabinetFetch("/v1/documents")]);
    if (!meResponse.ok) throw await failure(meResponse, "Кабинет не ответил. Попробуйте через пару минут.");
    if (!filesResponse.ok) throw await failure(filesResponse, "Кабинет не ответил. Попробуйте через пару минут.");
    const me = await meResponse.json();
    const { usage } = await filesResponse.json();
    const { body } = ensureBlock();

    if (!me.document_groups.length) {
      const link = element("a", "font-extrabold text-lime underline underline-offset-2", "Укажите её в кабинете");
      link.href = "/account#account";
      setStatus("");
      body.replaceChildren(element("p", "text-[11px] font-semibold leading-relaxed text-white/70", "Чтобы сохранять файлы, в профиле нужна форма бизнеса — ИП или ООО. "));
      body.firstChild.append(link, ".");
      return;
    }
    if (current.file.size > usage.max_file_bytes) {
      setStatus(`Файл ${formatBytes(current.file.size)} — больше ${formatBytes(usage.max_file_bytes)}, столько кабинет не хранит. Уменьшите разрешение или разбейте документ.`, "error");
      body.replaceChildren();
      return;
    }

    const label = element("label", "block text-[10px] font-extrabold uppercase tracking-[.12em] text-white/40", "Куда положить");
    label.htmlFor = "cabinetSaveKind";
    const select = element("select", "custom-select mt-2 w-full rounded-xl border border-white/10 bg-white px-4 py-3 text-sm font-extrabold text-ink outline-none transition focus:border-violet focus:ring-4 focus:ring-violet/20");
    select.id = "cabinetSaveKind";
    for (const group of me.document_groups) {
      const optgroup = element("optgroup");
      optgroup.label = group.title;
      for (const doc of group.documents) {
        const option = element("option", "", doc.title);
        option.value = doc.id;
        option.selected = doc.id === DEFAULT_KIND;
        optgroup.append(option);
      }
      select.append(optgroup);
    }
    const save = element("button", `${BTN} bg-lime text-ink hover:bg-white`, "Сохранить");
    save.type = "button";
    const cancel = element("button", `${BTN} text-white/60 hover:text-white`, "Отмена");
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      dropPending();
      showOffer();
    });
    save.addEventListener("click", () => upload(select.value, [save, cancel, select]));
    const actions = element("div", "mt-3 flex flex-wrap gap-2");
    actions.append(save, cancel);
    body.replaceChildren(
      element("p", "text-[11px] font-extrabold text-white", current.filename),
      element("p", "mt-0.5 text-[10px] font-semibold text-white/40", `${formatBytes(current.file.size)} · в кабинете занято ${formatBytes(usage.size_bytes)} из ${formatBytes(usage.max_size_bytes)}`),
      element("div", "mt-3"),
      actions
    );
    body.children[2].append(label, select);
    setStatus("");
  }

  async function upload(kind, controls) {
    controls.forEach((control) => { control.disabled = true; });
    setStatus("Сохраняем…");
    try {
      const query = new URLSearchParams({ kind, filename: current.filename });
      const response = await cabinetFetch(`/v1/documents?${query}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: current.file
      });
      if (!response.ok) throw await failure(response, "Не удалось сохранить файл.");
      await dropPending();
      const { body } = ensureBlock();
      const link = element("a", "font-extrabold text-lime underline underline-offset-2", "Открыть кабинет");
      link.href = "/account#documents";
      const done = element("p", "text-[11px] font-semibold leading-relaxed text-white/70", `Файл «${current.filename}» сохранён в кабинете. `);
      done.append(link);
      body.replaceChildren(done);
      setStatus("");
    } catch (error) {
      controls.forEach((control) => { control.disabled = false; });
      if (error instanceof SessionExpired) {
        setStatus("Сессия истекла — нажмите «Сохранить» ещё раз, чтобы войти.", "error");
        keycloak = null;
        showOffer();
        return;
      }
      setStatus(error.message || "Не удалось сохранить файл.", "error");
    }
  }

  // --- Связь с редактором ---------------------------------------------------------

  window.FNSKIT_CABINET_SAVE = {
    // Вызывается после сборки файла; ZIP и прочее в кабинет не сохраняется.
    offer(file, filename, formatLabel) {
      if (!file || !SAVABLE_FORMATS.has(formatLabel) || !ensureBlock()) return;
      current = { file, filename };
      showOffer();
    },
    // Новая сборка или другой файл: прежнее предложение больше не к месту.
    reset() {
      current = null;
      if (block) block.root.classList.add("hidden");
    }
  };

  // Вернулись из Keycloak после входа: файл ждёт в IndexedDB — продолжаем.
  async function resume() {
    let pending;
    try {
      pending = await readPending();
    } catch {
      return;
    }
    if (!pending) return;
    if (Date.now() - pending.createdAt > PENDING_MAX_AGE || pending.page !== location.pathname) {
      if (Date.now() - pending.createdAt > PENDING_MAX_AGE) dropPending();
      return;
    }
    current = { file: pending.file, filename: pending.filename };
    if (!ensureBlock()) return;
    showOffer();
    try {
      if (await signedIn()) await showPicker();
      else setStatus("Вход не завершён. Нажмите «Сохранить в кабинет», чтобы попробовать снова.");
    } catch (error) {
      setStatus(error.message === "timeout" ? "Сервис входа не ответил. Попробуйте ещё раз." : error.message || "Не удалось проверить вход.", "error");
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", resume);
  else resume();
})();
