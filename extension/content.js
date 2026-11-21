// Runs on Amazon order, tracking and sign-in pages. Hands the page to the extension so that
// (a) background syncs can fall back to a real tab, and (b) pages you open yourself are imported too.
chrome.runtime.sendMessage({
  type: "page-html",
  url: location.href,
  html: document.documentElement.outerHTML,
}).catch(() => {});
