/* Drives the real dashboard UI in jsdom against a real running server.
 *
 *   node tests/ui/dashboard.ui.js <dashboard-key> [base-url]
 *
 * The base URL defaults to http://127.0.0.1:8790; set LOFI_DASHBOARD_URL to
 * point at another instance. Start the server first:
 *
 *   python bot.py --demo --dashboard-token <key> --dashboard-port 8790
 *
 * Asserts on rendered DOM text rather than CSS, so markup that loses
 * information fails the run. Exits non-zero on the first failure.
 */
const { JSDOM, VirtualConsole } = require("jsdom");

const BASE = (process.argv[3] || process.env.LOFI_DASHBOARD_URL || "http://127.0.0.1:8790").replace(/\/$/, "");
const KEY = process.argv[2];
if (!KEY) {
  console.error("usage: node tests/ui/dashboard.ui.js <dashboard-key> [base-url]");
  process.exit(2);
}

let cookieJar = new Map();
async function fakeFetch(url, init = {}) {
  const full = String(url).startsWith("http") ? String(url) : BASE + url;
  const headers = new Headers(init.headers || {});
  if (cookieJar.size) headers.set("Cookie", [...cookieJar].map(([k, v]) => `${k}=${v}`).join("; "));
  const response = await fetch(full, { ...init, headers });
  for (const raw of response.headers.getSetCookie ? response.headers.getSetCookie() : []) {
    const [pair] = raw.split(";");
    const index = pair.indexOf("=");
    const name = pair.slice(0, index), value = pair.slice(index + 1);
    if (value) cookieJar.set(name, value); else cookieJar.delete(name);
  }
  return response;
}

const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (error) => errors.push(`jsdomError: ${error.message}`));
virtualConsole.on("error", (...args) => errors.push(`console.error: ${args.join(" ")}`));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function text(window, selector) {
  const node = window.document.querySelector(selector);
  return node ? node.textContent.replace(/\s+/g, " ").trim() : null;
}
function count(window, selector) {
  return window.document.querySelectorAll(selector).length;
}
function visible(window, selector) {
  const node = window.document.querySelector(selector);
  return Boolean(node) && !node.classList.contains("hidden");
}

(async () => {
  const htmlResponse = await fetch(`${BASE}/`);
  const html = await htmlResponse.text();
  const dom = new JSDOM(html, {
    url: `${BASE}/`,
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      window.fetch = fakeFetch;
      window.confirm = () => true;
      window.addEventListener("error", (event) => errors.push(`window.error: ${event.message}`));
    },
  });
  const { window } = dom;
  const document = window.document;

  await sleep(600);
  const checks = [];
  const check = (label, condition, detail = "") =>
    checks.push({ label, ok: Boolean(condition), detail });

  check("login view shown first", visible(window, "#login-view"));
  check("app view hidden before login", !visible(window, "#app-view"));

  // --- login ---
  document.getElementById("dashboard-key").value = "definitely-wrong";
  document.getElementById("login-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await sleep(700);
  check("wrong key shows an error", visible(window, "#login-error"), text(window, "#login-error"));
  check("still on the login view", visible(window, "#login-view"));

  document.getElementById("dashboard-key").value = KEY;
  document.getElementById("login-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await sleep(1500);
  check("app view shown after login", visible(window, "#app-view"));
  check("login view hidden after login", !visible(window, "#login-view"));

  // --- overview ---
  check("nav rendered", count(window, ".nav-btn") === 7, `${count(window, ".nav-btn")} buttons`);
  check("stat cards rendered", count(window, "#stat-cards .stat-card") === 4);
  check("guild rows rendered", count(window, "#guild-list .guild-row") === 3, `${count(window, "#guild-list .guild-row")} rows`);
  check("guild select filled", count(window, "#guild-select option") === 3);
  check("quick actions rendered", count(window, "#quick-actions .quick-action") === 4);
  check("top tracks rendered", count(window, "#top-tracks .queue-row") >= 3, text(window, "#top-tracks"));
  check("activity feed rendered", count(window, "#activity-feed .feed-row") >= 5);
  check("demo banner shown", visible(window, "#demo-banner"));
  check("bot badge updated", text(window, "#conn-state") === "Connected", text(window, "#conn-sub"));
  check("stat value is a number", /\d/.test(text(window, "#stat-cards .stat-value")), text(window, "#stat-cards .stat-value"));

  // --- navigation helper ---
  async function goto(pageId) {
    const button = [...document.querySelectorAll(".nav-btn")].find((node) => node.dataset.page === pageId);
    button.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await sleep(1400);
  }

  // --- player ---
  await goto("player");
  check("player page active", document.querySelector('.page[data-page="player"]').classList.contains("active"));
  check("now playing card rendered", Boolean(text(window, ".np-title")), text(window, ".np-title"));
  check("station label rendered", Boolean(text(window, ".np-station")), text(window, ".np-station"));
  check("transport buttons rendered", count(window, ".transport .tbtn") >= 3, `${count(window, ".transport .tbtn")}`);
  check("volume slider rendered", Boolean(document.getElementById("np-volume")));
  check("voice channel mock rendered", Boolean(text(window, ".vc-name")), text(window, ".vc-status"));
  check("player health rendered", count(window, "#player-health dt") >= 5);
  check("station select filled", count(window, "#play-station option") >= 8, `${count(window, "#play-station option")} options`);
  check("studio moods offered", count(window, "#play-station optgroup option") === 5, `${count(window, "#play-station optgroup option")} moods`);
  check("voice channel select filled", count(window, "#play-channel option") >= 2);
  check("visualizer bars", count(window, ".visualizer i") === 5);

  // Move to a generated station first: a live broadcast's title is the same
  // before and after a skip, so "the title changed" would prove nothing there.
  document.getElementById("play-station").value = "studio-midnight";
  document.getElementById("play-go-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(1400);
  check("station picker starts playback", /Studio/.test(text(window, ".np-station")), text(window, ".np-station"));
  const before = text(window, ".np-title");
  const skipButton = [...document.querySelectorAll(".transport .tbtn")].find((node) => node.dataset.act === "skip");
  skipButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(1200);
  const after = text(window, ".np-title");
  check("skip changed the track", before !== after, `${before} -> ${after}`);
  check("toast shown after an action", document.getElementById("toast").classList.contains("show"), text(window, "#toast"));

  const pauseButton = [...document.querySelectorAll(".transport .tbtn")].find((node) => node.dataset.act === "pause");
  if (pauseButton) {
    pauseButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await sleep(1000);
    check("pause flips the button", [...document.querySelectorAll(".transport .tbtn")].some((node) => node.dataset.act === "resume"));
    check("paused shows in the status line", /paused/i.test(text(window, ".vc-status")), text(window, ".vc-status"));
    const resumeButton = [...document.querySelectorAll(".transport .tbtn")].find((node) => node.dataset.act === "resume");
    resumeButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await sleep(900);
  }

  const slider = document.getElementById("np-volume");
  slider.value = "35";
  slider.dispatchEvent(new window.Event("input", { bubbles: true }));
  check("volume label follows the slider", text(window, "#np-volume-value") === "35%", text(window, "#np-volume-value"));
  slider.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(900);

  // --- servers ---
  await goto("servers");
  check("settings title shows the server", /settings$/i.test(text(window, "#settings-title")), text(window, "#settings-title"));
  check("default station select filled", count(window, "#set-station option") >= 8);
  check("voice channel select filled", count(window, "#set-voice option") >= 2);
  check("DJ role select filled", count(window, "#set-dj option") >= 3);
  check("permission rows rendered", count(window, "#perm-list .perm-row") === 6, `${count(window, "#perm-list .perm-row")}`);
  check("priority speaker marked no-effect", /no-effect/.test(document.getElementById("perm-list").innerHTML));
  check("guild stats rendered", count(window, "#guild-stats dt") >= 5);
  const saveButton = document.getElementById("save-settings-btn");
  document.getElementById("set-volume").value = "55";
  document.getElementById("set-volume").dispatchEvent(new window.Event("input", { bubbles: true }));
  const beforeSwitch = document.getElementById("set-autostart").getAttribute("aria-checked");
  document.getElementById("set-autostart").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const afterSwitch = document.getElementById("set-autostart").getAttribute("aria-checked");
  check("switch toggles aria-checked", beforeSwitch !== afterSwitch, `${beforeSwitch} -> ${afterSwitch}`);
  saveButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(1200);
  check("settings saved (toast)", /saved/i.test(text(window, "#toast")), text(window, "#toast"));

  // --- stations ---
  await goto("stations");
  check("station cards rendered", count(window, "#station-grid .station-card") >= 8, `${count(window, "#station-grid .station-card")}`);
  check("station actions rendered", count(window, "#station-grid [data-do]") >= 32);
  check("generative panel lists recipes", count(window, "#generative-panel .queue-row") === 5);
  check("library panel rendered", Boolean(document.getElementById("library-panel").textContent.trim()));
  check("empty library folder explains itself", /empty/i.test(document.getElementById("library-panel").textContent),
    document.getElementById("library-panel").textContent.replace(/\s+/g, " ").trim().slice(0, 160));
  const testButton = [...document.querySelectorAll("#station-grid [data-do='test']")].find((node) => node.dataset.station === "generative");
  testButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(3500);
  check("station test reports a resolution", /Resolved/.test(text(window, "#toast")), text(window, "#toast"));

  document.getElementById("new-name").value = "UI Test Station";
  document.getElementById("new-url").value = "https://example.invalid/stream.mp3";
  document.getElementById("new-art").value = "🧪";
  document.getElementById("add-station-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(1500);
  check("station added from the UI", /Saved/.test(text(window, "#toast")), text(window, "#toast"));
  check("new station card appears", [...document.querySelectorAll("#station-grid .station-name")].some((node) => node.textContent === "UI Test Station"));
  const removeButton = [...document.querySelectorAll("#station-grid [data-do='remove']")].find((node) => node.dataset.station === "ui-test-station");
  if (removeButton) {
    removeButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await sleep(1200);
    check("station removed from the UI", /Removed/.test(text(window, "#toast")), text(window, "#toast"));
  } else {
    check("station removed from the UI", false, "remove button not found");
  }

  // --- history ---
  await goto("history");
  check("history stats rendered", count(window, "#history-stats .stat-card") === 4);
  check("history rows rendered", count(window, "#history-body tr") >= 5, `${count(window, "#history-body tr")}`);
  check("history feed rendered", count(window, "#history-feed .feed-row") >= 5);
  document.getElementById("history-scope").value = "guild";
  document.getElementById("history-scope").dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(1200);
  check("history scope switch works", count(window, "#history-body tr") >= 1);

  // --- commands ---
  await goto("commands");
  check("command rows rendered", count(window, "#command-body tr") === 30, `${count(window, "#command-body tr")}`);
  check("command signature shown", /\/lofi play/.test(document.getElementById("command-body").textContent));
  check("permission tiers shown", count(window, "#command-body .tag") >= 30);

  // --- settings ---
  await goto("settings");
  check("network fields filled", document.getElementById("net-host").value.length > 0, document.getElementById("net-host").value);
  check("warnings rendered", Boolean(document.getElementById("net-warnings").textContent.trim()));
  check("install info rendered", count(window, "#install-info dt") >= 6);
  check("urls rendered", count(window, "#net-urls dt") >= 2);

  // --- guild switcher ---
  const select = document.getElementById("guild-select");
  select.value = select.options[1].value;
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(1400);
  await goto("player");
  check("switching servers changes the player", Boolean(text(window, ".np-station")), text(window, ".np-station"));

  // --- logout ---
  document.getElementById("logout-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await sleep(800);
  check("logout returns to the login view", visible(window, "#login-view"));

  const failed = checks.filter((item) => !item.ok);
  for (const item of checks) {
    console.log(`${item.ok ? "PASS" : "FAIL"}  ${item.label}${item.detail ? ` — ${String(item.detail).slice(0, 90)}` : ""}`);
  }
  console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
  if (errors.length) {
    console.log(`\nJS errors (${errors.length}):`);
    for (const error of [...new Set(errors)].slice(0, 12)) console.log("  " + error.slice(0, 300));
  } else {
    console.log("No JS errors.");
  }
  window.close();
  process.exit(failed.length || errors.length ? 1 : 0);
})().catch((error) => {
  console.error("harness failure:", error);
  process.exit(2);
});
