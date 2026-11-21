const $ = s => document.querySelector(s);
const DEFAULTS = { server: "", token: "", interval: 60, enabled: true };

chrome.storage.sync.get(DEFAULTS).then(s => {
  $("#server").value = s.server;
  $("#token").value = s.token;
  $("#interval").value = s.interval;
  $("#enabled").checked = s.enabled;
});

function say(text, cls = "muted") { const m = $("#msg"); m.textContent = text; m.className = cls; }

$("#save").onclick = async () => {
  let server = $("#server").value.trim().replace(/\/+$/, "");
  if (server && !/^https?:\/\//i.test(server)) server = "https://" + server;
  let origin;
  try { origin = new URL(server).origin; } catch { return say("Enter a valid URL", "bad"); }
  const token = $("#token").value.trim();
  if (!token) return say("Paste the import key from ParcelDeck", "bad");

  // Must be requested from this click: lets the extension talk to your ParcelDeck server.
  const granted = await chrome.permissions.request({ origins: [origin + "/*"] });
  if (!granted) return say("Chrome permission for " + origin + " was declined", "bad");

  await chrome.storage.sync.set({
    server, token,
    interval: Math.max(15, parseInt($("#interval").value, 10) || 60),
    enabled: $("#enabled").checked,
  });
  $("#server").value = server;
  say("Testing…");
  const r = await chrome.runtime.sendMessage({ type: "test-server" });
  if (!r?.ok) return say(r?.error || "Could not reach ParcelDeck", "bad");
  say("Connected. Syncing Amazon…", "ok");
  const { last } = await chrome.runtime.sendMessage({ type: "sync-now" }) || {};
  if (last?.ok) say(`Connected ✓ — found ${last.shipments} Amazon shipment(s), ${last.added} new sent to ParcelDeck`, "ok");
  else say("Connected to ParcelDeck, but Amazon sync failed: " + (last?.error || "unknown"), "bad");
};
