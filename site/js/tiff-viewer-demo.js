(() => {
  const $ = (id) => document.getElementById(id);
  const input = $("fileInput");
  const dropzone = $("dropzone");
  const status = $("status");
  const viewer = $("viewer");
  const canvas = $("canvas");
  const ctx = canvas.getContext("2d", { alpha: false });
  const viewport = $("viewport");
  const stage = $("stage");
  const rendering = $("rendering");
  const pageInput = $("pageInput");
  const pageCount = $("pageCount");
  const zoomValue = $("zoomValue");
  const thumbnails = $("thumbnails");
  const thumbnailStatus = $("thumbnailStatus");
  let documentData = null;
  let currentPage = 0;
  let zoom = 1;
  let renderSequence = 0;
  let thumbnailSequence = 0;

  async function restoreLatestResult() {
    try {
      const database = await new Promise((resolve, reject) => {
        const request = indexedDB.open("fnskit-viewer", 1);
        request.onupgradeneeded = () => request.result.createObjectStore("documents", { keyPath: "id" });
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      const stored = await new Promise((resolve, reject) => {
        const request = database.transaction("documents", "readonly").objectStore("documents").get("latest-result");
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      database.close();
      if (stored?.file instanceof Blob) await loadFile(new File([stored.file], stored.filename || "result.tiff", { type: "image/tiff" }));
    } catch {
      // The manual file picker remains available when IndexedDB is unavailable.
    }
  }

  const formatSize = (bytes) => `${(bytes / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`;
  const setStatus = (message, error = false) => {
    status.textContent = message;
    status.className = `mt-1 text-xs ${error ? "text-red-600" : "text-slate-500"}`;
  };

  async function loadFile(file) {
    if (!file || !/\.tiff?$/i.test(file.name)) {
      setStatus("Нужен файл с расширением .tif или .tiff.", true);
      return;
    }
    if (!window.UTIF) {
      setStatus("Не загрузилась библиотека декодирования TIFF.", true);
      return;
    }
    try {
      setStatus("Читаем структуру TIFF локально…");
      const buffer = await file.arrayBuffer();
      const frames = window.UTIF.decode(buffer);
      if (!frames.length) throw new Error("В файле не найдено ни одной страницы.");
      documentData = { buffer, frames, file };
      currentPage = 0;
      $("fileName").textContent = file.name;
      $("fileMeta").textContent = `${formatSize(file.size)} · ${frames.length} ${pluralPages(frames.length)}`;
      pageInput.max = frames.length;
      pageCount.textContent = `из ${frames.length}`;
      viewer.classList.remove("hidden");
      await renderPage();
      renderThumbnails();
      setStatus("Файл открыт локально. Сеть не использовалась.");
    } catch (error) {
      console.error(error);
      setStatus(`Не удалось открыть TIFF: ${error.message || "неподдерживаемое сжатие или повреждённый файл"}`, true);
    }
  }

  function pluralPages(number) {
    const rest = number % 100;
    if (rest >= 11 && rest <= 14) return "страниц";
    return ["страница", "страницы", "страниц"][[2, 0, 1, 1, 1, 2][Math.min(number % 10, 5)]];
  }

  async function renderPage() {
    if (!documentData) return;
    const sequence = ++renderSequence;
    rendering.classList.remove("hidden");
    await new Promise(requestAnimationFrame);
    try {
      const frame = documentData.frames[currentPage];
      window.UTIF.decodeImage(documentData.buffer, frame);
      const rgba = window.UTIF.toRGBA8(frame);
      if (sequence !== renderSequence) return;
      canvas.width = frame.width;
      canvas.height = frame.height;
      ctx.putImageData(new ImageData(new Uint8ClampedArray(rgba), frame.width, frame.height), 0, 0);
      pageInput.value = currentPage + 1;
      $("prev").disabled = currentPage === 0;
      $("next").disabled = currentPage === documentData.frames.length - 1;
      syncActiveThumbnail();
      applyZoom(true);
    } catch (error) {
      setStatus(`Не удалось отрисовать страницу: ${error.message || "этот вариант TIFF пока не поддерживается"}`, true);
    } finally {
      if (sequence === renderSequence) rendering.classList.add("hidden");
    }
  }

  function syncActiveThumbnail() {
    thumbnails.querySelectorAll("[data-page]").forEach((item) => {
      const isActive = Number(item.dataset.page) === currentPage;
      item.classList.toggle("border-violet", isActive);
      item.classList.toggle("ring-2", isActive);
      item.classList.toggle("ring-violet/25", isActive);
      item.setAttribute("aria-current", isActive ? "page" : "false");
    });
  }

  async function renderThumbnails() {
    const sequence = ++thumbnailSequence;
    thumbnails.replaceChildren();
    thumbnailStatus.textContent = "Готовим…";
    const thumbnailCanvas = document.createElement("canvas");
    const thumbnailContext = thumbnailCanvas.getContext("2d", { alpha: false });
    for (let index = 0; index < documentData.frames.length; index += 1) {
      if (sequence !== thumbnailSequence) return;
      const frame = documentData.frames[index];
      try {
        window.UTIF.decodeImage(documentData.buffer, frame);
        const rgba = window.UTIF.toRGBA8(frame);
        const source = document.createElement("canvas");
        source.width = frame.width;
        source.height = frame.height;
        source.getContext("2d", { alpha: false }).putImageData(
          new ImageData(new Uint8ClampedArray(rgba), frame.width, frame.height), 0, 0
        );
        const scale = Math.min(1, 132 / Math.max(frame.width, frame.height));
        thumbnailCanvas.width = Math.max(1, Math.round(frame.width * scale));
        thumbnailCanvas.height = Math.max(1, Math.round(frame.height * scale));
        thumbnailContext.fillStyle = "#ffffff";
        thumbnailContext.fillRect(0, 0, thumbnailCanvas.width, thumbnailCanvas.height);
        thumbnailContext.drawImage(source, 0, 0, thumbnailCanvas.width, thumbnailCanvas.height);
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.page = String(index);
        button.className = "shrink-0 rounded-xl border border-ink/10 bg-white p-2 text-left transition hover:border-violet lg:w-full";
        button.setAttribute("aria-label", `Открыть страницу ${index + 1}`);
        button.innerHTML = `<img class="mx-auto block max-h-32 max-w-[132px] object-contain" alt="Миниатюра страницы ${index + 1}"><span class="mt-1 block text-center text-[10px] font-extrabold text-ink/50">${index + 1}</span>`;
        button.querySelector("img").src = thumbnailCanvas.toDataURL("image/jpeg", .82);
        button.addEventListener("click", () => changePage(index));
        thumbnails.append(button);
      } catch {
        // A broken page is still represented in the navigation rail.
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.page = String(index);
        button.className = "shrink-0 rounded-xl border border-ink/10 bg-white p-3 text-center text-[10px] font-extrabold text-ink/45 transition hover:border-violet lg:w-full";
        button.textContent = `Страница ${index + 1}`;
        button.addEventListener("click", () => changePage(index));
        thumbnails.append(button);
      }
      thumbnailStatus.textContent = `${index + 1}/${documentData.frames.length}`;
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    if (sequence === thumbnailSequence) {
      thumbnailStatus.textContent = `${documentData.frames.length}`;
      syncActiveThumbnail();
    }
  }

  function applyZoom(center = false) {
    stage.style.width = `${canvas.width * zoom}px`;
    stage.style.height = `${canvas.height * zoom}px`;
    canvas.style.width = `${canvas.width * zoom}px`;
    canvas.style.height = `${canvas.height * zoom}px`;
    zoomValue.textContent = `${Math.round(zoom * 100)}%`;
    if (center) viewport.scrollTo({ left: 0, top: 0 });
  }

  function changePage(nextPage) {
    if (!documentData) return;
    currentPage = Math.max(0, Math.min(documentData.frames.length - 1, nextPage));
    renderPage();
  }

  $("prev").addEventListener("click", () => changePage(currentPage - 1));
  $("next").addEventListener("click", () => changePage(currentPage + 1));
  pageInput.addEventListener("change", () => changePage(Number(pageInput.value) - 1));
  $("zoomIn").addEventListener("click", () => { zoom = Math.min(4, zoom * 1.25); applyZoom(); });
  $("zoomOut").addEventListener("click", () => { zoom = Math.max(.1, zoom / 1.25); applyZoom(); });
  $("fit").addEventListener("click", () => {
    zoom = Math.min(1, Math.max(.1, (viewport.clientWidth - 40) / canvas.width));
    applyZoom();
  });
  input.addEventListener("change", () => loadFile(input.files[0]));
  restoreLatestResult();

  ["dragenter", "dragover"].forEach((event) => dropzone.addEventListener(event, (e) => { e.preventDefault(); dropzone.classList.add("drop-active"); }));
  ["dragleave", "drop"].forEach((event) => dropzone.addEventListener(event, (e) => { e.preventDefault(); dropzone.classList.remove("drop-active"); }));
  dropzone.addEventListener("drop", (event) => loadFile(event.dataTransfer.files[0]));

  document.addEventListener("keydown", (event) => {
    if (!documentData || event.target === pageInput) return;
    if (event.key === "ArrowLeft") changePage(currentPage - 1);
    if (event.key === "ArrowRight") changePage(currentPage + 1);
    if (event.key === "+" || event.key === "=") { zoom = Math.min(4, zoom * 1.25); applyZoom(); }
    if (event.key === "-") { zoom = Math.max(.1, zoom / 1.25); applyZoom(); }
  });

  viewport.addEventListener("wheel", (event) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    zoom = Math.max(.1, Math.min(4, zoom * (event.deltaY > 0 ? .9 : 1.1)));
    applyZoom();
  }, { passive: false });

  let drag = null;
  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    drag = { x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("dragging");
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!drag) return;
    viewport.scrollLeft = drag.left - (event.clientX - drag.x);
    viewport.scrollTop = drag.top - (event.clientY - drag.y);
  });
  ["pointerup", "pointercancel"].forEach((event) => viewport.addEventListener(event, () => { drag = null; viewport.classList.remove("dragging"); }));
})();
