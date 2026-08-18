(() => {
  const themeToggle = document.getElementById("theme-toggle");
  const systemTheme = () => window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
  const savedTheme = () => {
    try { return localStorage.getItem("mergerail-theme"); }
    catch { return null; }
  };
  const currentTheme = () => {
    const manual = document.documentElement.dataset.theme;
    return manual === "light" || manual === "dark" ? manual : systemTheme();
  };
  const updateThemeToggle = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    themeToggle.setAttribute("aria-label", `Use ${next} theme`);
    themeToggle.title = `Use ${next} theme`;
  };
  const saved = savedTheme();
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  themeToggle.onclick = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("mergerail-theme", next); } catch {}
    updateThemeToggle();
  };
  updateThemeToggle();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if (!document.documentElement.dataset.theme) updateThemeToggle();
  });
})();
