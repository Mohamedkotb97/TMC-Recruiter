// content.js — LinkedIn messaging + profile capture for Recruiter CRM.
//
// Three floating buttons (messaging page):
//   ⬇ Save current — saves the open thread
//   ◔ Save last N  — saves the top N conversations (newest-first in sidebar)
//   ⇅ Sync ALL     — lazy-loads entire sidebar, saves every thread
//
// All three POST to /api/conversations/bulk (dedupes by thread_url on the server).
// The walker clicks each sidebar item, waits for the URL to actually change,
// scrolls the thread pane to the top to lazy-load full history, then extracts.

const DEFAULT_BACKEND_URL = "http://localhost:8000";
const BATCH_SIZE = 8;             // conversations per upload POST
const OPEN_SETTLE_MS = 600;       // settle after the thread is loaded (600 ms — LinkedIn re-renders a few times)
const THREAD_WAIT_MS = 15000;     // max wait for a thread's messages to render
const URL_CHANGE_WAIT_MS = 10000; // max wait for the URL to switch to a new thread
const LIST_SCROLL_ROUNDS = 60;    // max lazy-load rounds when syncing ALL
const HISTORY_SCROLL_ROUNDS = 25; // how many times we scroll thread to top
const HISTORY_SCROLL_WAIT_MS = 900;
const INTER_CLICK_MS = 500;       // breather between conversation clicks (avoid LI anti-bot throttle)
const EMPTY_EXTRACT_RETRY_MS = 2500;

// ========== Utilities ==========

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getConfig() {
  return new Promise((resolve) => {
    chrome.storage.local.get(["apiKey", "backendUrl"], (r) => {
      resolve({
        apiKey: r.apiKey || "",
        backendUrl: (r.backendUrl || DEFAULT_BACKEND_URL).replace(/\/+$/, ""),
      });
    });
  });
}

async function getApiKey() { return (await getConfig()).apiKey; }
async function getBackendUrl() { return (await getConfig()).backendUrl; }

function isOnMessagingThread() {
  return /\/messaging\/thread\//.test(window.location.pathname);
}
function isOnMessagingInbox() {
  return window.location.pathname.includes("/messaging/");
}
function isOnProfile() {
  return /\/in\/[^/]+\/?$/.test(window.location.pathname);
}

function currentThreadId() {
  const m = window.location.pathname.match(/\/messaging\/thread\/([^/]+)/);
  return m ? m[1] : null;
}

// ========== Conversation Extraction (Messaging page) ==========

function queryFirst(parent, selectors) {
  for (const s of selectors) {
    const el = parent.querySelector(s);
    if (el) return el;
  }
  return null;
}

// Parse a single msg-s-event-listitem (or similar) into {sender, body, timestamp}.
function extractMessageFromNode(node, defaultSender) {
  const senderEl = queryFirst(node, [
    ".msg-s-message-group__name",
    ".msg-s-message-group__profile-link",
    ".msg-s-message-group__meta .msg-s-message-group__name",
  ]);
  const sender = senderEl ? senderEl.innerText.trim() : (defaultSender || "");

  const timeEl = queryFirst(node, [
    "time[datetime]",
    ".msg-s-message-group__timestamp",
    ".msg-s-message-list__time-heading",
  ]);
  const timestamp = timeEl
    ? (timeEl.getAttribute("datetime") || timeEl.innerText.trim())
    : "";

  // Pick the INNERMOST body element so we don't accidentally include the sender
  // label / timestamp text inside "body".
  const bodyEl = queryFirst(node, [
    ".msg-s-event-listitem__body",
    ".msg-s-event-with-indicator__body",
    ".msg-s-event__content",
  ]);
  const body = bodyEl ? bodyEl.innerText.trim() : "";
  return { sender, body, timestamp };
}

function extractConversation() {
  const threadContainer = document.querySelector(
    ".msg-s-message-list, .msg-s-message-list-container, [aria-label*='conversation']"
  );
  if (!threadContainer) return null;

  // --- candidate identity (from conversation header) ---
  const nameEl = queryFirst(document, [
    ".msg-entity-lockup__entity-title",
    "h2.msg-title",
    ".msg-thread__link-to-profile",
  ]);
  const candidateName = nameEl ? nameEl.innerText.trim().split("\n")[0] : "Unknown";

  const profileLink = queryFirst(document, [
    "a.msg-thread__link-to-profile",
    ".msg-entity-lockup a[href*='/in/']",
    "a[href*='/in/'][data-test-app-aware-link]",
  ]);
  const profileUrl = profileLink ? profileLink.href.split("?")[0] : "";

  const subtitleEl = queryFirst(document, [
    ".msg-entity-lockup__entity-info",
    ".msg-entity-lockup__occupation",
    ".msg-thread__subtitle",
  ]);
  const headline = subtitleEl ? subtitleEl.innerText.trim().replace(/\s+/g, " ") : "";
  let currentTitle = "";
  let currentCompany = "";
  if (headline && headline.includes(" at ")) {
    const parts = headline.split(" at ");
    currentTitle = parts[0].trim();
    currentCompany = parts.slice(1).join(" at ").trim();
  }

  // --- messages ---
  // Prefer the specific selector `.msg-s-event-listitem` because it's 1-item-per-message.
  // `.msg-s-message-list__event` is sometimes a wrapper that contains 2+ event-listitems.
  // If we used the union we'd double-count.
  let messageNodes = Array.from(
    threadContainer.querySelectorAll(".msg-s-event-listitem")
  );
  if (messageNodes.length === 0) {
    // Fall back to `.msg-s-message-list__event`, but only direct children.
    messageNodes = Array.from(
      threadContainer.querySelectorAll(".msg-s-message-list__event")
    );
  }

  // Sender stickiness: LinkedIn shows the sender name ONCE at the top of a
  // group, then subsequent messages in that group have no name. We carry it
  // forward until we see a new one.
  let lastSender = "";
  const seenBodies = new Set(); // dedupe identical consecutive bodies
  const messages = [];
  for (const node of messageNodes) {
    const m = extractMessageFromNode(node, lastSender);
    if (!m.body) continue;
    if (m.sender) lastSender = m.sender;
    // Prevent exact duplicate back-to-back (LinkedIn sometimes renders twice).
    const key = (lastSender || m.sender) + "::" + m.body;
    if (seenBodies.has(key)) continue;
    seenBodies.add(key);
    messages.push({
      sender: m.sender || lastSender || "Unknown",
      timestamp: m.timestamp,
      body: m.body,
    });
  }

  const threadUrl = window.location.href.split("?")[0].split("#")[0];

  return {
    candidate_name: candidateName,
    profile_url: profileUrl,
    thread_url: threadUrl,
    captured_at: new Date().toISOString(),
    messages,
    source: "linkedin_extension",
    headline,
    current_title: currentTitle,
    current_company: currentCompany,
    location: "",
  };
}

// Scroll the thread pane UP to load older messages. Break only when the
// message count has been stable for TWO consecutive rounds (one stable
// round isn't enough — LinkedIn sometimes pauses a round before streaming
// the next batch of older messages).
async function loadFullThreadHistory(maxRounds = HISTORY_SCROLL_ROUNDS) {
  const scroller = document.querySelector(
    ".msg-s-message-list, .msg-s-message-list-container"
  );
  if (!scroller) return;
  let prev = -1;
  let stableRounds = 0;
  for (let i = 0; i < maxRounds; i++) {
    const items = scroller.querySelectorAll(".msg-s-event-listitem").length;
    if (items === prev) {
      stableRounds += 1;
      if (stableRounds >= 2 && i > 2) break;
    } else {
      stableRounds = 0;
    }
    prev = items;
    scroller.scrollTop = 0;
    await sleep(HISTORY_SCROLL_WAIT_MS);
  }
  // After loading history, scroll back to bottom (LinkedIn's natural state).
  scroller.scrollTop = scroller.scrollHeight;
  await sleep(150);
}

// ========== Backend communication ==========

async function postBulk(conversations) {
  const { apiKey, backendUrl } = await getConfig();
  if (!apiKey) {
    const msg = "No API key — open the extension popup and paste your personal key from the dashboard → Settings.";
    statusLog(msg, "err");
    showToast(msg, "error");
    return null;
  }
  statusLog(`POST ${backendUrl}/api/conversations/bulk — ${conversations.length} thread(s)…`, "info");
  try {
    const res = await fetch(`${backendUrl}/api/conversations/bulk`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": apiKey,
      },
      body: JSON.stringify({ conversations, source: "linkedin_extension" }),
    });
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch { data = { raw: text }; }
    if (!res.ok) {
      const msg = data.detail || data.raw || `HTTP ${res.status}`;
      console.error("[CRM] backend rejected:", res.status, msg);
      if (res.status === 401) {
        statusLog(`✗ 401 — your key is invalid. Regenerate in Settings and re-save in the extension popup.`, "err");
        showToast("Your key is invalid — regenerate it in the dashboard's Settings page.", "error");
      } else {
        statusLog(`✗ HTTP ${res.status}: ${String(msg).slice(0,180)}`, "err");
        showToast(`Server error ${res.status}`, "error");
      }
      return null;
    }
    statusLog(`✓ backend: created=${data.created||0} updated=${data.updated||0} unchanged=${data.unchanged||0} skipped=${data.skipped||0}`, "ok");
    return data;
  } catch (err) {
    console.error("[CRM] bulk upload failed:", err);
    const hint = err.message.includes("Failed to fetch")
      ? ` — can't reach ${backendUrl}. Is the backend running? Check the URL in the popup.`
      : "";
    statusLog(`✗ network: ${err.message}${hint}`, "err");
    showToast("Upload failed: " + err.message, "error");
    return null;
  }
}

async function postSingle(endpoint, payload) {
  const { apiKey, backendUrl } = await getConfig();
  if (!apiKey) { showToast("Paste your personal key from the dashboard first.", "error"); return null; }
  try {
    const res = await fetch(`${backendUrl}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}${txt ? `: ${txt.slice(0, 160)}` : ""}`);
    }
    return await res.json();
  } catch (err) {
    console.error("[CRM] save failed:", err);
    showToast("Save failed: " + err.message, "error");
    return null;
  }
}

// ========== UI: toast + persistent status panel ==========

function showToast(message, type = "success") {
  const existing = document.getElementById("crm-toast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.id = "crm-toast";
  toast.className = `crm-toast crm-toast--${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.classList.add("crm-toast--visible"), 10);
  setTimeout(() => {
    toast.classList.remove("crm-toast--visible");
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// Persistent progress panel — stays open during long syncs so the user can
// watch each step. Lives in the bottom-left; close via the × in the header.
function statusPanel() {
  let panel = document.getElementById("crm-status-panel");
  if (panel) return panel;
  panel = document.createElement("div");
  panel.id = "crm-status-panel";
  panel.innerHTML = `
    <div class="crm-sp__head">
      <strong>TMC sync</strong>
      <span class="crm-sp__close" title="Close">×</span>
    </div>
    <div class="crm-sp__body"></div>
  `;
  document.body.appendChild(panel);
  panel.querySelector(".crm-sp__close").addEventListener("click", () => panel.remove());
  return panel;
}
function statusLog(msg, kind = "info") {
  const panel = statusPanel();
  const body = panel.querySelector(".crm-sp__body");
  const line = document.createElement("div");
  line.className = `crm-sp__line crm-sp__line--${kind}`;
  const time = new Date().toLocaleTimeString();
  line.innerHTML = `<span class="crm-sp__time">${time}</span> ${msg}`;
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
  console.log(`[CRM] ${msg}`);
}
function statusClear() {
  const panel = document.getElementById("crm-status-panel");
  if (panel) panel.querySelector(".crm-sp__body").innerHTML = "";
}

// Wait until a thread pane has actually loaded messages.
function waitForThreadLoaded(timeout = THREAD_WAIT_MS) {
  return new Promise((resolve) => {
    const start = Date.now();
    const check = () => {
      const hasMessages = document.querySelector(".msg-s-event-listitem");
      if (hasMessages) return setTimeout(resolve, OPEN_SETTLE_MS);
      if (Date.now() - start > timeout) return resolve();
      setTimeout(check, 200);
    };
    check();
  });
}

// Wait until the URL's thread id changes away from `prev`.
function waitForThreadIdChange(prev, timeout = URL_CHANGE_WAIT_MS) {
  return new Promise((resolve) => {
    const start = Date.now();
    const check = () => {
      const cur = currentThreadId();
      if (cur && cur !== prev) return resolve(cur);
      if (Date.now() - start > timeout) return resolve(null);
      setTimeout(check, 150);
    };
    check();
  });
}

// ------------------------------------------------------------------
// Sidebar detection
//
// LinkedIn's messaging sidebar redesign is a moving target — they rename
// CSS classes, swap <a href="..."> for button-with-onclick, and rewire the
// scroll container every few months. To avoid getting broken by every
// reshuffle we use FOUR independent strategies (in priority order):
//
//   1. Known CSS classes            — fastest, works on unchanged designs.
//   2. Thread-URL anchors           — href="*/messaging/thread/*".
//   3. URN / data-attribute hits    — data-urn="urn:li:messagingConversation:…".
//   4. Structural fallback          — every <li>/role=listitem that is a
//                                     direct-ish child of the sidebar
//                                     scrollable container and contains
//                                     something clickable.
//
// A row detected by ANY strategy is normalised to its nearest container
// (<li> preferred) and deduped by reference.
// ------------------------------------------------------------------

// Find the scrollable sidebar container. Tries known classes first, then
// falls back to "the scrollable ancestor of any rendered conversation row",
// then finally "the biggest scrollable div in the left half of the viewport".
function getSidebarScroller() {
  const known = document.querySelector(
    ".msg-conversations-container__conversations-list, " +
    "ul.msg-conversations-container__conversations-list, " +
    "[data-view-name='messaging-conversation-list']"
  );
  if (known) return known;

  // From any known conversation-row element, walk UP to first scrollable.
  const seed =
    document.querySelector("a[href*='/messaging/thread/']") ||
    document.querySelector("[data-urn*='messagingConversation']") ||
    document.querySelector("[data-view-name*='conversation-list-item']") ||
    document.querySelector("li.msg-conversation-listitem");
  if (seed) {
    let el = seed.parentElement;
    while (el && el !== document.body) {
      const style = window.getComputedStyle(el);
      if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 4) {
        return el;
      }
      el = el.parentElement;
    }
  }

  // Last resort: biggest scrollable div in the left half of the viewport.
  const candidates = Array.from(document.querySelectorAll("ul, div, section"))
    .filter((el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 200 || r.height < 300) return false;
      if (r.left > window.innerWidth * 0.55) return false; // must be on the left
      const s = window.getComputedStyle(el);
      return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 4;
    })
    .sort((a, b) => b.scrollHeight - a.scrollHeight);
  return candidates[0] || null;
}

// Scroll the conversations sidebar to force lazy-load of older items.
async function loadAllSidebarItems(maxRounds = LIST_SCROLL_ROUNDS) {
  const list = getSidebarScroller();
  if (!list) return collectSidebarItems();

  let prev = -1;
  for (let i = 0; i < maxRounds; i++) {
    const n = collectSidebarItems().length;
    if (n === prev) break;
    prev = n;
    list.scrollTop = list.scrollHeight;
    await sleep(600);
  }
  return collectSidebarItems();
}

function collectSidebarItems() {
  const set = new Set();
  const tryAdd = (el, requireAnchor = false) => {
    const row = rowContainerFor(el);
    if (!row) return;
    if (requireAnchor) {
      // Only accept rows that have SOMETHING clickable — a, button, role=button,
      // role=link. Otherwise this catches header/separator rows.
      if (!row.querySelector("a, button, [role='button'], [role='link']")) return;
    }
    set.add(row);
  };

  // --- Strategy 1: known CSS classes (fast path) ------------------
  const classSelectors = [
    "li.msg-conversation-listitem",
    "li.msg-conversations-container__convo-item",
    ".msg-conversation-card__content--selectable",
    ".msg-conversation-listitem",
    "[data-view-name='messaging-conversation-list-item']",
    "[data-view-name='messaging-conversation-list-item-wrapper']",
  ];
  for (const sel of classSelectors) {
    document.querySelectorAll(sel).forEach((el) => tryAdd(el, true));
  }

  // --- Strategy 2: thread-URL anchors -----------------------------
  document.querySelectorAll("a[href*='/messaging/thread/']").forEach((a) => tryAdd(a));

  // --- Strategy 3: URN / data-attributes --------------------------
  // LinkedIn puts conversation URNs on various ancestors even when they
  // remove the href. Examples seen in the wild:
  //   data-urn="urn:li:messagingConversation:2-xxx"
  //   data-conversation-urn="urn:li:fs_conversation:..."
  //   data-view-name="messaging-conversation-list-item"
  document.querySelectorAll(
    "[data-urn*='messagingConversation'], " +
    "[data-urn*='fs_conversation'], " +
    "[data-conversation-urn], " +
    "[data-test-app-aware-link*='/messaging/thread/']"
  ).forEach((el) => tryAdd(el));

  // --- Strategy 4: structural (scroller children) -----------------
  // Only run if we haven't found anything yet — this is slower and
  // potentially over-inclusive, but it rescues us when LinkedIn ships a
  // fresh redesign that breaks every class/URN heuristic above.
  if (set.size === 0) {
    const scroller = getSidebarScroller();
    if (scroller) {
      scroller
        .querySelectorAll("li, [role='listitem']")
        .forEach((el) => tryAdd(el, true));
    }
  }

  return Array.from(set);
}

// Given any node inside a conversation row, return the element we should
// treat as the row — in priority order:
//   1) nearest <li>
//   2) nearest [role=listitem] / [data-view-name*='conversation']
//   3) the element itself
function rowContainerFor(el) {
  if (!el) return null;
  return (
    el.closest("li") ||
    el.closest("[role='listitem']") ||
    el.closest("[data-view-name*='conversation']") ||
    el
  );
}

// Wait until the sidebar looks hydrated — either the scroller has non-
// trivial scrollHeight (items have rendered) or we see some rows via the
// normal detection. Returns the final item count.
async function waitForSidebarHydration(timeoutMs = 15000) {
  const start = Date.now();
  let items = collectSidebarItems();
  while (items.length === 0 && Date.now() - start < timeoutMs) {
    // Nudge the scroller so LinkedIn's virtualiser actually renders rows.
    const list = getSidebarScroller();
    if (list) {
      list.scrollTop = 10;
      await sleep(60);
      list.scrollTop = 0;
    }
    await sleep(350);
    items = collectSidebarItems();
  }
  return items;
}

// Extract a sidebar item's target thread URL (preferred click target).
function getSidebarItemLink(item) {
  return (
    item.querySelector("a[href*='/messaging/thread/']") ||
    item.querySelector("a[href*='/thread/']") ||
    null
  );
}

// ========== Walker — open N items, extract, upload in batches ==========

async function walkAndUpload(items, btn, label) {
  const original = btn.innerHTML;
  const restore = () => { btn.disabled = false; btn.innerHTML = original; };
  btn.disabled = true;

  statusClear();
  statusLog(`${label}: walking ${items.length} sidebar conversation(s)…`, "info");

  let created = 0, updated = 0, unchanged = 0, skipped = 0, failed = 0;
  let batch = [];
  const seenThreadIds = new Set();

  const flush = async () => {
    if (!batch.length) return;
    btn.innerHTML = `<span class="crm-save-btn__icon">⏳</span><span>Uploading ${batch.length}…</span>`;
    const res = await postBulk(batch);
    if (res) {
      created += res.created || 0;
      updated += res.updated || 0;
      unchanged += res.unchanged || 0;
      skipped += res.skipped || 0;
    } else {
      failed += batch.length;
    }
    batch = [];
  };

  for (let i = 0; i < items.length; i++) {
    try {
      btn.innerHTML = `<span class="crm-save-btn__icon">⏳</span><span>${label} ${i + 1}/${items.length}</span>`;

      const prevId = currentThreadId();
      const item = items[i];
      item.scrollIntoView({ block: "center" });
      await sleep(150);

      // Prefer clicking the real <a> so LinkedIn's router navigates us.
      // Fall back to dispatching a real bubbling MouseEvent (plain .click()
      // on non-anchor elements sometimes doesn't trigger LinkedIn's SPA
      // router).
      const link = getSidebarItemLink(item);
      const clickTarget = link || item.querySelector("a, [role='link'], button") || item;
      try {
        clickTarget.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      } catch {
        clickTarget.click();
      }

      // Wait for the thread to switch AND the messages to render.
      await waitForThreadIdChange(prevId);
      await waitForThreadLoaded();
      await loadFullThreadHistory();

      const threadId = currentThreadId();
      if (!threadId) {
        statusLog(`item ${i+1}: no thread id after click — skipped`, "warn");
        skipped++; continue;
      }
      if (seenThreadIds.has(threadId)) {
        statusLog(`item ${i+1}: duplicate threadId ${threadId} — skipped`, "warn");
        skipped++; continue;
      }
      seenThreadIds.add(threadId);

      // Extract once; if we got zero messages it's usually because LinkedIn
      // hasn't finished rendering (throttled, slow network). Wait a little
      // and retry once before giving up.
      let payload = extractConversation();
      if (!payload || payload.messages.length === 0) {
        await sleep(EMPTY_EXTRACT_RETRY_MS);
        await loadFullThreadHistory(6);
        payload = extractConversation();
      }
      if (!payload || payload.messages.length === 0) {
        statusLog(`item ${i+1} (${threadId.slice(0,10)}…): extracted 0 messages — skipped`, "warn");
        skipped++;
        continue;
      }
      statusLog(`item ${i+1}: "${payload.candidate_name}" · ${payload.messages.length} msgs`, "ok");
      batch.push(payload);
      if (batch.length >= BATCH_SIZE) await flush();

      // Breather between items so LinkedIn's router doesn't rate-limit us.
      await sleep(INTER_CLICK_MS);
    } catch (e) {
      console.error(`[CRM] walker item ${i+1} failed:`, e);
      statusLog(`item ${i+1}: crashed — ${e.message}`, "err");
      failed++;
    }
  }
  await flush();

  const ok = (created + updated) > 0;
  statusLog(`${ok ? "✓" : "⚠"} ${label} DONE — new=${created} updated=${updated} same=${unchanged} skipped=${skipped} failed=${failed}`, ok ? "ok" : "warn");
  if (ok) {
    statusLog(`Open the dashboard to see them in the inbox.`, "info");
  } else if (skipped === items.length) {
    statusLog(`Nothing uploaded. Common causes: LinkedIn didn't open the threads, or your key is wrong. Test your key from the extension popup.`, "warn");
  }
  showToast(
    `${ok ? "✓" : "⚠"} ${label}: new=${created} updated=${updated} same=${unchanged} skipped=${skipped} failed=${failed}`,
    ok ? "success" : "error"
  );
  restore();
}

// ========== Handlers ==========

async function handleSaveCurrent(btn) {
  const original = btn.innerHTML;
  const restore = () => { btn.disabled = false; btn.innerHTML = original; };
  btn.disabled = true;
  btn.innerHTML = `<span class="crm-save-btn__icon">⏳</span><span>Saving…</span>`;

  if (isOnMessagingThread() || isOnMessagingInbox()) {
    await waitForThreadLoaded();
    await loadFullThreadHistory();
    const payload = extractConversation();
    if (!payload || payload.messages.length === 0) {
      showToast("No conversation found — open a thread first", "error");
      return restore();
    }
    const res = await postBulk([payload]);
    if (res) {
      const word = res.updated ? "updated" : res.created ? "saved" : "already up to date";
      showToast(`✓ ${payload.candidate_name}: ${word} (${payload.messages.length} msgs)`, "success");
    }
  } else if (isOnProfile()) {
    const payload = {
      full_name: document.querySelector("h1")?.innerText?.trim() || "Unknown",
      profile_url: window.location.href.split("?")[0],
      captured_at: new Date().toISOString(),
      source: "linkedin_profile",
    };
    const res = await postSingle("/api/candidates", payload);
    if (res) showToast(`✓ Saved profile: ${payload.full_name}`, "success");
  } else {
    showToast("Open a LinkedIn conversation or profile first", "error");
  }
  restore();
}

async function handleSaveLastN(btn, n) {
  const original = btn.innerHTML;
  const restore = () => { btn.disabled = false; btn.innerHTML = original; };
  btn.disabled = true;
  btn.innerHTML = `<span class="crm-save-btn__icon">⏳</span><span>Loading list…</span>`;
  statusClear();
  statusLog(`Save last ${n}: scrolling sidebar to top…`, "info");

  const list = getSidebarScroller();
  if (list) { list.scrollTop = 0; await sleep(400); }
  else statusLog(`Couldn't locate the sidebar scroll container — will keep trying…`, "warn");

  // Phase 1: wait for the sidebar to hydrate at all (can take several seconds
  // on a cold messaging page load).
  let items = await waitForSidebarHydration(12000);

  // Phase 2: once some rows exist, wait for at least `n` to be present and
  // the count to be stable across two polls (the virtualiser re-renders a
  // few times).
  let prev = -1;
  let stable = 0;
  for (let i = 0; i < 15; i++) {
    items = collectSidebarItems();
    if (items.length >= n && items.length === prev) {
      stable += 1;
      if (stable >= 2) break;
    } else {
      stable = 0;
    }
    prev = items.length;
    await sleep(400);
  }

  statusLog(`Found ${items.length} conversation item(s) in the sidebar`, items.length ? "ok" : "warn");
  if (items.length === 0) {
    // Detailed diagnostic so we can see exactly which strategies failed.
    const anchors      = document.querySelectorAll("a[href*='/messaging/thread/']").length;
    const urns         = document.querySelectorAll("[data-urn*='messagingConversation'], [data-urn*='fs_conversation'], [data-conversation-urn]").length;
    const knownRows    = document.querySelectorAll("li.msg-conversation-listitem, li.msg-conversations-container__convo-item, [data-view-name='messaging-conversation-list-item']").length;
    const scroller     = getSidebarScroller();
    const scrollerLis  = scroller ? scroller.querySelectorAll("li").length : 0;
    const onMessaging  = /\/messaging\//.test(window.location.pathname);
    statusLog(`Diagnostic · URL on /messaging/: ${onMessaging} · thread anchors: ${anchors} · URN rows: ${urns} · known-class rows: ${knownRows} · scroller found: ${!!scroller} · <li> in scroller: ${scrollerLis}`, "warn");
    if (!onMessaging) {
      statusLog(`You're not on https://www.linkedin.com/messaging/ — navigate there first.`, "warn");
    } else if (anchors === 0 && urns === 0 && scrollerLis === 0) {
      statusLog(`Sidebar hasn't hydrated yet. Scroll the left list once by hand so LinkedIn renders the items, then click Save last ${n} again.`, "warn");
    } else {
      statusLog(`LinkedIn redesigned the sidebar DOM again. Send me this diagnostic so I can add a new selector.`, "warn");
    }
    showToast("No conversations visible in the sidebar", "error");
    return restore();
  }
  const picked = items.slice(0, n);
  statusLog(`Picked the newest ${picked.length} conversation(s)`, "info");
  await walkAndUpload(picked, btn, `Last ${n}`);
  restore();
}

async function handleSaveAll(btn) {
  const original = btn.innerHTML;
  const restore = () => { btn.disabled = false; btn.innerHTML = original; };
  btn.disabled = true;
  btn.innerHTML = `<span class="crm-save-btn__icon">⏳</span><span>Loading list…</span>`;
  statusClear();

  // Wait for sidebar hydration first (same guard as Save last N).
  statusLog(`Sync ALL: waiting for the sidebar to hydrate…`, "info");
  await waitForSidebarHydration(12000);

  statusLog(`Sync ALL: scrolling sidebar to lazy-load every conversation…`, "info");
  const items = await loadAllSidebarItems();
  if (items.length === 0) {
    const anchors = document.querySelectorAll("a[href*='/messaging/thread/']").length;
    const urns    = document.querySelectorAll("[data-urn*='messagingConversation'], [data-urn*='fs_conversation'], [data-conversation-urn]").length;
    const scroller = getSidebarScroller();
    const scrollerLis = scroller ? scroller.querySelectorAll("li").length : 0;
    statusLog(`Diagnostic · thread anchors: ${anchors} · URN rows: ${urns} · scroller found: ${!!scroller} · <li> in scroller: ${scrollerLis}`, "warn");
    showToast("No conversations found in the sidebar", "error");
    return restore();
  }
  statusLog(`Loaded ${items.length} conversation(s) — uploading…`, "ok");
  await walkAndUpload(items, btn, "Sync");
  restore();
}

// ========== UI injection ==========

function injectSaveButton() {
  const existingSave = document.getElementById("crm-save-btn");
  const existingN    = document.getElementById("crm-save-n-btn");
  const existingAll  = document.getElementById("crm-save-all-btn");

  if (!isOnMessagingInbox() && !isOnProfile()) {
    if (existingSave) existingSave.remove();
    if (existingN) existingN.remove();
    if (existingAll) existingAll.remove();
    return;
  }

  if (!existingSave) {
    const btn = document.createElement("button");
    btn.id = "crm-save-btn";
    btn.className = "crm-save-btn";
    btn.innerHTML = `<span class="crm-save-btn__icon">⬇</span><span>Save current</span>`;
    btn.addEventListener("click", () => handleSaveCurrent(btn));
    document.body.appendChild(btn);
  }

  if (isOnMessagingInbox()) {
    if (!existingN) {
      const nBtn = document.createElement("button");
      nBtn.id = "crm-save-n-btn";
      nBtn.className = "crm-save-btn crm-save-btn--secondary";
      nBtn.innerHTML = `<span class="crm-save-btn__icon">◔</span><span>Save last 5</span>`;
      nBtn.addEventListener("click", () => handleSaveLastN(nBtn, 5));
      document.body.appendChild(nBtn);
    }
    if (!existingAll) {
      const allBtn = document.createElement("button");
      allBtn.id = "crm-save-all-btn";
      allBtn.className = "crm-save-btn crm-save-btn--tertiary";
      allBtn.innerHTML = `<span class="crm-save-btn__icon">⇅</span><span>Sync ALL</span>`;
      allBtn.addEventListener("click", () => handleSaveAll(allBtn));
      document.body.appendChild(allBtn);
    }
  } else {
    if (existingN) existingN.remove();
    if (existingAll) existingAll.remove();
  }
}

// ========== Init ==========

function init() {
  injectSaveButton();
  let lastUrl = location.href;
  new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      setTimeout(injectSaveButton, 1000);
    }
  }).observe(document, { subtree: true, childList: true });
}

init();
