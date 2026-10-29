// Редактор документов: общий код для editor-dlya-fns, editor-dlya-registracii и
// editor-custom. Страницы отличаются только текстами и профилем в data-profile
// у <html>, логика у них одна.
//
// В работе может быть сразу несколько будущих файлов («вкладки файлов»): у каждой
// вкладки свой список страниц и своё имя результата. Исходные файлы лежат в
// общем пуле — один и тот же скан можно разложить по разным результатам, а браузер
// прочитает и сохранит его только один раз.
(() => {
  const profiles = {
    vat: {
      title: "НДС и требования ФНС",
      badge: "8 бит · Серый",
      dpi: "300 DPI",
      color: "Серый",
      compression: "LZW",
      background: "#DDD9FF"
    },
    registration: {
      title: "Регистрация ООО / ИП",
      badge: "1 бит · Ч/б",
      dpi: "300 DPI",
      color: "Ч/б",
      compression: "CCITT G4",
      background: "#C8FF32"
    },
    custom: {
      title: "Кастомный TIFF",
      badge: "8 бит · Серый",
      dpi: "300 DPI",
      color: "Серый",
      compression: "LZW",
      background: "#FFDFA8"
    }
  };
  const allowedExtensions = new Set(["pdf", "jpg", "jpeg", "png", "tif", "tiff"]);
  // Те же пределы, что и у процессора (MAX_TOTAL_BYTES, MAX_PAGES): считаем их
  // здесь, чтобы не гонять заведомо отбитый запрос.
  // Размер — на все вкладки сразу, страницы — на каждый итоговый файл отдельно.
  const maxTotalSize = 100 * 1024 * 1024;
  const maxPagesPerFile = 200;
  const maxEditorFiles = 10;
  const fileStoreDatabase = "fnskit-editor";
  const fileStoreName = "documents";
  const fileStoreRecordId = "selected-files";
  const processorApiBase = window.FNSKIT_PROCESSOR_API || "http://localhost:8080";
  const profilePages = {
    vat: "/editor-dlya-fns",
    registration: "/editor-dlya-registracii",
    custom: "/editor-custom"
  };
  const profileButtons = [...document.querySelectorAll(".profile-button")];
  const profileSummary = document.getElementById("profileSummary");
  const profileTitle = document.getElementById("profileTitle");
  const profileBadge = document.getElementById("profileBadge");
  const profileDpi = document.getElementById("profileDpi");
  const profileColor = document.getElementById("profileColor");
  const profileCompression = document.getElementById("profileCompression");
  const customSettings = document.getElementById("customSettings");
  const customSettingsGrid = document.getElementById("customSettingsGrid");
  const customSettingsRow2 = document.getElementById("customSettingsRow2");
  const customPreset = document.getElementById("customPreset");
  const customFormat = document.getElementById("customFormat");
  const customDpi = document.getElementById("customDpi");
  const customColor = document.getElementById("customColor");
  const customCompressionLabel = document.getElementById("customCompressionLabel");
  const customCompression = document.getElementById("customCompression");
  const customQualityContainer = document.getElementById("customQualityContainer");
  const customQualityBadge = document.getElementById("customQualityBadge");
  const customQuality = document.getElementById("customQuality");
  const customTargetMbContainer = document.getElementById("customTargetMbContainer");
  const customTargetMbBadge = document.getElementById("customTargetMbBadge");
  const customTargetMb = document.getElementById("customTargetMb");
  const customThresholdContainer = document.getElementById("customThresholdContainer");
  const customThresholdValue = document.getElementById("customThresholdValue");
  const customThreshold = document.getElementById("customThreshold");
  const thresholdMinus = document.getElementById("thresholdMinus");
  const thresholdPlus = document.getElementById("thresholdPlus");
  const customSettingsHint = document.getElementById("customSettingsHint");
  const rotateAllButton = document.getElementById("rotateAllButton");

  const customPresets = {
    bank: {
      format: "pdf",
      dpi: "300",
      color: "color",
      compression: "jpeg",
      quality: "85",
      targetMb: "15"
    },
    fns: {
      format: "tiff",
      dpi: "300",
      color: "bw",
      compression: "g4",
      quality: "85",
      targetMb: "0",
      threshold: "180"
    },
    court: {
      format: "pdfa",
      dpi: "300",
      color: "bw",
      compression: "jpeg",
      quality: "85",
      targetMb: "30",
      threshold: "180"
    },
    registration: {
      format: "tiff",
      dpi: "300",
      color: "bw",
      compression: "lzw",
      quality: "85",
      targetMb: "0",
      threshold: "180"
    }
  };
  const fileInput = document.getElementById("fileInput");
  const dropzone = document.getElementById("dropzone");
  const uploadStatus = document.getElementById("uploadStatus");
  const uploadStatusText = document.getElementById("uploadStatusText");
  const fileError = document.getElementById("fileError");
  const fileTabs = document.getElementById("fileTabs");
  const fileTabsSelect = document.getElementById("fileTabsSelect");
  const addFileTab = document.getElementById("addFileTab");
  const filesPanel = document.getElementById("filesPanel");
  const fileList = document.getElementById("fileList");
  const fileCountTop = document.getElementById("fileCountTop");
  const activeFileTitle = document.getElementById("activeFileTitle");
  const addMoreButton = document.getElementById("addMoreButton");
  const clearFiles = document.getElementById("clearFiles");
  const summaryFiles = document.getElementById("summaryFiles");
  const summarySize = document.getElementById("summarySize");
  const summaryTotals = document.getElementById("summaryTotals");
  const resultFilename = document.getElementById("resultFilename");
  const resultFilenameHint = document.getElementById("resultFilenameHint");
  const outputFormatSelect = document.getElementById("outputFormat");
  const outputFormatHint = document.getElementById("outputFormatHint");
  const createButton = document.getElementById("createButton");
  const createButtonLabel = createButton.querySelector("span");
  const createHint = document.getElementById("createHint");
  const createMessage = document.getElementById("createMessage");
  const pagePreviewModal = document.getElementById("pagePreviewModal");
  const pagePreviewTitle = document.getElementById("pagePreviewTitle");
  const pagePreviewImage = document.getElementById("pagePreviewImage");
  const pagePreviewViewport = document.getElementById("pagePreviewViewport");
  const pagePreviewLoading = document.getElementById("pagePreviewLoading");
  const pagePreviewLoadingText = document.getElementById("pagePreviewLoadingText");
  const pagePreviewZoomOut = document.getElementById("pagePreviewZoomOut");
  const pagePreviewZoomFit = document.getElementById("pagePreviewZoomFit");
  const pagePreviewZoomValue = document.getElementById("pagePreviewZoomValue");
  const pagePreviewZoomIn = document.getElementById("pagePreviewZoomIn");
  const closePagePreviewButton = document.querySelector("[data-page-preview-close]");
  const createModal = document.getElementById("createModal");
  const createModalTarget = document.getElementById("createModalTarget");
  const consentCheckbox = document.getElementById("consentCheckbox");
  const modalCreateError = document.getElementById("modalCreateError");
  const confirmCreateButton = document.getElementById("confirmCreateButton");
  const closeCreateModalButton = document.querySelector("[data-create-modal-close]");
  const createMessageHiddenClass = "mt-3 hidden rounded-xl px-4 py-3 text-xs font-bold leading-relaxed";
  let activeProfile = "vat";
  let sourceDocuments = [];
  let editorFiles = [];
  let activeFileId = null;
  let filesRestored;
  let isReadingFiles = false;
  let isProcessing = false;
  // Версия текста согласия на обработку ПДн (site/consent.html). Сервер сверяет
  // её и без неё файл не готовит, отметка на странице — только чтобы не доходить
  // до этого отказа. Та же строка записана в backend/processor/app/main.py,
  // совпадение проверяет backend/processor/tests/test_consent.py.
  const consentVersion = "2026-10-17.1";
  const previewCache = new Map();
  const fullPreviewCache = new Map();
  const previewRequests = new Map();
  const pdfDocuments = new Map();
  const tiffFrames = new Map();
  const previewZoomSteps = [1, 1.25, 1.5, 2, 3, 4];
  let previewZoomIndex = 0;
  let activePreviewPageId = null;

  function openFileStore() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(fileStoreDatabase, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(fileStoreName)) {
          request.result.createObjectStore(fileStoreName, { keyPath: "id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function saveEditorState() {
    try {
      const database = await openFileStore();
      await new Promise((resolve, reject) => {
        const transaction = database.transaction(fileStoreName, "readwrite");
        transaction.objectStore(fileStoreName).put({
          id: fileStoreRecordId,
          version: 4,
          sources: sourceDocuments,
          files: editorFiles.map(file => ({
            id: file.id,
            name: file.name,
            filenameEdited: file.filenameEdited,
            profile: file.profile,
            outputFormat: file.outputFormat,
            pages: file.pages
          })),
          activeId: activeFileId
        });
        transaction.oncomplete = resolve;
        transaction.onerror = () => reject(transaction.error);
      });
      database.close();
    } catch {
      // Редактор остаётся рабочим, даже если браузер запретил локальное хранилище.
    }
  }

  async function restoreEditorState() {
    try {
      const database = await openFileStore();
      const stored = await new Promise((resolve, reject) => {
        const request = database.transaction(fileStoreName, "readonly").objectStore(fileStoreName).get(fileStoreRecordId);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      const handoff = await new Promise((resolve) => {
        const req = database.transaction(fileStoreName, "readonly").objectStore(fileStoreName).get("check-handoff");
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
      });
      if (handoff?.file instanceof File) {
        await new Promise((resolve) => {
          const tx = database.transaction(fileStoreName, "readwrite");
          tx.objectStore(fileStoreName).delete("check-handoff");
          tx.oncomplete = resolve;
          tx.onerror = resolve;
        });
      }
      database.close();
      if (handoff?.file instanceof File) {
        const targetProfile = profiles[handoff.profile] ? handoff.profile : activeProfile;
        sourceDocuments = [];
        editorFiles = [];
        activeFileId = null;
        ensureEditorFile();
        activeFile().profile = targetProfile;
        if (targetProfile === "vat") {
          activeFile().outputFormat = handoff.file.name.toLowerCase().endsWith(".pdf") ? "pdf" : "tiff";
        } else if (targetProfile === "registration") {
          activeFile().outputFormat = "tiff";
        }
        selectProfile(targetProfile);
        renderEditor();
        setTimeout(() => addFiles([handoff.file]), 50);
        return;
      }
      if ([2, 3, 4].includes(stored?.version) && Array.isArray(stored.files)) {
        sourceDocuments = (stored.sources || []).filter(file => file instanceof File);
        editorFiles = stored.files
          .filter(file => Array.isArray(file?.pages))
          .slice(0, maxEditorFiles)
          .map(file => {
            // В старых сессиях профиль был общим для всего редактора. Оставляем
            // им профиль открытой страницы, а новые вкладки хранят его отдельно.
            const restoredProfile = profiles[file.profile] ? file.profile : activeProfile;
            let restoredFormat = "tiff";
            if (restoredProfile === "custom") {
              restoredFormat = ["tiff", "pdf", "pdfa", "zip"].includes(file.outputFormat) ? file.outputFormat : "tiff";
            } else if (restoredProfile === "vat") {
              restoredFormat = file.outputFormat === "pdf" ? "pdf" : "tiff";
            }
            return {
              id: typeof file.id === "string" && file.id ? file.id : createEditorFileId(),
              name: typeof file.name === "string" && file.name
                ? normalizeResultFilename(file.name, restoredFormat, restoredProfile)
                : defaultResultFilename(restoredProfile, restoredFormat),
              filenameEdited: Boolean(file.filenameEdited),
              profile: restoredProfile,
              outputFormat: restoredFormat,
              pages: file.pages.slice(0, maxPagesPerFile).map(page => ({
                ...page,
                rotation: Number(page.rotation) || 0
              }))
            };
          });
        if (typeof stored.activeId === "string") activeFileId = stored.activeId;
      } else if (Array.isArray(stored?.documents) && Array.isArray(stored?.pages)) {
        // Записи прежнего редактора: один файл без вкладок.
        sourceDocuments = stored.documents.filter(file => file instanceof File);
        editorFiles = [makeEditorFile(stored.pages.slice(0, maxPagesPerFile))];
      } else if (Array.isArray(stored?.files) && stored.files.every(file => file instanceof File)) {
        // Самая старая запись: страницы ещё не выделялись из файлов.
        sourceDocuments = stored.files.slice(0, maxPagesPerFile);
        editorFiles = [makeEditorFile(sourceDocuments.map(file => ({
          id: `${fileKey(file)}:1`, sourceId: fileKey(file), name: file.name, pageNumber: 1, pageTotal: 1, rotation: 0, type: "file"
        })))];
      }
    } catch {
      // При недоступном IndexedDB просто начинаем с пустого списка.
    }
    ensureEditorFile();
    refreshDefaultFilenames();
    dropUnusedSources();
    selectProfile(activeFile().profile);
    renderEditor();
  }

  function formatBytes(bytes) {
    if (!bytes) return "0 Б";
    const units = ["Б", "КБ", "МБ", "ГБ"];
    const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / (1024 ** unit);
    return `${value.toLocaleString("ru-RU", { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
  }

  function baseResultFilename(profile) {
    if (profile === "registration") return "fnskit-registration";
    if (profile === "custom") return "fnskit-custom";
    return "fnskit-fns-nds";
  }

  function extensionForFormat(outputFormat = "tiff") {
    if (outputFormat === "pdf" || outputFormat === "pdfa") return "pdf";
    if (outputFormat === "zip") return "zip";
    return "tiff";
  }

  function defaultResultFilename(profile, outputFormat = "tiff") {
    return `${baseResultFilename(profile)}.${extensionForFormat(outputFormat)}`;
  }

  function uniqueFilename(base, taken, outputFormat = "tiff") {
    const extension = extensionForFormat(outputFormat);
    let candidate = `${base}.${extension}`;
    let index = 1;
    while (taken.has(candidate.toLowerCase())) {
      index += 1;
      candidate = `${base}-${index}.${extension}`;
    }
    return candidate;
  }

  function takenFilenames(exceptFile) {
    return new Set(editorFiles.filter(file => file !== exceptFile).map(file => file.name.toLowerCase()));
  }

  // Имена по умолчанию зависят от профиля и формата конкретной вкладки.
  function refreshDefaultFilenames() {
    editorFiles.forEach(file => {
      if (file.filenameEdited) return;
      file.name = uniqueFilename(
        baseResultFilename(file.profile),
        takenFilenames(file),
        file.outputFormat
      );
    });
  }

  function normalizeResultFilename(value, outputFormat = activeFile().outputFormat, profile = activeFile().profile) {
    const extension = extensionForFormat(outputFormat);
    let filename = String(value)
      .trim()
      .replace(/[<>:"\/\\|?*\u0000-\u001F]/g, "-")
      .replace(/[. ]+$/g, "")
      .replace(/\.(?:tiff?|pdf|zip)$/i, "");
    if (!filename) filename = baseResultFilename(profile);
    return `${filename}.${extension}`;
  }

  function pluralFiles(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return `${count} файл`;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} файла`;
    return `${count} файлов`;
  }

  function pluralPages(count) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return `${count} страница`;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} страницы`;
    return `${count} страниц`;
  }

  function fileKey(file) {
    return `${file.name}:${file.size}:${file.lastModified}`;
  }

  function createEditorFileId() {
    return `tiff-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }

  function makeEditorFile(pages = []) {
    let outputFormat = "tiff";
    if (activeProfile === "vat" && outputFormatSelect?.value === "pdf") {
      outputFormat = "pdf";
    } else if (activeProfile === "custom" && customFormat?.value) {
      outputFormat = customFormat.value;
    }
    return {
      id: createEditorFileId(),
      name: uniqueFilename(baseResultFilename(activeProfile), takenFilenames(null), outputFormat),
      filenameEdited: false,
      profile: activeProfile,
      outputFormat,
      pages: pages.map(p => ({ ...p, rotation: Number(p.rotation) || 0 }))
    };
  }

  function ensureEditorFile() {
    if (!editorFiles.length) editorFiles.push(makeEditorFile());
    if (!editorFiles.some(file => file.id === activeFileId)) activeFileId = editorFiles[0].id;
  }

  function activeFile() {
    return editorFiles.find(file => file.id === activeFileId) || editorFiles[0];
  }

  function activePages() {
    return activeFile().pages;
  }

  function totalPageCount() {
    return editorFiles.reduce((total, file) => total + file.pages.length, 0);
  }

  function poolSize() {
    return sourceDocuments.reduce((total, file) => total + file.size, 0);
  }

  // Исходники активной вкладки в том порядке, в котором они уедут на сервер:
  // индекс в этом списке и есть source_index в page_order.
  function activeSourceFiles() {
    const seen = new Set();
    const files = [];
    activePages().forEach(page => {
      if (seen.has(page.sourceId)) return;
      const file = sourceDocuments.find(item => fileKey(item) === page.sourceId);
      if (!file) return;
      seen.add(page.sourceId);
      files.push(file);
    });
    return files;
  }

  function activeFileSize() {
    return activeSourceFiles().reduce((total, file) => total + file.size, 0);
  }

  // Исходник, который не нужен ни одной вкладке, держать незачем — вместе с ним
  // выбрасываем и его превью, иначе кэши растут после каждого удаления.
  function dropUnusedSources() {
    const used = new Set(editorFiles.flatMap(file => file.pages.map(page => page.sourceId)));
    sourceDocuments = sourceDocuments.filter(file => used.has(fileKey(file)));
    [pdfDocuments, tiffFrames].forEach(cache => {
      [...cache.keys()].forEach(key => {
        if (!used.has(key)) cache.delete(key);
      });
    });
    [previewCache, fullPreviewCache].forEach(cache => {
      [...cache.keys()].forEach(key => {
        if (!used.has(key.slice(0, key.lastIndexOf(":")))) cache.delete(key);
      });
    });
  }

  async function getPdfJs() {
    const pdfjs = await import("/vendor/pdfjs-6.1.200.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs-worker-6.1.200.mjs";
    return pdfjs;
  }

  function escapeHtml(value) {
    return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }

  function sourceFileForPage(page) {
    return sourceDocuments.find(file => fileKey(file) === page.sourceId);
  }

  function canvasDataUrl(canvas) {
    return canvas.toDataURL("image/jpeg", .82);
  }

  // Длинная сторона карточки превью: у PDF карточка получается 190 x 270, здесь
  // чуть чётче. Кадр A4 в 300 DPI — это 2480 x 3508 и около 0,9 МБ JPEG в строке
  // data: URL, которая живёт в кэше превью всё время работы: 200 кадров держали
  // бы около 170 МБ кучи. Уменьшенный кадр весит 20-30 КБ.
  const thumbnailSide = 420;

  // Уменьшение вдвое за шаг, пока не останется не больше двух долей: одним
  // drawImage в 8 раз браузер теряет часть строк, а тонкие линии сканов
  // пропадают.
  function thumbnailCanvas(source) {
    const ratio = thumbnailSide / Math.max(source.width, source.height);
    if (ratio >= 1) return source;
    const width = Math.max(1, Math.round(source.width * ratio));
    const height = Math.max(1, Math.round(source.height * ratio));
    let current = source;
    while (current.width > width * 2 || current.height > height * 2) {
      const half = document.createElement("canvas");
      half.width = Math.max(width, Math.ceil(current.width / 2));
      half.height = Math.max(height, Math.ceil(current.height / 2));
      const context = half.getContext("2d");
      context.imageSmoothingQuality = "high";
      context.drawImage(current, 0, 0, half.width, half.height);
      current = half;
    }
    const result = document.createElement("canvas");
    result.width = width;
    result.height = height;
    const context = result.getContext("2d");
    context.imageSmoothingQuality = "high";
    context.drawImage(current, 0, 0, width, height);
    return result;
  }

  // Кэши хранят обещания, а не готовые значения: превью всех карточек
  // запрашиваются разом, и с готовыми значениями каждая успела бы прочитать и
  // разобрать файл заново, пока первая ещё читает. На PDF в сотню мегабайт и 200
  // страницах это 200 копий файла и 200 воркеров pdf.js.
  function pdfDocumentFor(file, sourceId) {
    let pending = pdfDocuments.get(sourceId);
    if (!pending) {
      pending = (async () => {
        const pdfjs = await getPdfJs();
        return pdfjs.getDocument({
          data: new Uint8Array(await file.arrayBuffer()),
          disableWorker: location.protocol === "file:"
        }).promise;
      })();
      pdfDocuments.set(sourceId, pending);
      // Неудачную загрузку не запоминаем: следующая карточка попробует заново.
      pending.catch(() => {
        if (pdfDocuments.get(sourceId) === pending) pdfDocuments.delete(sourceId);
      });
    }
    return pending;
  }

  function tiffFor(file, sourceId) {
    let pending = tiffFrames.get(sourceId);
    if (!pending) {
      pending = (async () => {
        const buffer = await file.arrayBuffer();
        return { buffer, frames: window.UTIF.decode(buffer) };
      })();
      tiffFrames.set(sourceId, pending);
      pending.catch(() => {
        if (tiffFrames.get(sourceId) === pending) tiffFrames.delete(sourceId);
      });
    }
    return pending;
  }

  // Кадр TIFF на канве. UTIF кладёт пиксели кадра в frame.data и не отпускает их,
  // а кэш хранит все кадры файла: 40 кадров A4 в 300 DPI держали 330 МБ, а 200 —
  // 1,7 ГБ в одной вкладке. Пиксели нужны только на время превью, поэтому после
  // него их отпускаем (при следующем просмотре кадр декодируется заново). RGBA
  // отдаём канве без копии: это тот же буфер, а не ещё 35 МБ на кадр.
  function tiffFrameCanvas(tiff, pageNumber) {
    const frame = tiff.frames[pageNumber - 1];
    window.UTIF.decodeImage(tiff.buffer, frame);
    try {
      const rgba = window.UTIF.toRGBA8(frame);
      const canvas = document.createElement("canvas");
      canvas.width = frame.width;
      canvas.height = frame.height;
      const pixels = new Uint8ClampedArray(rgba.buffer, rgba.byteOffset, rgba.length);
      canvas.getContext("2d").putImageData(new ImageData(pixels, frame.width, frame.height), 0, 0);
      return canvas;
    } finally {
      frame.data = null;
    }
  }

  async function makePagePreview(page) {
    const file = sourceFileForPage(page);
    if (!file) throw new Error("Исходный файл не найден.");
    const extension = file.name.split(".").pop()?.toLowerCase();

    if (extension === "pdf") {
      const pdfDocument = await pdfDocumentFor(file, page.sourceId);
      const pdfPage = await pdfDocument.getPage(page.pageNumber);
      const viewport = pdfPage.getViewport({ scale: .32 });
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(viewport.width));
      canvas.height = Math.max(1, Math.round(viewport.height));
      await pdfPage.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
      return canvasDataUrl(canvas);
    }

    if (extension === "tif" || extension === "tiff") {
      if (!window.UTIF) throw new Error("Не удалось загрузить обработчик TIFF.");
      const tiff = await tiffFor(file, page.sourceId);
      return canvasDataUrl(thumbnailCanvas(tiffFrameCanvas(tiff, page.pageNumber)));
    }

    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("Не удалось прочитать изображение."));
      reader.readAsDataURL(file);
    });
  }

  async function makeFullPagePreview(page) {
    const file = sourceFileForPage(page);
    if (!file) throw new Error("Исходный файл не найден.");
    const extension = file.name.split(".").pop()?.toLowerCase();

    if (extension === "pdf") {
      const pdfDocument = await pdfDocumentFor(file, page.sourceId);
      const pdfPage = await pdfDocument.getPage(page.pageNumber);
      const baseViewport = pdfPage.getViewport({ scale: 1 });
      const renderScale = Math.min(4, Math.max(2, 2400 / baseViewport.width));
      const viewport = pdfPage.getViewport({ scale: renderScale });
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(viewport.width));
      canvas.height = Math.max(1, Math.round(viewport.height));
      await pdfPage.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
      return canvas.toDataURL("image/png");
    }

    if (extension === "tif" || extension === "tiff") {
      if (!window.UTIF) throw new Error("Не удалось загрузить обработчик TIFF.");
      const tiff = await tiffFor(file, page.sourceId);
      return tiffFrameCanvas(tiff, page.pageNumber).toDataURL("image/png");
    }

    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("Не удалось прочитать изображение."));
      reader.readAsDataURL(file);
    });
  }

  function cacheFullPreview(pageId, source) {
    fullPreviewCache.delete(pageId);
    fullPreviewCache.set(pageId, source);
    while (fullPreviewCache.size > 3) {
      fullPreviewCache.delete(fullPreviewCache.keys().next().value);
    }
  }

  function requestPagePreview(page) {
    const card = document.querySelector(`[data-preview-id="${encodeURIComponent(page.id)}"]`);
    const preview = card?.querySelector("[data-page-preview]");
    const transformStyle = page.rotation ? ` style="transform: rotate(${page.rotation}deg); transition: transform 0.2s ease;"` : ' style="transition: transform 0.2s ease;"';
    const cachedSource = previewCache.get(page.id);
    if (cachedSource) {
      if (preview) {
        preview.innerHTML = `<img src="${cachedSource}" alt="Превью: ${escapeHtml(page.name)}, страница ${page.pageNumber}" draggable="false"${transformStyle} />`;
      }
      const openButton = card?.querySelector('[data-file-action="preview"]');
      if (openButton) openButton.disabled = false;
      return;
    }
    if (previewRequests.has(page.id)) return;
    const request = makePagePreview(page)
      .then(source => {
        previewCache.set(page.id, source);
        const card = document.querySelector(`[data-preview-id="${encodeURIComponent(page.id)}"]`);
        const preview = card?.querySelector("[data-page-preview]");
        if (preview) preview.innerHTML = `<img src="${source}" alt="Превью: ${escapeHtml(page.name)}, страница ${page.pageNumber}" draggable="false"${transformStyle} />`;
        const openButton = card?.querySelector('[data-file-action="preview"]');
        if (openButton) openButton.disabled = false;
      })
      .catch(() => {
        const card = document.querySelector(`[data-preview-id="${encodeURIComponent(page.id)}"]`);
        const preview = card?.querySelector("[data-page-preview]");
        if (preview) preview.innerHTML = `<span class="text-[10px] font-extrabold uppercase tracking-[.08em] text-ink/35">Без превью</span>`;
      })
      .finally(() => previewRequests.delete(page.id));
    previewRequests.set(page.id, request);
  }

  async function expandIntoPages(file) {
    const extension = file.name.split(".").pop()?.toLowerCase();
    const sourceId = fileKey(file);
    let pageCount = 1;

    if (extension === "pdf") {
      const pdfjs = await getPdfJs();
      const data = new Uint8Array(await file.arrayBuffer());
      const document = await pdfjs.getDocument({ data, disableWorker: location.protocol === "file:" }).promise;
      pageCount = document.numPages;
    } else if (extension === "tif" || extension === "tiff") {
      if (!window.UTIF) throw new Error("Не удалось загрузить обработчик TIFF.");
      pageCount = window.UTIF.decode(await file.arrayBuffer()).length;
    }

    return Array.from({ length: pageCount }, (_, index) => ({
      id: `${sourceId}:${index + 1}`,
      sourceId,
      name: file.name,
      pageNumber: index + 1,
      pageTotal: pageCount,
      rotation: 0,
      type: extension === "pdf" ? "PDF" : extension === "tif" || extension === "tiff" ? "TIFF" : "Изображение"
    }));
  }

  function selectProfile(profile) {
    const data = profiles[profile];
    if (!data) return;
    activeProfile = profile;
    const file = activeFile();
    if (file) {
      file.profile = profile;
      if (profile === "registration") {
        file.outputFormat = "tiff";
      } else if (profile === "vat") {
        file.outputFormat = file.outputFormat === "pdf" ? "pdf" : "tiff";
      } else if (profile === "custom") {
        if (customFormat?.value) file.outputFormat = customFormat.value;
      }
      file.name = normalizeResultFilename(file.name, file.outputFormat, profile);
    }
    profileButtons.forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.profile === profile));
    });
    profileTitle.textContent = data.title;
    profileBadge.textContent = data.badge;
    profileDpi.textContent = data.dpi;
    profileColor.textContent = data.color;
    profileCompression.textContent = data.compression;
    profileSummary.style.backgroundColor = data.background;
    customSettings.classList.toggle("hidden", profile !== "custom");
    if (profile === "custom") updateCustomSettings();
    refreshDefaultFilenames();
    syncOutputControls();
    const url = new URL(window.location.href);
    url.searchParams.set("profile", profile);
    window.history.replaceState({}, "", url);
  }

  function updateCustomSettings() {
    if (!customDpi || !customColor || !customCompression) return;
    const dpi = Number.parseInt(customDpi.value, 10);
    const color = customColor.value;
    const format = customFormat ? customFormat.value : (activeFile().outputFormat || "tiff");
    activeFile().outputFormat = format;

    const isBw = color === "bw";
    const isTiff = format === "tiff";
    const isPdf = format === "pdf" || format === "pdfa";
    const isZip = format === "zip";

    const g4Option = customCompression.querySelector('option[value="g4"]');
    const lzwOption = customCompression.querySelector('option[value="lzw"]');
    const deflateOption = customCompression.querySelector('option[value="deflate"]');
    const jpegOption = customCompression.querySelector('option[value="jpeg"]');

    if (customCompressionLabel) customCompressionLabel.classList.remove("opacity-50");

    if (isTiff) {
      customCompression.disabled = false;

      if (g4Option) {
        g4Option.disabled = !isBw;
        g4Option.textContent = "CCITT G4 (только ч/б)";
      }
      if (lzwOption) {
        lzwOption.disabled = false;
        lzwOption.textContent = "LZW (без потерь)";
      }
      if (deflateOption) {
        deflateOption.disabled = false;
        deflateOption.textContent = "Deflate / ZIP (без потерь)";
      }
      if (jpegOption) {
        jpegOption.disabled = isBw;
        jpegOption.textContent = isBw ? "JPEG (недоступно для ч/б TIFF)" : "JPEG (для цвета и фото)";
      }

      if (!isBw && customCompression.value === "g4") {
        customCompression.value = "lzw";
        customSettingsHint.textContent = "CCITT G4 доступно только для чёрно-белого TIFF; переключено на LZW.";
      } else if (isBw && customCompression.value === "jpeg") {
        customCompression.value = "g4";
        customSettingsHint.textContent = "Сжатие JPEG недоступно для 1-битного ч/б TIFF; выбрано CCITT G4.";
      } else {
        customSettingsHint.textContent = "Параметры будут применены ко всем страницам итогового документа.";
      }
    } else if (isPdf) {
      customCompression.disabled = false;

      if (g4Option) {
        g4Option.disabled = true;
        g4Option.textContent = "CCITT G4 (только для TIFF)";
      }
      if (lzwOption) {
        lzwOption.disabled = true;
        lzwOption.textContent = "LZW (только для TIFF)";
      }
      if (deflateOption) {
        deflateOption.disabled = true;
        deflateOption.textContent = "Deflate (только для TIFF/ZIP)";
      }
      if (jpegOption) {
        jpegOption.disabled = false;
        jpegOption.textContent = "JPEG (стандарт для PDF)";
      }

      if (customCompression.value !== "jpeg") {
        customCompression.value = "jpeg";
      }
      customSettingsHint.textContent = "Для PDF используется сжатие JPEG. Качество регулируется блоком ниже.";
    } else if (isZip) {
      customCompression.disabled = false;

      if (g4Option) {
        g4Option.disabled = true;
        g4Option.textContent = "CCITT G4 (только для TIFF)";
      }
      if (lzwOption) {
        lzwOption.disabled = true;
        lzwOption.textContent = "LZW (только для TIFF)";
      }
      if (deflateOption) {
        deflateOption.disabled = false;
        deflateOption.textContent = "Deflate (архив ZIP)";
      }
      if (jpegOption) {
        jpegOption.disabled = true;
        jpegOption.textContent = "JPEG (для PDF и TIFF)";
      }

      if (customCompression.value !== "deflate") {
        customCompression.value = "deflate";
      }
      customSettingsHint.textContent = "Страницы упаковываются в ZIP-архив со сжатием Deflate.";
    }

    // 1. Порог бинаризации (Threshold) - зависит от Цветового режима, скрывается если не ч/б
    if (customThresholdContainer) {
      customThresholdContainer.classList.toggle("hidden", !isBw);
      if (customThreshold) customThreshold.disabled = !isBw;
      if (thresholdMinus) thresholdMinus.disabled = !isBw;
      if (thresholdPlus) thresholdPlus.disabled = !isBw;
      if (isBw) {
        const val = Number(customThreshold?.value) || 180;
        let note = "Оптимально";
        if (val < 140) note = "Светлее";
        else if (val > 200) note = "Плотнее";
        if (customThresholdValue) customThresholdValue.textContent = `${val} · ${note}`;
      }
    }

    // 2. Качество сжатия (Quality) - зависит от Сжатия и Формата, скрывается если не JPEG
    const showsQuality = isPdf || (isTiff && customCompression.value === "jpeg") || (isZip && !isBw);
    if (customQuality && (!customQuality.value || !["92", "85", "75", "65"].includes(customQuality.value))) {
      customQuality.value = "85";
    }

    if (customQualityContainer) {
      customQualityContainer.classList.toggle("hidden", !showsQuality);
      if (customQuality) customQuality.disabled = !showsQuality;
      if (showsQuality && customQualityBadge) {
        const qVal = customQuality?.value || "85";
        const qualityLabels = {
          "92": "92% · Максимум",
          "85": "85% · Оптимально",
          "75": "75% · Компакт",
          "65": "65% · Сжатие"
        };
        customQualityBadge.textContent = qualityLabels[qVal] || `${qVal}%`;
      }
    }

    const gridTarget = customSettingsGrid || customSettingsRow2;
    if (gridTarget) {
      const fieldCount = 2 + (isBw ? 1 : 0) + (showsQuality ? 1 : 0);
      gridTarget.dataset.fields = String(fieldCount);
    }

    // 3. Лимит размера файла (Target MB)
    if (customTargetMbContainer) {
      customTargetMbContainer.classList.remove("hidden", "opacity-50", "pointer-events-none");
      if (customTargetMb) customTargetMb.disabled = false;
      if (customTargetMbBadge) {
        const mb = customTargetMb?.value || "0";
        customTargetMbBadge.textContent = mb === "0" ? "Без лимита" : `до ${mb} МБ`;
      }
    }

    const colors = {
      bw: { label: "Ч/б", badge: "1 бит · Ч/б" },
      gray: { label: "Серый", badge: "8 бит · Серый" },
      color: { label: "Цветной", badge: "24 бита · RGB" }
    };
    const compressions = { lzw: "LZW", deflate: "Deflate", g4: "CCITT G4", jpeg: "JPEG" };
    const formatLabels = { tiff: "TIFF", pdf: "PDF", pdfa: "PDF/A", zip: "ZIP" };

    profiles.custom.title = `Кастомный ${formatLabels[format] || "файл"}`;
    profiles.custom.dpi = `${dpi} DPI`;
    profiles.custom.color = colors[color] ? colors[color].label : "Серый";
    profiles.custom.badge = colors[color] ? colors[color].badge : "8 бит · Серый";
    profiles.custom.compression = format === "tiff"
      ? (compressions[customCompression.value] || "LZW")
      : (format === "zip" ? (color === "bw" ? "PNG" : "JPEG") : `JPEG · ${customQuality?.value || 85}%`);

    if (activeProfile === "custom") {
      profileTitle.textContent = profiles.custom.title;
      profileDpi.textContent = profiles.custom.dpi;
      profileColor.textContent = profiles.custom.color;
      profileBadge.textContent = profiles.custom.badge;
      profileCompression.textContent = profiles.custom.compression;
    }
  }

  function showError(message) {
    fileError.textContent = message;
    fileError.classList.toggle("hidden", !message);
  }

  function hideCreateMessage() {
    createMessage.className = createMessageHiddenClass;
    createMessage.textContent = "";
  }

  function setReadingState(isReading, message = "Читаем страницы документа…") {
    isReadingFiles = isReading;
    uploadStatusText.textContent = message;
    uploadStatus.classList.toggle("hidden", !isReading);
    uploadStatus.classList.toggle("flex", isReading);
    fileInput.disabled = isReading || isProcessing;
    addMoreButton.disabled = isReading || isProcessing;
    if (outputFormatSelect) outputFormatSelect.disabled = isReading || isProcessing;
    updateTabsInteractivity();
    updateCreateState();
  }

  function updateCreateState() {
    const pages = activePages();
    const format = activeFile().outputFormat === "pdf" ? "PDF" : "TIFF";
    createButton.disabled = !pages.length || isReadingFiles || isProcessing;
    if (isProcessing) {
      createHint.textContent = `Подготавливаем ${format} на сервере…`;
    } else if (!pages.length) {
      createHint.textContent = "Добавьте хотя бы один файл";
    } else {
      createHint.textContent = `Соберём «${activeFile().name}» · ${pluralPages(pages.length)}`;
    }
  }

  function updateTabsInteractivity() {
    const locked = isProcessing || isReadingFiles;
    fileTabs.classList.toggle("pointer-events-none", locked);
    fileTabs.classList.toggle("opacity-45", locked);
    fileTabsSelect.disabled = locked;
    addFileTab.disabled = locked || editorFiles.length >= maxEditorFiles;
  }

  function tabLabel(name) {
    return name.replace(/\.(?:tiff?|pdf)$/i, "") || "Без названия";
  }

  function renderFileTabs() {
    fileTabs.innerHTML = editorFiles.map((file, index) => {
      const isActive = file.id === activeFileId;
      const canClose = editorFiles.length > 1 || file.pages.length > 0;
      return `
        <div class="flex shrink-0 items-center gap-0.5 rounded-xl border p-1 transition ${isActive ? "border-ink bg-ink text-white shadow-card" : "border-ink/10 bg-white hover:border-ink/30"}" data-tab-id="${escapeHtml(file.id)}">
          <button type="button" data-tab-select aria-pressed="${isActive}" title="${escapeHtml(file.name)}" class="flex max-w-[15rem] items-center gap-2 rounded-lg px-2 py-1.5 text-left transition">
            <span class="grid h-5 w-5 shrink-0 place-items-center rounded-md text-[10px] font-extrabold ${isActive ? "bg-lime text-ink" : "bg-paper text-ink/45"}">${index + 1}</span>
            <span class="truncate text-xs font-extrabold">${escapeHtml(tabLabel(file.name))}</span>
            <span class="shrink-0 rounded-md px-1.5 py-0.5 text-[9px] font-extrabold ${isActive ? "bg-white/15 text-white/70" : "bg-paper text-ink/40"}">${file.pages.length}</span>
          </button>
          <button type="button" data-tab-close aria-label="Удалить файл «${escapeHtml(file.name)}»" class="grid h-6 w-6 shrink-0 place-items-center rounded-md text-sm font-bold transition disabled:opacity-20 ${isActive ? "text-white/45 hover:bg-white/15 hover:text-white" : "text-ink/30 hover:bg-red-50 hover:text-red-700"}" ${canClose ? "" : "disabled"}>×</button>
        </div>
      `;
    }).join("");
    fileTabsSelect.innerHTML = editorFiles.map((file, index) =>
      `<option value="${escapeHtml(file.id)}" ${file.id === activeFileId ? "selected" : ""}>${index + 1}. ${escapeHtml(tabLabel(file.name))} · ${pluralPages(file.pages.length)}</option>`
    ).join("");
    updateTabsInteractivity();
  }

  function syncFilenameInput() {
    const file = activeFile();
    if (document.activeElement !== resultFilename) resultFilename.value = file.name;
    activeFileTitle.textContent = `Страницы файла «${tabLabel(file.name)}»`;
    syncOutputControls();
  }

  function syncOutputControls() {
    const file = activeFile();
    let format = file.outputFormat || "tiff";
    if (file.profile === "registration") {
      format = "tiff";
    } else if (file.profile === "vat") {
      format = format === "pdf" ? "pdf" : "tiff";
    }
    file.outputFormat = format;
    const formatLabels = { tiff: "TIFF", pdf: "PDF", pdfa: "PDF/A", zip: "ZIP" };
    const label = formatLabels[format] || "TIFF";
    const extension = extensionForFormat(format);

    if (outputFormatSelect) {
      outputFormatSelect.value = format === "pdfa" ? "pdf" : format;
      outputFormatSelect.disabled = isProcessing || isReadingFiles;
    }
    if (customFormat) {
      customFormat.value = format;
      customFormat.disabled = isProcessing || isReadingFiles;
    }
    if (outputFormatHint) {
      if (format === "pdf") {
        outputFormatHint.textContent = "Многостраничный image-only PDF до 100 МБ: 300 DPI, 8-битный серый цвет, без ссылок, форм и вложений.";
      } else if (format === "pdfa") {
        outputFormatHint.textContent = "Архивный PDF/A-1b с метаданными для судов и долгосрочного хранения.";
      } else if (format === "zip") {
        outputFormatHint.textContent = "ZIP-архив с постраничными изображениями (JPG / PNG).";
      } else {
        outputFormatHint.textContent = "Многостраничный TIFF: 300 DPI, 8-битный серый цвет, сжатие LZW.";
      }
    }
    resultFilenameHint.textContent = `Так назовётся ${label} активной вкладки. Расширение .${extension} добавится автоматически`;
    createButtonLabel.textContent = `Скачать ${label}`;
    if (!isProcessing) confirmCreateButton.textContent = `Скачать ${label}`;
    if (file.profile === "vat") {
      profileCompression.textContent = format === "pdf" ? "JPEG · 92%" : profiles.vat.compression;
    }
  }

  function renderEditor() {
    renderFileTabs();
    renderFiles();
    syncFilenameInput();
  }

  function updateCounters() {
    const pages = activePages();
    filesPanel.classList.toggle("hidden", pages.length === 0);
    fileCountTop.textContent = pages.length ? pluralPages(pages.length) : "Пока пусто";
    summaryFiles.textContent = pluralPages(pages.length);
    summarySize.textContent = formatBytes(activeFileSize());
    summaryTotals.textContent = `Всего в работе: ${pluralFiles(editorFiles.length)} · ${pluralPages(totalPageCount())}`;
    updateCreateState();
  }

  function renderFiles() {
    const pages = activePages();
    fileList.innerHTML = pages.map((page, index) => `
      <li class="page-card file-row group rounded-2xl border border-ink/10 bg-white p-2 shadow-[0_5px_14px_rgba(23,24,21,.04)] transition hover:-translate-y-0.5 hover:border-violet/45 hover:shadow-card" data-file-index="${index}" data-preview-id="${encodeURIComponent(page.id)}" draggable="true">
        <div class="page-preview page-preview--empty relative grid overflow-hidden rounded-xl border border-ink/10 bg-paper">
          <div data-page-preview class="grid h-full w-full place-items-center text-center"><span class="h-6 w-6 animate-spin rounded-full border-2 border-violet/20 border-t-violet"></span></div>
          <span data-page-position class="absolute left-2 top-2 grid h-7 min-w-7 place-items-center rounded-lg bg-ink px-1.5 text-[10px] font-extrabold text-lime shadow-card">${index + 1}</span>
          ${page.rotation ? `<span data-page-rotation class="absolute top-2 left-10 rounded-md bg-violet px-1.5 py-0.5 text-[9px] font-extrabold text-white shadow-sm">${page.rotation}°</span>` : ""}
          <button type="button" data-file-action="preview" disabled aria-label="Увеличить страницу" class="absolute right-2 top-2 grid h-8 w-8 place-items-center rounded-lg bg-white/90 text-ink shadow-card transition hover:bg-violet hover:text-white disabled:cursor-wait disabled:opacity-45">
            <svg viewBox="0 0 24 24" class="h-4 w-4" aria-hidden="true"><circle cx="10.5" cy="10.5" r="5.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="m15 15 4.5 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          </button>
          <span class="absolute bottom-2 right-2 rounded-md bg-white/90 px-1.5 py-1 text-[9px] font-extrabold text-ink/55 shadow-sm">${page.type}</span>
        </div>
        <div class="min-w-0 px-1 pb-1 pt-2">
          <p class="truncate text-xs font-extrabold" title="${escapeHtml(page.name)}">${escapeHtml(page.name)}</p>
          <label class="mt-1 flex items-center gap-1 text-[10px] font-semibold text-ink/40">
            <span>Страница</span>
            <input data-page-position-input type="text" inputmode="numeric" maxlength="3" value="${index + 1}" aria-label="Позиция страницы ${index + 1}" class="h-6 w-9 rounded-md border border-ink/10 bg-paper px-1 text-center text-[10px] font-extrabold text-ink outline-none transition focus:border-violet focus:ring-2 focus:ring-violet/10" />
            <span>из <span data-page-count>${pages.length}</span></span>
          </label>
          <div class="mt-2 flex items-center justify-between gap-1">
            <span class="text-[10px] font-bold text-ink/30 whitespace-nowrap select-none" aria-hidden="true">⠿ Переместить</span>
            <span class="flex items-center gap-1 flex-shrink-0">
              <button type="button" data-file-action="rotate" title="Повернуть на 90°" aria-label="Повернуть страницу на 90 градусов" class="grid h-7 w-7 place-items-center rounded-md border border-ink/10 text-xs font-bold transition hover:border-violet hover:bg-paper hover:text-violet">↻</button>
              <button type="button" data-file-action="up" aria-label="Переместить страницу выше" class="grid h-7 w-7 place-items-center rounded-md border border-ink/10 text-xs font-bold transition hover:bg-paper disabled:opacity-20" ${index === 0 ? "disabled" : ""}>↑</button>
              <button type="button" data-file-action="down" aria-label="Переместить страницу ниже" class="grid h-7 w-7 place-items-center rounded-md border border-ink/10 text-xs font-bold transition hover:bg-paper disabled:opacity-20" ${index === pages.length - 1 ? "disabled" : ""}>↓</button>
              <button type="button" data-file-action="remove" aria-label="Удалить страницу" class="grid h-7 w-7 place-items-center rounded-md border border-ink/10 transition hover:border-red-200 hover:bg-red-50 hover:text-red-700">
                <svg viewBox="0 0 24 24" class="h-3.5 w-3.5" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m-8 0 1 13h8l1-13M10 10v7m4-7v7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
            </span>
          </div>
        </div>
      </li>
    `).join("");
    pages.forEach(requestPagePreview);
    updateCounters();
  }

  function syncPageCardOrder() {
    const pages = activePages();
    pages.forEach((page, index) => {
      const card = fileList.querySelector(`[data-preview-id="${encodeURIComponent(page.id)}"]`);
      if (!card) return;
      card.dataset.fileIndex = String(index);
      card.querySelector("[data-page-position]").textContent = String(index + 1);
      const positionInput = card.querySelector("[data-page-position-input]");
      positionInput.value = String(index + 1);
      positionInput.setAttribute("aria-label", `Позиция страницы ${index + 1}`);
      card.querySelector("[data-page-count]").textContent = String(pages.length);
      card.querySelector('[data-file-action="up"]').disabled = index === 0;
      card.querySelector('[data-file-action="down"]').disabled = index === pages.length - 1;
      card.classList.remove("dragging", "drop-before", "drop-after", "drop-swap");
      delete card.dataset.dropPosition;
      fileList.append(card);
    });
    updateCounters();
    renderFileTabs();
  }

  function selectEditorFile(id) {
    if (isProcessing || isReadingFiles || id === activeFileId) return;
    if (!editorFiles.some(file => file.id === id)) return;
    activeFileId = id;
    selectProfile(activeFile().profile);
    showError("");
    hideCreateMessage();
    renderEditor();
    saveEditorState();
  }

  async function addEditorFile() {
    await filesRestored;
    if (isProcessing || isReadingFiles) return;
    if (editorFiles.length >= maxEditorFiles) {
      showError(`Одновременно можно готовить не больше ${pluralFiles(maxEditorFiles)}.`);
      return;
    }
    const file = makeEditorFile();
    editorFiles.push(file);
    activeFileId = file.id;
    showError("");
    hideCreateMessage();
    await saveEditorState();
    renderEditor();
  }

  async function closeEditorFile(id) {
    await filesRestored;
    if (isProcessing || isReadingFiles) return;
    const index = editorFiles.findIndex(file => file.id === id);
    if (index < 0) return;
    const target = editorFiles[index];
    if (target.pages.length && !window.confirm(`Удалить файл «${target.name}» вместе со страницами (${pluralPages(target.pages.length)})?`)) return;
    editorFiles.splice(index, 1);
    if (!editorFiles.length) editorFiles.push(makeEditorFile());
    if (activeFileId === id) activeFileId = editorFiles[Math.min(index, editorFiles.length - 1)].id;
    dropUnusedSources();
    showError("");
    hideCreateMessage();
    await saveEditorState();
    renderEditor();
  }

  async function addFiles(files) {
    if (isReadingFiles || isProcessing) return;
    await filesRestored;
    showError("");
    const incoming = [...files];
    fileInput.value = "";
    if (!incoming.length) return;
    const invalid = incoming.find(file => !allowedExtensions.has(file.name.split(".").pop()?.toLowerCase()));
    if (invalid) {
      showError(`Формат файла «${invalid.name}» не поддерживается. Добавьте PDF, JPG, PNG или TIFF.`);
      return;
    }

    const seenKeys = new Set();
    const uniqueIncoming = incoming.filter(file => {
      const key = fileKey(file);
      if (seenKeys.has(key)) return false;
      seenKeys.add(key);
      return true;
    });
    // Файл, уже прочитанный для другой вкладки, второй раз в пул не кладём —
    // но его страницы всё равно можно добавить в текущий итоговый файл.
    const poolKeys = new Set(sourceDocuments.map(fileKey));
    const newSources = uniqueIncoming.filter(file => !poolKeys.has(fileKey(file)));
    const totalSize = poolSize() + newSources.reduce((total, file) => total + file.size, 0);
    if (totalSize > maxTotalSize) {
      showError(`Общий размер всех файлов не должен превышать ${formatBytes(maxTotalSize)}.`);
      return;
    }

    setReadingState(true, uniqueIncoming.length > 1 ? "Читаем страницы документов…" : "Читаем страницы документа…");
    try {
      const target = activeFile();
      const existingPages = new Set(target.pages.map(page => page.id));
      const expanded = (await Promise.all(uniqueIncoming.map(expandIntoPages))).flat();
      const newPages = expanded.filter(page => !existingPages.has(page.id));
      if (!newPages.length) {
        showError("Эти страницы уже добавлены в текущий файл.");
        return;
      }
      if (target.pages.length + newPages.length > maxPagesPerFile) {
        showError(`В одном итоговом файле может быть не больше ${pluralPages(maxPagesPerFile)}.`);
        return;
      }
      sourceDocuments.push(...newSources);
      target.pages.push(...newPages);
      await saveEditorState();
      renderEditor();
    } catch (error) {
      showError(`Не удалось прочитать страницы документа. ${error.message || "Проверьте файл и попробуйте снова."}`);
    } finally {
      setReadingState(false);
    }
  }

  function showCreateMessage(message, tone = "success") {
    createMessage.textContent = message;
    createMessage.className = tone === "error"
      ? "mt-3 rounded-xl bg-red-50 px-4 py-3 text-xs font-bold leading-relaxed text-red-700"
      : "mt-3 rounded-xl bg-lime px-4 py-3 text-xs font-bold leading-relaxed text-ink";
  }

  function setProcessingState(processing) {
    isProcessing = processing;
    const format = activeFile().outputFormat === "pdf" ? "PDF" : "TIFF";
    confirmCreateButton.disabled = processing;
    confirmCreateButton.textContent = processing ? `Готовим ${format}…` : `Скачать ${format}`;
    closeCreateModalButton.disabled = processing;
    fileInput.disabled = processing || isReadingFiles;
    addMoreButton.disabled = processing;
    clearFiles.disabled = processing;
    resultFilename.disabled = processing;
    if (outputFormatSelect) outputFormatSelect.disabled = processing || isReadingFiles;
    profileButtons.forEach(button => { button.disabled = processing; });
    updateTabsInteractivity();
    updateCreateState();
  }

  function pageOrderFor(uploads) {
    const sourceIndexes = new Map(uploads.map((file, index) => [fileKey(file), index]));
    return activePages().map(page => ({
      source_index: sourceIndexes.get(page.sourceId),
      page_number: page.pageNumber,
      rotation: page.rotation || 0
    }));
  }

  async function downloadResult(response, filename) {
    const resultFile = await response.blob();
    const downloadUrl = URL.createObjectURL(resultFile);
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    return resultFile;
  }

  async function saveResultForViewer(file, filename) {
    const database = await new Promise((resolve, reject) => {
      const request = indexedDB.open("fnskit-viewer", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("documents", { keyPath: "id" });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise((resolve, reject) => {
      const transaction = database.transaction("documents", "readwrite");
      transaction.objectStore("documents").put({ id: "latest-result", file, filename, createdAt: Date.now() });
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  }

  function offerResultViewer(result) {
    if (result.formatLabel !== "TIFF" || !result.file) return;
    const modal = document.createElement("div");
    modal.className = "fixed inset-0 z-[90] flex items-center justify-center bg-ink/45 p-4 backdrop-blur-sm";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.innerHTML = `
      <div class="w-full max-w-md rounded-[2rem] border border-ink/10 bg-paper p-5 shadow-[0_24px_80px_rgba(23,24,21,.28)] sm:p-7">
        <span class="grid h-11 w-11 place-items-center rounded-2xl bg-lime text-xl">✓</span>
        <p class="mt-5 text-[10px] font-extrabold uppercase tracking-[.16em] text-violet">TIFF готов</p>
        <h2 class="mt-2 text-2xl font-extrabold tracking-[-.05em]">Проверить результат?</h2>
        <p class="mt-3 text-sm font-semibold leading-relaxed text-ink/55">Откроем созданный файл в локальном просмотрщике. Он останется только в вашем браузере.</p>
        <div class="mt-6 grid gap-2 sm:grid-cols-2">
          <button type="button" data-view-result class="rounded-xl bg-ink px-4 py-3 text-sm font-extrabold text-white transition hover:bg-violet">Открыть просмотр</button>
          <button type="button" data-close-result class="rounded-xl border border-ink/15 bg-white px-4 py-3 text-sm font-extrabold transition hover:border-ink">Позже</button>
        </div>
      </div>`;
    const close = () => modal.remove();
    modal.querySelector("[data-close-result]").addEventListener("click", close);
    modal.addEventListener("click", event => { if (event.target === modal) close(); });
    modal.querySelector("[data-view-result]").addEventListener("click", async () => {
      const button = modal.querySelector("[data-view-result]");
      button.disabled = true;
      button.textContent = "Открываем…";
      try {
        await saveResultForViewer(result.file, result.filename);
        window.location.assign("/tiff-viewer");
      } catch {
        button.disabled = false;
        button.textContent = "Повторить";
      }
    });
    document.body.append(modal);
  }

  // Собирается только активная вкладка: один запрос — один итоговый файл.
  async function createDocument() {
    const target = activeFile();
    let outputFormat = target.outputFormat || "tiff";
    if (target.profile === "registration") {
      outputFormat = "tiff";
    } else if (target.profile === "vat") {
      outputFormat = outputFormat === "pdf" ? "pdf" : "tiff";
    }
    const formatLabels = { tiff: "TIFF", pdf: "PDF", pdfa: "PDF/A", zip: "ZIP" };
    const formatLabel = formatLabels[outputFormat] || "TIFF";
    const uploads = activeSourceFiles();
    const pageOrder = pageOrderFor(uploads);
    if (!pageOrder.length || pageOrder.some(page => page.source_index === undefined)) {
      throw new Error("Не удалось подготовить список страниц. Обновите редактор и попробуйте снова.");
    }

    const formData = new FormData();
    formData.append("profile", activeProfile);
    formData.append("output_format", outputFormat);
    if (activeProfile === "custom") {
      formData.append("dpi", customDpi.value);
      formData.append("color_mode", customColor.value);
      formData.append("compression", customCompression.value);
      const isPdf = outputFormat === "pdf" || outputFormat === "pdfa";
      const isZip = outputFormat === "zip";
      const isTiff = outputFormat === "tiff";
      const showsQuality = isPdf || (isTiff && customCompression.value === "jpeg") || (isZip && customColor.value !== "bw");
      if (showsQuality && customQuality && customQuality.value) {
        formData.append("quality", customQuality.value);
      }
      if (customTargetMb && customTargetMb.value !== "0") {
        formData.append("target_max_mb", customTargetMb.value);
      }
      if (customColor.value === "bw" && customThreshold && customThreshold.value) {
        formData.append("threshold", customThreshold.value);
      }
    }
    formData.append("page_order", JSON.stringify(pageOrder));
    formData.append("personal_data_consent", String(consentCheckbox.checked));
    formData.append("consent_version", consentVersion);
    uploads.forEach(file => formData.append("files", file, file.name));

    const response = await fetch(`${processorApiBase}/v1/tiff`, {
      method: "POST",
      body: formData
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = payload.detail;
      if (detail && typeof detail === "object" && Number.isInteger(detail.source_index)) {
        const source = uploads[detail.source_index];
        const fileLabel = source ? `Файл «${source.name}»` : `Файл №${detail.source_index + 1}`;
        const pageLabel = Number.isInteger(detail.page_number) ? `, страница ${detail.page_number}` : "";
        throw new Error(`${fileLabel}${pageLabel}. ${detail.message || "Не удалось обработать файл."}`);
      }
      throw new Error(typeof detail === "string" ? detail : `Не удалось подготовить ${formatLabel}.`);
    }

    const pageCount = Number(response.headers.get("X-Page-Count") || pageOrder.length);
    target.name = normalizeResultFilename(target.name, outputFormat, target.profile);
    const filename = target.name;
    const file = await downloadResult(response, filename);
    return { pageCount, filename, formatLabel, file };
  }

  function closeCreateModal(options) {
    const force = options === true;
    if (isProcessing && !force) return;
    createModal.classList.add("hidden");
    createModal.classList.remove("flex");
    createModal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("overflow-hidden");
    createButton.focus();
  }

  function openCreateModal() {
    modalCreateError.classList.add("hidden");
    // Отметку не переносим из прошлого открытия: согласие — на этот запрос, а не навсегда.
    consentCheckbox.checked = false;
    syncOutputControls();
    createModalTarget.textContent = `Соберём файл «${activeFile().name}» · ${pluralPages(activePages().length)}`;
    createModal.classList.remove("hidden");
    createModal.classList.add("flex");
    createModal.setAttribute("aria-hidden", "false");
    document.body.classList.add("overflow-hidden");
    requestAnimationFrame(() => consentCheckbox.focus());
  }

  function updatePagePreviewZoom() {
    const multiplier = previewZoomSteps[previewZoomIndex];
    pagePreviewZoomOut.disabled = previewZoomIndex === 0;
    pagePreviewZoomIn.disabled = previewZoomIndex === previewZoomSteps.length - 1;
    pagePreviewZoomValue.textContent = `${Math.round(multiplier * 100)}%`;
    if (!pagePreviewImage.naturalWidth || pagePreviewImage.classList.contains("hidden")) return;

    const availableWidth = Math.max(1, pagePreviewViewport.clientWidth - 32);
    const availableHeight = Math.max(1, pagePreviewViewport.clientHeight - 32);
    const fitScale = Math.min(
      availableWidth / pagePreviewImage.naturalWidth,
      availableHeight / pagePreviewImage.naturalHeight,
      1
    );
    pagePreviewImage.style.width = `${Math.round(pagePreviewImage.naturalWidth * fitScale * multiplier)}px`;
    pagePreviewImage.style.height = "auto";
    if (previewZoomIndex === 0) {
      pagePreviewViewport.scrollLeft = 0;
      pagePreviewViewport.scrollTop = 0;
    }
  }

  async function openPagePreview(index) {
    const page = activePages()[index];
    if (!page) return;
    activePreviewPageId = page.id;
    previewZoomIndex = 0;
    pagePreviewTitle.textContent = `${page.name} · страница ${page.pageNumber} из ${page.pageTotal}`;
    pagePreviewImage.alt = `Превью: ${page.name}, страница ${page.pageNumber}`;
    pagePreviewImage.classList.add("hidden");
    pagePreviewImage.removeAttribute("src");
    pagePreviewLoadingText.textContent = "Готовим чёткое изображение…";
    pagePreviewLoading.classList.remove("hidden");
    pagePreviewLoading.classList.add("flex");
    pagePreviewModal.classList.remove("hidden");
    pagePreviewModal.classList.add("flex");
    pagePreviewModal.setAttribute("aria-hidden", "false");
    document.body.classList.add("overflow-hidden");
    updatePagePreviewZoom();
    closePagePreviewButton.focus();

    try {
      let source = fullPreviewCache.get(page.id);
      if (!source) {
        source = await makeFullPagePreview(page);
        cacheFullPreview(page.id, source);
      }
      if (activePreviewPageId !== page.id) return;
      pagePreviewImage.onload = () => {
        if (activePreviewPageId !== page.id) return;
        pagePreviewImage.classList.remove("hidden");
        pagePreviewLoading.classList.add("hidden");
        pagePreviewLoading.classList.remove("flex");
        requestAnimationFrame(updatePagePreviewZoom);
      };
      pagePreviewImage.src = source;
    } catch {
      if (activePreviewPageId === page.id) {
        pagePreviewLoadingText.textContent = "Не удалось подготовить увеличенное изображение.";
      }
    }
  }

  function closePagePreview() {
    activePreviewPageId = null;
    pagePreviewModal.classList.add("hidden");
    pagePreviewModal.classList.remove("flex");
    pagePreviewModal.setAttribute("aria-hidden", "true");
    pagePreviewImage.onload = null;
    pagePreviewImage.removeAttribute("src");
    pagePreviewImage.classList.add("hidden");
    document.body.classList.remove("overflow-hidden");
  }

  const requestedProfile = document.documentElement.dataset.profile || new URLSearchParams(window.location.search).get("profile");
  ensureEditorFile();
  selectProfile(profiles[requestedProfile] ? requestedProfile : "vat");
  if (activeProfile === "custom") updateCustomSettings();
  renderEditor();
  profileButtons.forEach(button => button.addEventListener("click", async () => {
    const profile = button.dataset.profile;
    if (!profiles[profile] || isProcessing || isReadingFiles) return;
    selectProfile(profile);
    await saveEditorState();
    window.location.href = profilePages[profile];
  }));
  if (customThreshold) {
    customThreshold.addEventListener("input", () => {
      const val = Number(customThreshold.value) || 180;
      let note = "Оптимально";
      if (val < 140) note = "Светлее";
      else if (val > 200) note = "Плотнее";
      if (customThresholdValue) customThresholdValue.textContent = `${val} · ${note}`;
    });
    customThreshold.addEventListener("change", async () => {
      if (customPreset) customPreset.value = "manual";
      await saveEditorState();
    });
  }

  thresholdMinus?.addEventListener("click", () => {
    if (customThreshold && !customThreshold.disabled) {
      const current = Number(customThreshold.value) || 180;
      const next = Math.max(100, current - 5);
      customThreshold.value = String(next);
      customThreshold.dispatchEvent(new Event("input"));
      customThreshold.dispatchEvent(new Event("change"));
    }
  });

  thresholdPlus?.addEventListener("click", () => {
    if (customThreshold && !customThreshold.disabled) {
      const current = Number(customThreshold.value) || 180;
      const next = Math.min(220, current + 5);
      customThreshold.value = String(next);
      customThreshold.dispatchEvent(new Event("input"));
      customThreshold.dispatchEvent(new Event("change"));
    }
  });

  customPreset?.addEventListener("change", async () => {
    const presetKey = customPreset.value;
    if (presetKey === "manual") return;
    const p = customPresets[presetKey];
    if (!p) return;
    if (customFormat) customFormat.value = p.format;
    if (customDpi) customDpi.value = p.dpi;
    if (customColor) customColor.value = p.color;
    if (customCompression) customCompression.value = p.compression;
    if (customQuality) customQuality.value = p.quality || "85";
    if (customTargetMb) customTargetMb.value = p.targetMb || "0";
    if (p.threshold && customThreshold) {
      customThreshold.value = p.threshold;
    }
    const file = activeFile();
    file.outputFormat = p.format;
    file.name = normalizeResultFilename(file.name, file.outputFormat, file.profile);
    updateCustomSettings();
    renderEditor();
    await saveEditorState();
  });

  [customFormat, customDpi, customColor, customCompression, customQuality, customTargetMb].forEach(el => {
    el?.addEventListener("change", async () => {
      if (customPreset) customPreset.value = "manual";
      if (el === customFormat) {
        const file = activeFile();
        file.outputFormat = customFormat.value;
        file.name = normalizeResultFilename(file.name, file.outputFormat, file.profile);
      }
      updateCustomSettings();
      renderEditor();
      await saveEditorState();
    });
  });

  fileTabs.addEventListener("click", async event => {
    const holder = event.target.closest("[data-tab-id]");
    if (!holder) return;
    await filesRestored;
    if (event.target.closest("[data-tab-close]")) {
      await closeEditorFile(holder.dataset.tabId);
      return;
    }
    if (event.target.closest("[data-tab-select]")) selectEditorFile(holder.dataset.tabId);
  });
  fileTabsSelect.addEventListener("change", () => selectEditorFile(fileTabsSelect.value));
  addFileTab.addEventListener("click", addEditorFile);

  resultFilename.addEventListener("input", () => {
    const file = activeFile();
    file.filenameEdited = true;
    file.name = resultFilename.value;
    renderFileTabs();
    syncFilenameInput();
    updateCreateState();
  });
  resultFilename.addEventListener("blur", async () => {
    const file = activeFile();
    file.name = normalizeResultFilename(file.name, file.outputFormat, file.profile);
    resultFilename.value = file.name;
    renderFileTabs();
    syncFilenameInput();
    updateCreateState();
    await saveEditorState();
  });
  resultFilename.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      resultFilename.blur();
    }
  });
  outputFormatSelect?.addEventListener("change", async () => {
    await filesRestored;
    if (isProcessing || isReadingFiles) return;
    const file = activeFile();
    file.outputFormat = outputFormatSelect.value === "pdf" ? "pdf" : "tiff";
    file.name = normalizeResultFilename(file.name, file.outputFormat, file.profile);
    hideCreateMessage();
    renderEditor();
    await saveEditorState();
  });
  fileInput.addEventListener("change", event => addFiles(event.target.files));

  ["dragenter", "dragover"].forEach(type => {
    dropzone.addEventListener(type, event => {
      event.preventDefault();
      dropzone.classList.add("drop-active");
    });
  });
  ["dragleave", "drop"].forEach(type => {
    dropzone.addEventListener(type, event => {
      event.preventDefault();
      dropzone.classList.remove("drop-active");
    });
  });
  dropzone.addEventListener("drop", event => addFiles(event.dataTransfer.files));

  fileList.addEventListener("click", async event => {
    await filesRestored;
    const button = event.target.closest("[data-file-action]");
    const row = event.target.closest("[data-file-index]");
    if (!button || !row) return;
    const pages = activePages();
    const index = Number(row.dataset.fileIndex);
    const action = button.dataset.fileAction;
    if (action === "rotate") {
      const page = pages[index];
      if (!page) return;
      page.rotation = ((page.rotation || 0) + 90) % 360;
      const img = row.querySelector("[data-page-preview] img");
      if (img) img.style.transform = `rotate(${page.rotation}deg)`;
      let badge = row.querySelector("[data-page-rotation]");
      if (page.rotation) {
        if (!badge) {
          badge = document.createElement("span");
          badge.dataset.pageRotation = "";
          badge.className = "absolute top-2 left-10 rounded-md bg-violet px-1.5 py-0.5 text-[9px] font-extrabold text-white shadow-sm";
          row.querySelector(".page-preview").append(badge);
        }
        badge.textContent = `${page.rotation}°`;
      } else if (badge) {
        badge.remove();
      }
      await saveEditorState();
      return;
    }
    if (action === "preview") {
      openPagePreview(index);
      return;
    }
    if (action === "remove") {
      pages.splice(index, 1);
      row.remove();
      dropUnusedSources();
    }
    if (action === "up" && index > 0) [pages[index - 1], pages[index]] = [pages[index], pages[index - 1]];
    if (action === "down" && index < pages.length - 1) [pages[index + 1], pages[index]] = [pages[index], pages[index + 1]];
    syncPageCardOrder();
    await saveEditorState();
  });

  fileList.addEventListener("input", event => {
    const input = event.target.closest("[data-page-position-input]");
    if (!input) return;
    input.value = input.value.replace(/\D/g, "").slice(0, 3);
  });

  fileList.addEventListener("keydown", event => {
    const input = event.target.closest("[data-page-position-input]");
    if (!input) return;
    if (event.key === "Enter") {
      event.preventDefault();
      input.blur();
    }
    if (event.key === "Escape") {
      const row = input.closest("[data-file-index]");
      input.value = row ? String(Number(row.dataset.fileIndex) + 1) : "";
      input.blur();
    }
  });

  fileList.addEventListener("change", async event => {
    const input = event.target.closest("[data-page-position-input]");
    const row = input?.closest("[data-file-index]");
    if (!input || !row) return;
    const pages = activePages();
    const from = Number(row.dataset.fileIndex);
    const enteredPosition = Number.parseInt(input.value, 10);
    if (!Number.isFinite(enteredPosition)) {
      syncPageCardOrder();
      return;
    }
    const to = Math.max(0, Math.min(pages.length - 1, enteredPosition - 1));
    if (from !== to) {
      const [page] = pages.splice(from, 1);
      pages.splice(to, 0, page);
    }
    syncPageCardOrder();
    await saveEditorState();
  });

  fileList.addEventListener("dragstart", event => {
    if (isProcessing || event.target.closest("button, input")) {
      event.preventDefault();
      return;
    }
    const card = event.target.closest("[data-file-index]");
    if (!card) return;
    const page = activePages()[Number(card.dataset.fileIndex)];
    if (!page) return;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", page.id);
    card.classList.add("dragging");
  });

  fileList.addEventListener("dragover", event => {
    if (isProcessing) return;
    event.preventDefault();
    const card = event.target.closest("[data-file-index]");
    fileList.querySelectorAll(".drop-before, .drop-after, .drop-swap").forEach(item => {
      item.classList.remove("drop-before", "drop-after", "drop-swap");
      delete item.dataset.dropPosition;
    });
    if (card && !card.classList.contains("dragging")) {
      const bounds = card.getBoundingClientRect();
      const horizontalPosition = (event.clientX - bounds.left) / bounds.width;
      const position = horizontalPosition < .3
        ? "before"
        : horizontalPosition > .7
          ? "after"
          : "swap";
      card.dataset.dropPosition = position;
      card.classList.add(
        position === "before"
          ? "drop-before"
          : position === "after"
            ? "drop-after"
            : "drop-swap"
      );
    }
    event.dataTransfer.dropEffect = "move";
  });

  fileList.addEventListener("dragleave", event => {
    const card = event.target.closest("[data-file-index]");
    if (card && !card.contains(event.relatedTarget)) {
      card.classList.remove("drop-before", "drop-after", "drop-swap");
      delete card.dataset.dropPosition;
    }
  });

  fileList.addEventListener("dragend", () => {
    fileList.querySelectorAll(".dragging, .drop-before, .drop-after, .drop-swap").forEach(item => {
      item.classList.remove("dragging", "drop-before", "drop-after", "drop-swap");
      delete item.dataset.dropPosition;
    });
  });

  fileList.addEventListener("drop", async event => {
    if (isProcessing) return;
    event.preventDefault();
    const pages = activePages();
    const target = event.target.closest("[data-file-index]");
    const draggedId = event.dataTransfer.getData("text/plain");
    const from = pages.findIndex(page => page.id === draggedId);
    const targetPage = target ? pages[Number(target.dataset.fileIndex)] : null;
    if (!target || from < 0 || !targetPage || targetPage.id === draggedId) return;
    const targetIndex = Number(target.dataset.fileIndex);
    const dropPosition = target.dataset.dropPosition || "swap";
    if (dropPosition === "swap") {
      [pages[from], pages[targetIndex]] = [pages[targetIndex], pages[from]];
    } else {
      let insertionIndex = targetIndex + (dropPosition === "after" ? 1 : 0);
      const [page] = pages.splice(from, 1);
      if (from < insertionIndex) insertionIndex -= 1;
      pages.splice(insertionIndex, 0, page);
    }
    syncPageCardOrder();
    await saveEditorState();
  });

  rotateAllButton?.addEventListener("click", async () => {
    await filesRestored;
    const pages = activePages();
    if (!pages.length) return;
    pages.forEach(page => {
      page.rotation = ((page.rotation || 0) + 90) % 360;
    });
    renderFiles();
    await saveEditorState();
  });

  addMoreButton.addEventListener("click", () => fileInput.click());
  closePagePreviewButton.addEventListener("click", closePagePreview);
  pagePreviewZoomOut.addEventListener("click", () => {
    if (previewZoomIndex === 0) return;
    previewZoomIndex -= 1;
    updatePagePreviewZoom();
  });
  pagePreviewZoomFit.addEventListener("click", () => {
    previewZoomIndex = 0;
    updatePagePreviewZoom();
  });
  pagePreviewZoomIn.addEventListener("click", () => {
    if (previewZoomIndex === previewZoomSteps.length - 1) return;
    previewZoomIndex += 1;
    updatePagePreviewZoom();
  });
  pagePreviewModal.addEventListener("click", event => {
    if (event.target === pagePreviewModal) closePagePreview();
  });
  window.addEventListener("resize", () => {
    if (!pagePreviewModal.classList.contains("hidden")) updatePagePreviewZoom();
  });

  clearFiles.addEventListener("click", async () => {
    await filesRestored;
    if (isProcessing || isReadingFiles) return;
    const file = activeFile();
    if (!file.pages.length) return;
    file.pages = [];
    dropUnusedSources();
    showError("");
    hideCreateMessage();
    await saveEditorState();
    renderEditor();
  });
  consentCheckbox.addEventListener("change", () => modalCreateError.classList.add("hidden"));
  closeCreateModalButton.addEventListener("click", closeCreateModal);
  createModal.addEventListener("click", event => {
    if (event.target === createModal) closeCreateModal();
  });

  createButton.addEventListener("click", () => {
    if (createButton.disabled) return;
    openCreateModal();
  });

  // Окно остаётся открытым, пока идёт обработка, поэтому ошибка показывается в
  // нём, а не на странице под затемнением.
  confirmCreateButton.addEventListener("click", async () => {
    if (confirmCreateButton.disabled) return;
    modalCreateError.classList.add("hidden");
    if (!consentCheckbox.checked) {
      modalCreateError.textContent = "Отметьте согласие на обработку персональных данных.";
      modalCreateError.classList.remove("hidden");
      consentCheckbox.focus();
      return;
    }
    hideCreateMessage();
    setProcessingState(true);
    try {
      const result = await createDocument();
      closeCreateModal(true);
      showCreateMessage(`Файл «${result.filename}» готов: ${pluralPages(result.pageCount)}. Скачивание началось.`);
      offerResultViewer(result);
      await saveEditorState();
      renderEditor();
    } catch (error) {
      const format = activeFile().outputFormat === "pdf" ? "PDF" : "TIFF";
      modalCreateError.textContent = error.message || `Не удалось подготовить ${format}.`;
      modalCreateError.classList.remove("hidden");
    } finally {
      setProcessingState(false);
    }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !pagePreviewModal.classList.contains("hidden")) {
      closePagePreview();
      return;
    }
    if (event.key === "Escape" && !createModal.classList.contains("hidden")) closeCreateModal();
  });

  filesRestored = restoreEditorState();
})();
