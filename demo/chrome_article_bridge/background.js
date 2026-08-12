const ENDPOINT = 'http://127.0.0.1:7860/api/browser/page';

const BADGES = {
  ready: { text: '\u2713', color: '#188038', title: 'Page text is ready for Chatbot' },
  sending: { text: '\u2026', color: '#1a73e8', title: 'Sending page text to Chatbot' },
  error: { text: '!', color: '#d93025', title: 'Local Chatbot is not running on port 7860' },
  blocked: { text: 'X', color: '#b06000', title: 'Blocked on password or payment pages' },
  'no-content': { text: '\u2014', color: '#6b7280', title: 'No bounded article or main page text found' },
  hidden: { text: '', color: '#6b7280', title: 'Background tabs are not shared' },
  receiver: { text: '', color: '#6b7280', title: 'The Chatbot page is never shared with itself' },
  unsupported: { text: 'X', color: '#6b7280', title: 'Chrome does not allow page extraction here' },
};

async function setStatus(tabId, state, detail = '') {
  if (!tabId) return;
  const badge = BADGES[state] || BADGES['no-content'];
  await chrome.action.setBadgeText({ tabId, text: badge.text });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: badge.color });
  await chrome.action.setTitle({
    tabId,
    title: detail ? `${badge.title}: ${detail}` : badge.title,
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const tabId = sender.tab?.id;
  if (message?.type === 'bridge-status') {
    void setStatus(tabId, message.state, message.detail);
    return false;
  }
  if (message?.type !== 'publish-page' || !message.page || !tabId) return false;

  void (async () => {
    try {
      await setStatus(tabId, 'sending');
      const response = await fetch(ENDPOINT, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Chatbot-Bridge': 'page-v1',
        },
        body: JSON.stringify(message.page),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `HTTP ${response.status}`);
      }
      await setStatus(tabId, 'ready', `${message.page.text.length} characters ready`);
      sendResponse({ ok: true });
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      await setStatus(tabId, 'error', detail.slice(0, 160));
      sendResponse({ ok: false, error: detail });
    }
  })();
  return true;
});

chrome.action.onClicked.addListener((tab) => {
  if (!tab.id) return;
  chrome.tabs.sendMessage(tab.id, { type: 'publish-now' }, () => {
    if (chrome.runtime.lastError) void setStatus(tab.id, 'unsupported');
  });
});
