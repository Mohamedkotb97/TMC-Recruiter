const keyInput = document.getElementById("apiKey");
const urlInput = document.getElementById("backendUrl");
const saveBtn = document.getElementById("saveBtn");
const testBtn = document.getElementById("testBtn");
const openDashboardBtn = document.getElementById("openDashboardBtn");
const versionBadge = document.getElementById("versionBadge");
const statusEl = document.getElementById("status");

const DEFAULT_URL = "https://tmc-recruiter.onrender.com";

try {
  const mf = chrome.runtime.getManifest();
  if (versionBadge) versionBadge.textContent = "v" + mf.version;
} catch {}

function showStatus(msg, ok = true) {
  statusEl.textContent = msg;
  statusEl.className = "status " + (ok ? "ok" : "err");
}
function clearStatus() {
  statusEl.textContent = "";
  statusEl.className = "status";
}

chrome.storage.local.get(["apiKey", "backendUrl"], (r) => {
  if (r.apiKey) keyInput.value = r.apiKey;
  urlInput.value = r.backendUrl || DEFAULT_URL;
  // Auto-test when the popup opens so the user doesn't have to click.
  if (r.apiKey) runTest(true);
  else showStatus("⚠ Paste your personal key from the dashboard → Settings.", false);
});

function readConfig() {
  return {
    apiKey: keyInput.value.trim(),
    backendUrl: (urlInput.value.trim() || DEFAULT_URL).replace(/\/+$/, ""),
  };
}

saveBtn.addEventListener("click", () => {
  const cfg = readConfig();
  if (!cfg.apiKey) return showStatus("Paste your key first.", false);
  chrome.storage.local.set(cfg, () => runTest(false));
});

testBtn.addEventListener("click", () => runTest(false));

if (openDashboardBtn) {
  openDashboardBtn.addEventListener("click", () => {
    const cfg = readConfig();
    const base = cfg.backendUrl || DEFAULT_URL;
    const target = base.replace(/\/+$/, "") + "/#settings";
    try { chrome.tabs.create({ url: target }); }
    catch { window.open(target, "_blank"); }
  });
}

async function runTest(silent) {
  const cfg = readConfig();
  if (!cfg.apiKey) return showStatus("Paste your key first.", false);
  if (cfg.apiKey && !cfg.apiKey.startsWith("tmc_")) {
    showStatus("⚠ That doesn't look like a personal key. Personal keys start with 'tmc_'. Copy the key from the dashboard → Settings → Chrome Extension.", false);
  }
  testBtn.disabled = true;
  if (!silent) showStatus("Testing…", true);
  try {
    const res = await fetch(`${cfg.backendUrl}/api/auth/me`, {
      headers: { "X-API-Key": cfg.apiKey },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 401) {
        showStatus("✗ Key rejected. Log in to the dashboard → Settings → Regenerate, then paste the new tmc_… key here.", false);
      } else {
        showStatus(`✗ Server responded ${res.status}: ${data.detail || "unknown"}`, false);
      }
    } else {
      const u = data.user;
      if (!u) {
        showStatus("⚠ Connected, but no user matches this key. This usually means you pasted the global CRM_API_KEY instead of your personal tmc_… key.", false);
      } else {
        showStatus(`✓ Connected as ${u.display_name || u.username}. Ready — open LinkedIn messaging.`, true);
        chrome.storage.local.set(cfg);
      }
    }
  } catch (e) {
    const hint = e.message && e.message.indexOf("Failed to fetch") === 0
      ? " — DNS/CORS/HTTPS problem. If the dashboard is https://, the backend URL must be https:// too."
      : "";
    showStatus(`✗ Can't reach ${cfg.backendUrl}${hint} (${e.message})`, false);
  } finally {
    testBtn.disabled = false;
  }
}
