// События для статистики Umami (stats.sherhn.space).
//
// Сам скрипт Umami подключён в <head> страниц и считает просмотры; здесь только
// обёртка для событий: window.FNSKIT_STATS.track(имя, данные). Обёртка молчит,
// если счётчик закрыт блокировщиком или не успел загрузиться, и никогда не
// бросает ошибку в код страницы.
//
// В данные идут числа и короткие коды: профиль, формат, число страниц, статус
// ответа. Имён файлов, текстов ошибок, адресов почты и содержимого документов
// в событиях быть не должно — этого требует политика ПДн.
(() => {
  // Скрипт Umami подключён с defer, и действие пользователя может прийти раньше,
  // чем он запустился (например, файл из проверки подхватывается при загрузке).
  // Такие события ждут конца загрузки страницы и после него отбрасываются.
  const pending = [];

  function ready() {
    return typeof window.umami?.track === "function";
  }

  function clean(data) {
    if (!data) return undefined;
    const entries = Object.entries(data).filter(([, value]) => value !== undefined && value !== null && value !== "");
    return entries.length ? Object.fromEntries(entries) : undefined;
  }

  function deliver(name, data) {
    try {
      if (data) window.umami.track(name, data);
      else window.umami.track(name);
    } catch {
      // Счётчик не должен ломать страницу.
    }
  }

  window.addEventListener("load", () => {
    while (pending.length && ready()) deliver(...pending.shift());
    pending.length = 0;
  });

  window.FNSKIT_STATS = {
    track(name, data) {
      const payload = clean(data);
      if (ready()) deliver(name, payload);
      else if (document.readyState !== "complete") pending.push([name, payload]);
    },
    // Размер в мегабайтах с одним знаком: точный размер файла о человеке ничего не говорит.
    megabytes(bytes) {
      return Math.round(bytes / 104857.6) / 10;
    },
    // Расширение без имени файла: «pdf», «tiff».
    extension(filename) {
      return String(filename || "").split(".").pop().toLowerCase().slice(0, 8) || undefined;
    }
  };
})();
