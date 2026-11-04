/** Light, dark, or the system's choice (the default). Kept per device; a choice sets data-theme on the root, which the
 * stylesheet's tokens follow. */
export type Theme = "system" | "light" | "dark";
const KEY = "limoon.theme";

export function savedTheme(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
  try {
    if (theme === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch { /* storage unavailable: the choice lasts until the app closes */ }
}
