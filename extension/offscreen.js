// Service workers have no DOMParser, so parsing happens in this hidden document.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.target !== "offscreen") return;
  try {
    reply(parseHtml(msg.kind, msg.html, msg.url, msg.base));
  } catch (e) {
    reply({ error: String(e) });
  }
});
