const ENDPOINT = 'http://127.0.0.1:7860/api/browser/page';
const HIDE_ENDPOINT = 'http://127.0.0.1:7860/api/browser/hide';
const CLEAR_ENDPOINT = 'http://127.0.0.1:7860/api/browser/clear';
const STATUS_ENDPOINT = 'http://127.0.0.1:7860/api/browser/status';
const RECEIVER_PATTERN = 'http://127.0.0.1:7860/*';
const RECEIVER_ORIGIN = 'http://127.0.0.1:7860';
const SESSION_KEY = 'chatbotPageBridgeSession';
const HEALTH_ALARM = 'chatbotPageBridgeHealth';
const BRIDGE_VERSION = '0.4.2';

const STATUS_TITLES = {
  ready: 'Page Bridge is enabled; this page text is ready for Chatbot',
  sending: 'Page Bridge is enabled; sending this page text to Chatbot',
  blocked: 'Page Bridge is enabled, but login, password, and payment pages are blocked',
  'no-content': 'Page Bridge is enabled, but this page has no bounded readable text',
  hidden: 'Page Bridge is enabled; background tabs are not shared',
  receiver: 'Page Bridge is enabled; the Chatbot page is never shared with itself',
  unsupported: 'Page Bridge is enabled, but Chrome does not allow extraction on this page',
};

async function getSession() {
  const stored = await chrome.storage.session.get(SESSION_KEY);
  const session = stored?.[SESSION_KEY];
  return session?.enabled
    ? { enabled: true, receiverTabId: session.receiverTabId || null, native: session.native === true }
    : { enabled: false, receiverTabId: null, native: false };
}

async function saveSession(session) {
  await chrome.storage.session.set({ [SESSION_KEY]: session });
}

async function startHealthAlarm() {
  await chrome.alarms.create(HEALTH_ALARM, { periodInMinutes: 0.5 });
}

async function clearReceiverCache() {
  const response = await fetch(CLEAR_ENDPOINT, {
    method: 'POST',
    headers: { 'X-Chatbot-Bridge': 'page-v1' },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
}

async function receiverIsAvailable() {
  const response = await fetch(STATUS_ENDPOINT, { method: 'GET' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const status = await response.json();
  if (status.expected_version !== BRIDGE_VERSION) {
    throw new Error(
      `Restart Chatbot: receiver expects bridge ${status.expected_version || 'unknown'}, ` +
      `but Chrome has ${BRIDGE_VERSION}.`
    );
  }
  return true;
}

async function setDisabledBadge(title = 'Open the local Chatbot page to start Page Bridge') {
  await chrome.action.setBadgeText({ text: '' });
  await chrome.action.setTitle({ title });
}

async function setErrorBadge(detail = '') {
  await chrome.action.setBadgeText({ text: '!' });
  await chrome.action.setBadgeBackgroundColor({ color: '#d93025' });
  await chrome.action.setTitle({
    title: detail
      ? `Page Bridge stopped because Chatbot is unavailable: ${detail}`
      : 'Page Bridge stopped because Chatbot is unavailable on port 7860',
  });
}

async function setStatus(_tabId, state, detail = '') {
  try {
    const session = await getSession();
    if (!session.enabled) {
      await setDisabledBadge();
      return;
    }
    const title = STATUS_TITLES[state] || STATUS_TITLES['no-content'];
    await chrome.action.setBadgeText({ text: '\u2713' });
    await chrome.action.setBadgeBackgroundColor({ color: '#188038' });
    await chrome.action.setTitle({
      title: detail ? `${title}: ${detail}` : title,
    });
  } catch (error) {
    console.warn('[Chatbot Page Bridge] badge update failed:', error);
  }
}

async function disableSession(reason, showError = false) {
  await chrome.storage.session.remove(SESSION_KEY);
  await chrome.alarms.clear(HEALTH_ALARM);
  try {
    await clearReceiverCache();
  } catch (error) {
    if (!showError) {
      console.warn('[Chatbot Page Bridge] receiver cache clear failed:', error);
    }
  }
  if (showError) await setErrorBadge(reason);
  else await setDisabledBadge(reason || 'Chatbot Page Bridge is waiting for the local Chatbot');
}

function isReceiverUrl(value) {
  try {
    const url = new URL(value);
    return url.origin === RECEIVER_ORIGIN && ['/', '/index.html'].includes(url.pathname);
  } catch {
    return false;
  }
}

async function findReceiverTab() {
  const tabs = await chrome.tabs.query({ url: [RECEIVER_PATTERN] });
  return tabs.find((tab) => tab.id && isReceiverUrl(tab.url)) || null;
}

async function chatbotReceiverIsActive() {
  try {
    const tabs = await chrome.tabs.query({
      active: true,
      lastFocusedWindow: true,
      url: [RECEIVER_PATTERN],
    });
    return tabs.some((tab) => isReceiverUrl(tab.url));
  } catch (error) {
    console.warn('[Chatbot Page Bridge] could not inspect the active receiver tab:', error);
    return false;
  }
}

async function senderIsCurrent(sender) {
  const focusedWindow = await chrome.windows.getLastFocused({ populate: false });
  return Boolean(sender.tab?.active && sender.tab.windowId === focusedWindow.id);
}

async function enableSession(tab) {
  try {
    await receiverIsAvailable();
    const receiverTab = await findReceiverTab();
    // Native-app mode: the macOS Voice panel has no receiver tab, so enable
    // on explicit toolbar click without one. The health alarm still enforces
    // receiver reachability; only the receiver-tab scoping is skipped.
    const native = !receiverTab?.id;
    await saveSession({ enabled: true, receiverTabId: receiverTab?.id || null, native });
    await startHealthAlarm();
    await setStatus(tab?.id, tab?.id != null && tab.id === receiverTab?.id ? 'receiver' : 'ready');
    if (tab?.id && tab.id !== receiverTab?.id) {
      chrome.tabs.sendMessage(tab.id, { type: 'publish-now' }, () => {
        if (chrome.runtime.lastError) void setStatus(tab.id, 'unsupported');
      });
    }
    return { ok: true, native };
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    await disableSession(detail.slice(0, 160), true);
    return { ok: false, error: detail };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const tabId = sender.tab?.id;
  if (message?.type === 'bridge-status') {
    void (async () => {
      const session = await getSession();
      if (!session.enabled) {
        if (message.state === 'receiver') {
          const result = await enableSession(sender.tab);
          sendResponse({ ...result, enabled: result.ok });
        } else {
          await setDisabledBadge();
          sendResponse({ ok: true, enabled: false });
        }
        return;
      }
      try {
        // The receiver content script reports every 30 seconds. That doubles
        // as a bounded, best-effort heartbeat for detecting a stopped server.
        if (message.state === 'receiver') await receiverIsAvailable();
        // Adopt receiver scoping if the web page appears after a native-mode
        // (toolbar) enable, so closing it ends the session as usual.
        if (message.state === 'receiver' && session.native && tabId) {
          await saveSession({ enabled: true, receiverTabId: tabId, native: false });
          session.native = false;
          session.receiverTabId = tabId;
        }
        if (
          ['blocked', 'no-content', 'unsupported'].includes(message.state) &&
          await senderIsCurrent(sender)
        ) {
          await clearReceiverCache();
        }
        await setStatus(tabId, message.state, message.detail);
        sendResponse({ ok: true, enabled: true });
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        await disableSession(detail.slice(0, 160), true);
        sendResponse({ ok: false, enabled: false, error: detail });
      }
    })();
    return true;
  }
  if (message?.type === 'hide-page' && tabId) {
    void (async () => {
      const session = await getSession();
      if (!session.enabled) {
        sendResponse({ ok: true, enabled: false });
        return;
      }
      try {
        // Switching from the readable page to the chatbot is the normal voice
        // workflow. Preserve that page for the server's bounded five-minute
        // TTL so read_article can consume it from the receiver tab. Switching
        // to any other page still removes that tab's entry immediately.
        if (await chatbotReceiverIsActive()) {
          await setStatus(tabId, 'hidden');
          sendResponse({ ok: true, enabled: true, preserved: true });
          return;
        }
        const response = await fetch(HIDE_ENDPOINT, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Chatbot-Bridge': 'page-v1',
          },
          body: JSON.stringify({ tab_id: String(tabId) }),
        });
        if (!response.ok) {
          const detail = await response.text();
          throw new Error(detail || `HTTP ${response.status}`);
        }
        await setStatus(tabId, 'hidden');
        sendResponse({ ok: true, enabled: true });
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        await disableSession(detail.slice(0, 160), true);
        sendResponse({ ok: false, enabled: false, error: detail });
      }
    })();
    return true;
  }
  if (message?.type !== 'publish-page' || !message.page || !tabId) return false;

  void (async () => {
    const session = await getSession();
    if (!session.enabled) {
      sendResponse({ ok: false, enabled: false, disabled: true });
      return;
    }
    try {
      // A delayed callback from a background tab or a different Chrome window
      // must never replace the page in the focused window.
      if (!await senderIsCurrent(sender)) {
        await setStatus(tabId, 'hidden');
        sendResponse({ ok: false, enabled: true, hidden: true, error: 'Background tabs are not shared.' });
        return;
      }
      await setStatus(tabId, 'sending');
      const response = await fetch(ENDPOINT, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Chatbot-Bridge': 'page-v1',
        },
        body: JSON.stringify({ ...message.page, tab_id: String(tabId) }),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `HTTP ${response.status}`);
      }
      await setStatus(tabId, 'ready', `${message.page.text.length} characters ready`);
      sendResponse({ ok: true, enabled: true });
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      await disableSession(detail.slice(0, 160), true);
      sendResponse({ ok: false, enabled: false, error: detail });
    }
  })();
  return true;
});

chrome.action.onClicked.addListener((tab) => {
  void (async () => {
    try {
      const session = await getSession();
      if (!session.enabled || await chatbotReceiverIsActive()) {
        await enableSession(tab);
        return;
      }
      if (tab?.id) {
        await clearReceiverCache();
        chrome.tabs.sendMessage(tab.id, { type: 'publish-now' }, () => {
          if (chrome.runtime.lastError) void setStatus(tab.id, 'unsupported');
        });
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      await disableSession(detail.slice(0, 160), true);
    }
  })();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== HEALTH_ALARM) return;
  void (async () => {
    const session = await getSession();
    if (!session.enabled) return;
    try {
      const receiverTab = await findReceiverTab();
      // Native-mode sessions have no receiver tab to track; the
      // receiverIsAvailable check below still ends them if the server stops.
      if (!session.native && (!receiverTab || receiverTab.id !== session.receiverTabId)) {
        await disableSession('Chatbot Page Bridge stopped because the Chatbot tab was closed');
        return;
      }
      await receiverIsAvailable();
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      await disableSession(detail.slice(0, 160), true);
    }
  })();
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void (async () => {
    const session = await getSession();
    if (session.enabled && session.receiverTabId === tabId) {
      await disableSession('Chatbot Page Bridge stopped because the Chatbot tab was closed');
    }
  })();
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  void (async () => {
    const session = await getSession();
    try {
      if (await chatbotReceiverIsActive()) {
        if (session.enabled) await setStatus(tabId, 'receiver');
        else await enableSession({ id: tabId });
        return;
      }
      if (!session.enabled) return;
      // Clear the prior tab before asking the newly active tab to publish.
      // This also closes the stale-text gap for chrome:// and other pages on
      // which Chrome does not inject this extension's content script.
      await clearReceiverCache();
      chrome.tabs.sendMessage(tabId, { type: 'publish-now' }, () => {
        if (chrome.runtime.lastError) void setStatus(tabId, 'unsupported');
      });
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      await disableSession(detail.slice(0, 160), true);
    }
  })();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (!changeInfo.url) return;
  void (async () => {
    const session = await getSession();
    if (isReceiverUrl(changeInfo.url)) {
      if (!session.enabled || session.receiverTabId !== tabId) {
        await enableSession({ id: tabId });
      }
      return;
    }
    if (
      session.enabled &&
      session.receiverTabId === tabId
    ) {
      await disableSession('Chatbot Page Bridge stopped because the Chatbot tab was closed');
    }
  })();
});

void (async () => {
  const session = await getSession();
  const receiverTab = await findReceiverTab();
  if (receiverTab) {
    await enableSession(receiverTab);
  } else if (session.enabled) {
    await disableSession('Chatbot Page Bridge stopped because the Chatbot tab was closed');
  } else {
    await setDisabledBadge();
  }
})();
