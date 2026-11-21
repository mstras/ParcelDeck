const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function ago(t) {
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return new Date(t).toLocaleString();
}

function render(last) {
  const el = $("#status");
  if (!last) { el.innerHTML = '<span class="muted">Not synced yet</span>'; return; }
  el.innerHTML = last.ok
    ? `<span class="ok">✓ Synced ${ago(last.at)}</span>
       <span>${last.shipments} shipment${last.shipments === 1 ? "" : "s"} in the last 30 days, ${last.with_tracking} with tracking ID</span>
       <span class="muted">${last.added ? `${last.added} new sent to ParcelDeck` : "Nothing new"}</span>`
    : `<span class="bad">⚠ ${esc(last.error)}</span><span class="muted">${ago(last.at)}</span>`;
}

chrome.storage.local.get("last").then(({ last }) => render(last));

$("#sync").onclick = async () => {
  const b = $("#sync");
  b.disabled = true; b.textContent = "Syncing…";
  const { last } = await chrome.runtime.sendMessage({ type: "sync-now" }) || {};
  render(last || (await chrome.storage.local.get("last")).last);
  b.disabled = false; b.textContent = "Sync now";
};

$("#open").onclick = async () => {
  const { server } = await chrome.storage.sync.get({ server: "" });
  server ? chrome.tabs.create({ url: server }) : chrome.runtime.openOptionsPage();
};
$("#opts").onclick = e => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
