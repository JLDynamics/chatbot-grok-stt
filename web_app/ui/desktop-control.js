// @ts-check

/**
 * Read the current Desktop control preference, migrating the old read_screen
 * name without letting it override an explicit modern value.
 * @param {Record<string, unknown>} stored
 */
export function readDesktopControlPreference(stored) {
  return Boolean(stored.desktop_control ?? stored.read_screen ?? false);
}

/** @param {boolean} preferred @param {boolean} serverAvailable */
export function desktopControlToolEnabled(preferred, serverAvailable) {
  return preferred && serverAvailable;
}

/**
 * Add the model tool only when both the browser preference and server
 * capability allow it.
 * @template T
 * @param {T[]} definitions
 * @param {T} tool
 * @param {boolean} preferred
 * @param {boolean} serverAvailable
 */
export function addDesktopControlTool(definitions, tool, preferred, serverAvailable) {
  if (desktopControlToolEnabled(preferred, serverAvailable)) definitions.push(tool);
}

/**
 * Bind the visible checkbox to the persisted preference owned by main.js.
 * @param {{
 *   input: HTMLInputElement,
 *   row: HTMLElement,
 *   hint: HTMLElement,
 *   getPreferred: () => boolean,
 *   getServerAvailable: () => boolean,
 *   onPreferenceChange: (enabled: boolean) => void,
 * }} options
 */
export function bindDesktopControlToggle(options) {
  const sync = () => {
    const available = options.getServerAvailable();
    const preferred = options.getPreferred();
    options.input.disabled = !available;
    options.input.checked = desktopControlToolEnabled(preferred, available);
    options.row.classList.toggle("disabled", !available);
    options.hint.textContent = !available
      ? "Unavailable. Desktop control is disabled on the server or desktop-harness is not installed."
      : preferred
        ? "On. The assistant can act or capture the screen only when you explicitly ask."
        : "Off. Needs desktop-harness plus macOS Accessibility and Screen Recording permissions.";
  };

  options.input.addEventListener("change", () => {
    if (!options.getServerAvailable()) {
      sync();
      return;
    }
    options.onPreferenceChange(options.input.checked);
    sync();
  });

  sync();
  return { sync };
}
