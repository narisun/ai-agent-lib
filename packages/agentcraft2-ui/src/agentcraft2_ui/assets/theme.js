// Use Chainlit's persisted preference on server-rendered pages too.
// This adapter targets the pinned release; chat owns its own theme controls.
(() => {
  const key = "vite-ui-theme";
  const media = matchMedia("(prefers-color-scheme: dark)");
  let preference = "light";
  function read() {
    try { preference = localStorage.getItem(key) || "light"; } catch { /* Storage may be disabled. */ }
    if (!["light", "dark", "system"].includes(preference)) preference = "light";
  }
  function apply() {
    const dark = preference === "dark" || (preference === "system" && media.matches);
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.classList.toggle("light", !dark);
    const picker = document.querySelector("[data-theme]");
    if (picker) picker.value = preference;
  }
  read(); apply();
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.querySelector("[data-theme]")?.addEventListener("change", event => {
      preference = event.target.value;
      try { localStorage.setItem(key, preference); } catch { /* Keep the page usable. */ }
      apply();
    });
  }, {once: true});
  media.addEventListener("change", apply);
  addEventListener("storage", event => { if (event.key === key) { read(); apply(); } });
})();
