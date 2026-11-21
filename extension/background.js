// ParcelDeck for Amazon — background service worker.
const DEFAULTS = { server: "", token: "", interval: 60, enabled: true };
const ORDERS_PATH = "/your-orders/orders?timeFilter=last30";
const MAX_TRACK_FETCHES = 15;       // per sync, for shipments not seen before
const PASSIVE_SYNC_GAP = 10 * 60e3; // don't re-sync more than every 10 min from page visits
const CACHE_DAYS = 60;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const settings = async () => ({ ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) });
// amazonBase is only overridden by the test harness.
const amazonBase = async () => (await chrome.storage.local.get({ amazonBase: "https://www.amazon.com" })).amazonBase;

// ------------------------------------------------------------------ scheduling
async function schedule() {
  const s = await settings();
  await chrome.alarms.clear("sync");
  if (s.enabled) chrome.alarms.create("sync", { delayInMinutes: 1, periodInMinutes: Math.max(15, +s.interval || 60) });
}
chrome.runtime.onInstalled.addListener(() => { schedule(); chrome.runtime.openOptionsPage(); });
chrome.runtime.onStartup.addListener(schedule);
chrome.storage.onChanged.addListener((c, area) => { if (area === "sync" && (c.interval || c.enabled)) schedule(); });
chrome.alarms.onAlarm.addListener(a => { if (a.name === "sync") sync("scheduled"); });

// ------------------------------------------------------------------ parsing (offscreen document)
async function ensureOffscreen() {
  if (chrome.offscreen.hasDocument && await chrome.offscreen.hasDocument()) return;
  try {
    await chrome.offscreen.createDocument({ url: "offscreen.html", reasons: ["DOM_PARSER"], justification: "Parse Amazon order pages" });
  } catch (e) {
    if (!String(e).includes("single offscreen")) throw e;
  }
}
async function parse(kind, page) {
  await ensureOffscreen();
  const r = await chrome.runtime.sendMessage({ target: "offscreen", kind, html: page.html, url: page.url, base: await amazonBase() });
  if (!r || r.error) throw new Error("Parse failed: " + (r?.error || "no response"));
  return r;
}

// ------------------------------------------------------------------ loading Amazon pages
async function loadViaFetch(url) {
  const res = await fetch(url, { credentials: "include", redirect: "follow" });
  if (!res.ok) throw new Error(`Amazon returned HTTP ${res.status}`);
  return { html: await res.text(), url: res.url };
}

// Fallback when cookies aren't sent with fetch: open the page in a background tab and let content.js hand it over.
const pendingTabs = new Map(); // tabId -> resolve
async function loadViaTab(url) {
  const tab = await chrome.tabs.create({ url, active: false });
  try {
    return await new Promise((resolve, reject) => {
      pendingTabs.set(tab.id, resolve);
      setTimeout(() => reject(new Error("Timed out loading Amazon in a tab")), 45e3);
    });
  } finally {
    pendingTabs.delete(tab.id);
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

// ------------------------------------------------------------------ sync
let running = null, lastPassive = 0;

async function setLast(info) {
  await chrome.storage.local.set({ last: { ...info, at: Date.now() } });
  await chrome.action.setBadgeText({ text: info.ok ? "" : "!" });
  if (!info.ok) await chrome.action.setBadgeBackgroundColor({ color: "#d93025" });
}

async function post(s, path, body) {
  const res = await fetch(new URL(path, s.server).href, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + s.token },
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new Error("ParcelDeck rejected the import key — copy it again from ParcelDeck ⚙︎");
  if (!res.ok) throw new Error(`ParcelDeck returned HTTP ${res.status}`);
  return res.json();
}

function sync(trigger, ordersPage) {
  if (!running) running = doSync(trigger, ordersPage).finally(() => { running = null; });
  return running;
}

async function doSync(trigger, ordersPage) {
  const s = await settings();
  if (!s.server || !s.token) {
    await setLast({ ok: false, error: "Open Options and enter your ParcelDeck URL and import key." });
    return chrome.storage.local.get("last");
  }
  try {
    const base = await amazonBase();
    let useTab = false;
    let page = ordersPage || await loadViaFetch(base + ORDERS_PATH);
    let r = await parse("orders", page);
    if (r.signin && !ordersPage) { useTab = true; page = await loadViaTab(base + ORDERS_PATH); r = await parse("orders", page); }
    if (r.signin) throw new Error("Signed out of Amazon — sign in at amazon.com in this browser, then Sync now.");

    const { cache = {} } = await chrome.storage.local.get("cache");
    const out = [];
    let fetched = 0;
    for (const sh of r.shipments) {
      const hit = cache[sh.key];
      if (hit?.tracking_id) { out.push({ ...sh, tracking_id: hit.tracking_id, carrier: hit.carrier || "" }); continue; }
      if (sh.delivered || fetched >= MAX_TRACK_FETCHES) continue;
      fetched++;
      if (fetched > 1) await sleep(1500 + Math.random() * 1500);  // be gentle
      let tp = useTab ? await loadViaTab(sh.url) : await loadViaFetch(sh.url);
      let tr = await parse("tracking", tp);
      if (tr.signin && !useTab) { useTab = true; tp = await loadViaTab(sh.url); tr = await parse("tracking", tp); }
      if (tr.tracking_id) cache[sh.key] = { tracking_id: tr.tracking_id, carrier: tr.carrier, t: Date.now() };
      out.push({ ...sh, tracking_id: tr.tracking_id, carrier: tr.carrier, status: tr.status || sh.status });
    }
    const cutoff = Date.now() - CACHE_DAYS * 864e5;
    for (const k of Object.keys(cache)) if (cache[k].t < cutoff) delete cache[k];
    await chrome.storage.local.set({ cache });

    const res = out.length ? await post(s, "/api/import/amazon", { shipments: out.map(strip) }) : { added: [], updated: 0 };
    await setLast({
      ok: true, trigger, shipments: r.shipments.length, with_tracking: out.filter(x => x.tracking_id).length,
      added: res.added.length, mode: useTab ? "tab" : "fetch",
    });
  } catch (e) {
    await setLast({ ok: false, trigger, error: e.message });
  }
  return chrome.storage.local.get("last");
}

const strip = sh => ({ order_id: sh.order_id, shipment_id: sh.shipment_id, items: sh.items, tracking_id: sh.tracking_id || "",
                       carrier: sh.carrier || "", status: sh.status || "", url: sh.url });

// Single tracking page opened by the user: import just that shipment.
async function importTrackingPage(page) {
  const s = await settings();
  if (!s.server || !s.token) return;
  const tr = await parse("tracking", page);
  if (tr.signin || !tr.tracking_id) return;
  const u = new URL(page.url);
  await post(s, "/api/import/amazon", { shipments: [{ order_id: u.searchParams.get("orderId") || "", shipment_id: u.searchParams.get("shipmentId") || "",
    items: [], tracking_id: tr.tracking_id, carrier: tr.carrier, status: tr.status, url: page.url }] }).catch(() => {});
}

// ------------------------------------------------------------------ messages
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg?.target === "offscreen") return;
  if (msg?.type === "page-html" && sender.tab) {
    const page = { html: msg.html, url: msg.url };
    const waiting = pendingTabs.get(sender.tab.id);
    if (waiting) { waiting(page); return; }
    // Passive: user is browsing Amazon.
    if (/progress-tracker|ship-track/.test(msg.url)) importTrackingPage(page);
    else if (Date.now() - lastPassive > PASSIVE_SYNC_GAP) { lastPassive = Date.now(); sync("page visit", page); }
    return;
  }
  if (msg?.type === "sync-now") { sync("manual").then(reply); return true; }
  if (msg?.type === "test-server") {
    settings().then(s => post(s, "/api/import/ping", {})).then(() => reply({ ok: true }), e => reply({ ok: false, error: e.message }));
    return true;
  }
});
