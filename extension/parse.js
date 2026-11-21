// Parsers for Amazon order-history and package-tracking pages.
// Text-based on purpose: Amazon's CSS class names change often, the visible wording much less.

const TRACK_SEL = 'a[href*="ship-track"], a[href*="progress-tracker"]';
const ITEM_SEL = 'a[href*="/dp/"], a[href*="/gp/product/"]';
const ORDER_RE = /Order\s*#\s*:?\s*((?:\d{3}|D\d{2})-\d{7}-\d{7})/i;
const STATUS_RE = /(?:^|\n)\s*((?:Arriving|Now arriving|Out for delivery|Delivered|Shipped|Running late|Delayed|In transit|Not yet shipped|Preparing for shipment|Package was handed|Arrived)[^\n]{0,80})/i;
const ITEM_NOISE = /buy it again|view your item|write a (product )?review|return or replace|track package|get product support|leave seller feedback|share gift receipt|ask product question/i;

const BLOCK = new Set(["ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "BR", "DD", "DIV", "DL", "DT", "FIELDSET",
  "FIGCAPTION", "FIGURE", "FOOTER", "FORM", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER", "HR", "LI", "MAIN",
  "NAV", "OL", "P", "PRE", "SECTION", "TABLE", "TR", "TD", "TH", "UL"]);

// innerText-like text with line breaks between blocks (works on DOMParser documents, which aren't rendered).
function docText(node) {
  const out = [];
  (function walk(n) {
    if (n.nodeType === 3) { out.push(n.nodeValue); return; }
    if (n.nodeType !== 1) return;
    const t = n.tagName;
    if (t === "SCRIPT" || t === "STYLE" || t === "NOSCRIPT" || t === "TEMPLATE") return;
    const block = BLOCK.has(t);
    if (block) out.push("\n");
    for (const c of n.childNodes) walk(c);
    if (block) out.push("\n");
  })(node);
  return out.join("").replace(/[ \t ]+/g, " ").replace(/ *\n[\s\n]*/g, "\n").trim();
}

function isSignIn(doc, url) {
  return /\/ap\/(signin|mfa|cvf)|\/ax\/claim/.test(url || "") ||
    !!doc.querySelector('form[name="signIn"], #ap_email, #ap_password, input[name="cvf_captcha_input"]');
}

function parseOrders(doc, base) {
  const shipments = [], seen = new Set();
  for (const a of doc.querySelectorAll(TRACK_SEL)) {
    let u;
    try { u = new URL(a.getAttribute("href"), base); } catch { continue; }
    // Shipment block: the largest ancestor that still contains only this tracking link.
    let block = a;
    while (block.parentElement && block.parentElement.querySelectorAll(TRACK_SEL).length === 1 &&
           !ORDER_RE.test(docText(block.parentElement))) block = block.parentElement;
    let card = block;
    while (card && !ORDER_RE.test(docText(card))) card = card.parentElement;
    const orderId = (card && (docText(card).match(ORDER_RE) || [])[1]) || u.searchParams.get("orderId") || "";
    const shipmentId = u.searchParams.get("shipmentId") || u.searchParams.get("packageId") || "";
    const pkg = u.searchParams.get("packageIndex") || "0";
    const key = `${orderId}|${shipmentId}|${pkg}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const scope = block.querySelector(ITEM_SEL) ? block : (card || block);
    const items = [...new Set([...scope.querySelectorAll(ITEM_SEL)]
      .map(x => docText(x).replace(/\n/g, " ").trim())
      .filter(t => t.length > 3 && !ITEM_NOISE.test(t)))];
    const text = docText(block);
    const status = ((text.match(STATUS_RE) || [])[1] || "").trim();
    const delivered = /^Delivered\b/i.test(status);
    shipments.push({ key, order_id: orderId, shipment_id: shipmentId, url: u.href, items, status, delivered });
  }
  return shipments;
}

function parseTracking(doc) {
  const t = docText(doc.body || doc);
  const tid = (t.match(/Tracking ID\s*:?\s*\n?\s*([A-Z0-9][A-Z0-9\-]{7,39})/i) || [])[1] || "";
  const carrier = ((t.match(/(?:Shipped with|Delivered by|Delivery by|Carrier)\s*:?\s*([^\n]{2,40})/i) || [])[1] || "").trim();
  const status = ((t.match(STATUS_RE) || [])[1] || "").trim();
  return { tracking_id: tid.replace(/-/g, "").toUpperCase(), carrier, status };
}

function parseHtml(kind, html, url, base) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  if (isSignIn(doc, url)) return { signin: true };
  if (kind === "orders") return { signin: false, shipments: parseOrders(doc, base) };
  return { signin: false, ...parseTracking(doc) };
}

if (typeof module !== "undefined") module.exports = { docText, parseOrders, parseTracking, parseHtml };
