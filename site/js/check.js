// Проверка готовых документов по приказам ФНС онлайн.
// Поддерживает три режима: НДС (vat), Регистрация (registration), Свои параметры (custom).
(() => {
  const profiles = {
    vat: {
      title: "НДС и требования ФНС",
      badge: "150–300 DPI · Серый",
      norm: "Приказ ФНС от 10.07.2024 № ЕД-7-26/547@",
      background: "#DDD9FF",
      editorUrl: "/editor-dlya-fns"
    },
    registration: {
      title: "Регистрация ООО / ИП",
      badge: "300 DPI · 1 бит Ч/б · G4",
      norm: "Приказ ФНС от 28.12.2022 № ЕД-7-14/1267@",
      background: "#C8FF32",
      editorUrl: "/editor-dlya-registracii"
    },
    custom: {
      title: "Свои параметры",
      badge: "Пользовательские требования",
      norm: "Настраиваемый профиль",
      background: "#FFDFA8",
      editorUrl: "/editor-custom"
    }
  };

  const allowedExtensions = new Set(["pdf", "tif", "tiff"]);
  const maxFileSize = 100 * 1024 * 1024;
  const consentVersion = "2026-10-17.1";
  const fileStoreDatabase = "fnskit-editor";
  const fileStoreName = "documents";
  const processorApiBase = window.FNSKIT_PROCESSOR_API || "http://localhost:8080";

  let activeProfile = "vat";
  let currentFile = null;
  let isChecking = false;

  // DOM Elements
  const profileButtons = [...document.querySelectorAll(".profile-button")];
  const profileSummary = document.getElementById("profileSummary");
  const profileTitle = document.getElementById("profileTitle");
  const profileBadge = document.getElementById("profileBadge");
  const profileNorm = document.getElementById("profileNorm");
  const customSettings = document.getElementById("customSettings");
  const customPreset = document.getElementById("customPreset");
  const customFormat = document.getElementById("customFormat");
  const customDpi = document.getElementById("customDpi");
  const customColor = document.getElementById("customColor");
  const customCompression = document.getElementById("customCompression");
  const customTargetMb = document.getElementById("customTargetMb");

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  const dropzoneActionBtn = document.getElementById("dropzoneActionBtn");
  const uploadError = document.getElementById("uploadError");
  const uploadedFileCard = document.getElementById("uploadedFileCard");
  const uploadedFileName = document.getElementById("uploadedFileName");
  const uploadedFileSize = document.getElementById("uploadedFileSize");
  const uploadedFileType = document.getElementById("uploadedFileType");
  const uploadedFilePages = document.getElementById("uploadedFilePages");
  const uploadedFilePagesCount = document.getElementById("uploadedFilePagesCount");
  const removeFileBtn = document.getElementById("removeFileBtn");
  const loadingIndicator = document.getElementById("loadingIndicator");
  const loadingText = document.getElementById("loadingText");
  const consentGate = document.getElementById("consentGate");
  const consentCheckbox = document.getElementById("consentCheckbox");
  const consentError = document.getElementById("consentError");
  const confirmConsentBtn = document.getElementById("confirmConsentBtn");
  const consentStorageKey = "fnskit_check_consent";

  const resultsSection = document.getElementById("resultsSection");
  const summaryBanner = document.getElementById("summaryBanner");
  const summaryStatusBadge = document.getElementById("summaryStatusBadge");
  const summaryTitle = document.getElementById("summaryTitle");
  const summaryDesc = document.getElementById("summaryDesc");
  const summaryCounts = document.getElementById("summaryCounts");
  const checklistContainer = document.getElementById("checklistContainer");
  const pagesDetails = document.getElementById("pagesDetails");
  const pagesTableBody = document.getElementById("pagesTableBody");
  const fixInEditorBtn = document.getElementById("fixInEditorBtn");
  const checkAnotherBtn = document.getElementById("checkAnotherBtn");

  const presets = {
    fns: { format: "tiff", dpi: "300", color: "bw", compression: "g4", targetMb: "60" },
    vat: { format: "pdf", dpi: "300", color: "gray", compression: "lzw", targetMb: "20" },
    registration: { format: "tiff", dpi: "300", color: "bw", compression: "g4", targetMb: "60" },
    bank: { format: "pdf", dpi: "300", color: "color", compression: "jpeg", targetMb: "15" },
    court: { format: "pdfa", dpi: "300", color: "bw", compression: "jpeg", targetMb: "30" }
  };

  function selectProfile(profileKey) {
    if (!profiles[profileKey]) return;
    activeProfile = profileKey;
    const info = profiles[profileKey];

    profileButtons.forEach(btn => {
      const match = btn.dataset.profile === profileKey;
      btn.setAttribute("aria-pressed", match ? "true" : "false");
    });

    if (profileSummary) profileSummary.style.backgroundColor = info.background;
    if (profileTitle) profileTitle.textContent = info.title;
    if (profileBadge) profileBadge.textContent = info.badge;
    if (profileNorm) profileNorm.textContent = info.norm;

    if (customSettings) {
      if (profileKey === "custom") {
        customSettings.classList.remove("hidden");
      } else {
        customSettings.classList.add("hidden");
      }
    }

    // Если файл уже выбран, запускаем повторную проверку с новым профилем
    if (currentFile && !isChecking) {
      performInspection(currentFile);
    }
  }

  customPreset?.addEventListener("change", () => {
    const val = customPreset.value;
    const preset = presets[val];
    if (!preset) return;
    if (customFormat) customFormat.value = preset.format;
    if (customDpi) customDpi.value = preset.dpi;
    if (customColor) customColor.value = preset.color;
    if (customCompression) customCompression.value = preset.compression;
    if (customTargetMb) customTargetMb.value = preset.targetMb;
    if (currentFile && !isChecking) performInspection(currentFile);
  });

  profileButtons.forEach(btn => {
    btn.addEventListener("click", () => selectProfile(btn.dataset.profile));
  });

  [customFormat, customDpi, customColor, customCompression, customTargetMb].forEach(el => {
    el?.addEventListener("change", () => {
      if (currentFile && !isChecking && activeProfile === "custom") {
        performInspection(currentFile);
      }
    });
  });

  function isConsentGiven() {
    try {
      return localStorage.getItem(consentStorageKey) === consentVersion;
    } catch {
      return false;
    }
  }

  function updateConsentUI() {
    const consented = isConsentGiven();
    if (consented) {
      if (consentGate) consentGate.classList.add("hidden");
      if (dropzone) {
        dropzone.classList.remove("dropzone-disabled", "cursor-not-allowed", "select-none");
        dropzone.classList.add("dropzone-ready");
      }
      if (fileInput) {
        fileInput.disabled = isChecking;
        fileInput.classList.remove("cursor-not-allowed");
      }
    } else {
      if (consentGate) consentGate.classList.remove("hidden");
      if (dropzone) {
        dropzone.classList.remove("dropzone-ready");
        dropzone.classList.add("dropzone-disabled");
      }
      if (fileInput) {
        fileInput.disabled = true;
      }
      if (consentCheckbox) consentCheckbox.checked = false;
    }
  }

  consentCheckbox?.addEventListener("change", () => {
    if (consentError) consentError.classList.add("hidden");
  });

  confirmConsentBtn?.addEventListener("click", () => {
    if (!consentCheckbox?.checked) {
      if (consentError) consentError.classList.remove("hidden");
      consentCheckbox?.focus();
      return;
    }
    if (consentError) consentError.classList.add("hidden");
    try {
      localStorage.setItem(consentStorageKey, consentVersion);
    } catch {}
    updateConsentUI();
  });

  function showError(msg) {
    if (!uploadError) return;
    if (msg) {
      uploadError.textContent = msg;
      uploadError.classList.remove("hidden");
    } else {
      uploadError.textContent = "";
      uploadError.classList.add("hidden");
    }
  }

  function setLoading(loading, message = "Проверяем структуру документа…") {
    isChecking = loading;
    if (loadingIndicator) {
      loadingIndicator.classList.toggle("hidden", !loading);
      loadingIndicator.classList.toggle("flex", loading);
    }
    if (loadingText) loadingText.textContent = message;
    if (fileInput) fileInput.disabled = loading;
    profileButtons.forEach(btn => { btn.disabled = loading; });
  }

  async function handleFileSelected(file) {
    showError("");
    if (!file) return;

    const ext = file.name.split(".").pop()?.toLowerCase();
    if (!allowedExtensions.has(ext)) {
      showError(`Формат «.${ext || "неизвестно"}» не поддерживается. Загрузите PDF или TIFF.`);
      return;
    }

    if (file.size > maxFileSize) {
      showError("Размер файла превышает 100 МБ. Уменьшите объём файла перед проверкой.");
      return;
    }

    if (!isConsentGiven()) {
      showError("Подтвердите согласие на обработку данных ниже, чтобы разблокировать загрузку.");
      updateConsentUI();
      consentCheckbox?.focus();
      return;
    }

    currentFile = file;
    updateUploadedFileCard(file);
    await performInspection(file);
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 Б";
    const k = 1024;
    const sizes = ["Б", "КБ", "МБ", "ГБ"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    const val = (bytes / Math.pow(k, i)).toFixed(i === 0 ? 0 : 1);
    return `${val} ${sizes[i]}`;
  }

  function updateUploadedFileCard(file, pageCount = null) {
    if (!uploadedFileCard || !file) return;
    const ext = (file.name.split(".").pop() || "").toUpperCase();
    if (uploadedFileName) uploadedFileName.textContent = file.name;
    if (uploadedFileSize) uploadedFileSize.textContent = formatBytes(file.size);
    if (uploadedFileType) uploadedFileType.textContent = ext;

    if (uploadedFilePages && uploadedFilePagesCount) {
      if (pageCount && pageCount > 0) {
        uploadedFilePagesCount.textContent = pageCount;
        uploadedFilePages.classList.remove("hidden");
      } else {
        uploadedFilePages.classList.add("hidden");
      }
    }

    uploadedFileCard.classList.remove("hidden");
    uploadedFileCard.classList.add("flex");
  }

  function clearUploadedFile() {
    currentFile = null;
    if (fileInput) fileInput.value = "";
    if (uploadedFileCard) {
      uploadedFileCard.classList.add("hidden");
      uploadedFileCard.classList.remove("flex");
    }
    if (resultsSection) resultsSection.classList.add("hidden");
    showError("");
  }

  removeFileBtn?.addEventListener("click", () => {
    clearUploadedFile();
  });

  async function performInspection(file) {
    setLoading(true, `Анализируем ${file.name}…`);
    showError("");
    if (resultsSection) resultsSection.classList.add("hidden");

    const formData = new FormData();
    formData.append("profile", activeProfile);
    formData.append("file", file);
    formData.append("personal_data_consent", "true");
    formData.append("consent_version", consentVersion);

    if (activeProfile === "custom") {
      if (customDpi?.value) formData.append("dpi", customDpi.value);
      if (customColor?.value) formData.append("color_mode", customColor.value);
      if (customCompression?.value) formData.append("compression", customCompression.value);
      if (customFormat?.value) formData.append("custom_format", customFormat.value);
      if (customTargetMb?.value) formData.append("target_max_mb", customTargetMb.value);
    }

    try {
      const response = await fetch(`${processorApiBase}/v1/inspect`, {
        method: "POST",
        body: formData
      });

      if (!response.ok) {
        let errorDetail = "Не удалось проверить документ.";
        try {
          const errData = await response.json();
          errorDetail = errData.detail || errorDetail;
        } catch {
          // keep fallback
        }
        throw new Error(errorDetail);
      }

      const report = await response.json();
      if (currentFile && report.page_count) {
        updateUploadedFileCard(currentFile, report.page_count);
      }
      renderReport(report);
    } catch (err) {
      showError(err.message || "Ошибка проверки документа на сервере.");
    } finally {
      setLoading(false);
    }
  }

  function renderReport(report) {
    if (!resultsSection) return;
    resultsSection.classList.remove("hidden");

    const sum = report.summary;
    let bannerBg = "bg-lime text-ink";
    let badgeText = "✓ ГОТОВ К ОТПРАВКЕ";
    let badgeBg = "bg-ink text-white";

    if (sum.status === "error") {
      bannerBg = "bg-red-50 text-red-900 border border-red-200";
      badgeText = "✕ НЕ СООТВЕТСТВУЕТ ТРЕБОВАНИЯМ";
      badgeBg = "bg-red-700 text-white";
    } else if (sum.status === "warning") {
      bannerBg = "bg-[#FFDFA8] text-ink";
      badgeText = "⚠️ ЕСТЬ ЗАМЕЧАНИЯ";
      badgeBg = "bg-ink text-white";
    }

    if (summaryBanner) {
      summaryBanner.className = `rounded-3xl p-6 sm:p-8 transition-colors ${bannerBg}`;
    }
    if (summaryStatusBadge) {
      summaryStatusBadge.textContent = badgeText;
      summaryStatusBadge.className = `rounded-full px-3 py-1 text-[11px] font-extrabold uppercase tracking-wider ${badgeBg}`;
    }
    if (summaryTitle) summaryTitle.textContent = sum.title;
    if (summaryDesc) summaryDesc.textContent = sum.description;

    if (summaryCounts) {
      summaryCounts.innerHTML = `
        <span class="inline-flex items-center gap-1.5 rounded-full bg-white/80 px-3 py-1 font-bold text-xs">
          <span class="h-2 w-2 rounded-full bg-lime"></span> Пройдено: ${sum.passed_count}
        </span>
        ${sum.warning_count ? `
          <span class="inline-flex items-center gap-1.5 rounded-full bg-white/80 px-3 py-1 font-bold text-xs">
            <span class="h-2 w-2 rounded-full bg-amber-500"></span> Предупреждений: ${sum.warning_count}
          </span>
        ` : ""}
        ${sum.error_count ? `
          <span class="inline-flex items-center gap-1.5 rounded-full bg-white/80 px-3 py-1 font-bold text-xs text-red-700">
            <span class="h-2 w-2 rounded-full bg-red-600"></span> Ошибок: ${sum.error_count}
          </span>
        ` : ""}
      `;
    }

    // Render Checklist
    if (checklistContainer) {
      checklistContainer.innerHTML = report.checks.map(check => {
        let icon = `<span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-lime text-ink font-black text-sm">✓</span>`;
        let cardBorder = "border-ink/10 bg-white";
        let statusBadge = `<span class="rounded-full bg-lime/30 px-2.5 py-0.5 text-[10px] font-extrabold text-ink uppercase">В норме</span>`;

        if (check.status === "error") {
          icon = `<span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-red-600 text-white font-black text-sm">✕</span>`;
          cardBorder = "border-red-300 bg-red-50/50";
          statusBadge = `<span class="rounded-full bg-red-600 px-2.5 py-0.5 text-[10px] font-extrabold text-white uppercase">Ошибка</span>`;
        } else if (check.status === "warning") {
          icon = `<span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-amber-500 text-white font-black text-sm">!</span>`;
          cardBorder = "border-amber-300 bg-amber-50/40";
          statusBadge = `<span class="rounded-full bg-amber-500 px-2.5 py-0.5 text-[10px] font-extrabold text-white uppercase">Внимание</span>`;
        }

        return `
          <div class="rounded-2xl border ${cardBorder} p-5 transition-shadow hover:shadow-card">
            <div class="flex items-start justify-between gap-4">
              <div class="flex items-start gap-3">
                ${icon}
                <div>
                  <h3 class="text-base font-extrabold text-ink">${escapeHtml(check.title)}</h3>
                  <p class="mt-1 text-xs text-ink/65 leading-relaxed">${escapeHtml(check.details)}</p>
                </div>
              </div>
              <div class="shrink-0">${statusBadge}</div>
            </div>
            <div class="mt-4 grid grid-cols-2 gap-3 rounded-xl bg-ink/[0.03] p-3 text-xs">
              <div>
                <span class="block text-ink/40 font-bold uppercase text-[9px] tracking-wider">В документе</span>
                <span class="font-extrabold text-ink">${escapeHtml(check.actual)}</span>
              </div>
              <div>
                <span class="block text-ink/40 font-bold uppercase text-[9px] tracking-wider">Требование</span>
                <span class="font-semibold text-ink/75">${escapeHtml(check.expected)}</span>
              </div>
            </div>
          </div>
        `;
      }).join("");
    }

    // Render Page-by-page table
    if (pagesTableBody && report.pages?.length) {
      pagesTableBody.innerHTML = report.pages.map(p => {
        const hasIssue = p.issues && p.issues.length > 0;
        const rowClass = hasIssue ? "bg-red-50/60" : "";
        const statusIcon = hasIssue
          ? `<span class="inline-flex items-center gap-1 text-xs font-extrabold text-red-600">✕ Замечание</span>`
          : `<span class="inline-flex items-center gap-1 text-xs font-bold text-ink/60">✓ Ок</span>`;

        return `
          <tr class="border-b border-ink/5 ${rowClass} text-xs">
            <td class="p-3 font-extrabold text-ink">Стр. ${p.page}</td>
            <td class="p-3 font-semibold">${p.dpi_x} × ${p.dpi_y} DPI</td>
            <td class="p-3 font-semibold">${escapeHtml(p.color)} (${p.bits} бит)</td>
            <td class="p-3 font-semibold">${p.width_mm} × ${p.height_mm} мм</td>
            <td class="p-3">
              ${hasIssue ? `<span class="font-bold text-red-700">${escapeHtml(p.issues.join(", "))}</span>` : `<span class="text-ink/45">Параметры в норме</span>`}
            </td>
            <td class="p-3 text-right">${statusIcon}</td>
          </tr>
        `;
      }).join("");
      pagesDetails?.classList.remove("hidden");
    } else {
      pagesDetails?.classList.add("hidden");
    }

    // Configure "Fix in editor" CTA button
    if (fixInEditorBtn) {
      const targetEditorUrl = profiles[activeProfile]?.editorUrl || "/editor-dlya-fns";
      fixInEditorBtn.onclick = async () => {
        await transferFileToEditor(targetEditorUrl);
      };
    }

    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function transferFileToEditor(editorUrl) {
    if (!currentFile) {
      window.location.href = editorUrl;
      return;
    }

    try {
      const database = await new Promise((resolve, reject) => {
        const req = indexedDB.open(fileStoreDatabase, 1);
        req.onupgradeneeded = () => {
          if (!req.result.objectStoreNames.contains(fileStoreName)) {
            req.result.createObjectStore(fileStoreName, { keyPath: "id" });
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      });

      await new Promise((resolve, reject) => {
        const tx = database.transaction(fileStoreName, "readwrite");
        tx.objectStore(fileStoreName).put({
          id: "check-handoff",
          file: currentFile,
          profile: activeProfile,
          timestamp: Date.now()
        });
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error);
      });
      database.close();
    } catch (e) {
      console.warn("Не удалось сохранить файл в IndexedDB:", e);
    }

    window.location.href = editorUrl;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Dropzone drag-and-drop
  if (dropzone && fileInput) {
    ["dragenter", "dragover"].forEach(type => {
      dropzone.addEventListener(type, e => {
        e.preventDefault();
        dropzone.classList.add("drop-active");
      });
    });

    ["dragleave", "drop"].forEach(type => {
      dropzone.addEventListener(type, e => {
        e.preventDefault();
        dropzone.classList.remove("drop-active");
      });
    });

    dropzone.addEventListener("drop", e => {
      const files = e.dataTransfer?.files;
      if (files && files.length > 0) {
        handleFileSelected(files[0]);
      }
    });

    fileInput.addEventListener("change", e => {
      const files = e.target.files;
      if (files && files.length > 0) {
        handleFileSelected(files[0]);
      }
    });
  }

  checkAnotherBtn?.addEventListener("click", () => {
    clearUploadedFile();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  // Инициализация
  selectProfile("vat");
  updateConsentUI();
})();
