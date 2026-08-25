(() => {
  const key = "destiny-default-character";
  const header = document.querySelector("[data-default-character]");
  if (!header) return;

  const validValues = new Set(["0", "1", "2"]);
  const readSavedCharacter = () => {
    const cookie = document.cookie
      .split("; ")
      .find((entry) => entry.startsWith(`${key}=`));
    const cookieValue = cookie ? decodeURIComponent(cookie.split("=")[1]) : "";
    if (validValues.has(cookieValue)) return cookieValue;

    try {
      const localValue = localStorage.getItem(key);
      if (validValues.has(localValue)) return localValue;
    } catch {
      // Browser storage may be unavailable; the cookie still works.
    }
    return "0";
  };

  const setDefaultCharacter = (value) => {
    if (!validValues.has(value)) return;
    window.destinyDefaultCharacter = value;
    try {
      localStorage.setItem(key, value);
    } catch {
      // The cookie remains the persistent fallback.
    }
    document.cookie = `${key}=${encodeURIComponent(value)}; path=/; max-age=31536000; SameSite=Lax`;
  };

  const savedCharacter = readSavedCharacter();
  header.value = savedCharacter;
  setDefaultCharacter(savedCharacter);
  document.dispatchEvent(
    new CustomEvent("destiny-default-character-changed", {
      detail: savedCharacter,
    })
  );

  header.addEventListener("change", () => {
    setDefaultCharacter(header.value);
    document.dispatchEvent(
      new CustomEvent("destiny-default-character-changed", {
        detail: header.value,
      })
    );
  });
})();
