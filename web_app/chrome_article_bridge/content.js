(() => {
  const MIN_PAGE_CHARS = 200;
  const MAX_PAGE_CHARS = 60000;
  const DEBOUNCE_MS = 700;
  const HEARTBEAT_MS = 30000;
  const BRIDGE_VERSION = '0.4.3';
  const RECEIVER_HOSTS = new Set(['127.0.0.1', 'localhost']);

  let publishTimer = null;
  let publishing = false;
  let publishAfterCurrent = false;
  let lastFingerprint = '';
  let lastSentAt = 0;
  let extensionContextInvalidated = false;
  let invalidationWarned = false;
  let observer = null;
  let heartbeatTimer = null;

  function clean(text) {
    return String(text || '')
      .replace(/\r\n?/g, '\n')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/[ \t]{2,}/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function visible(element) {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }

  function hasSensitiveContext() {
    const selectors = [
      'input[type="password"]',
      'input[autocomplete="current-password"]',
      'input[autocomplete="new-password"]',
      'input[autocomplete="one-time-code"]',
      'input[autocomplete="cc-number"]',
      'input[autocomplete="cc-csc"]',
      'input[autocomplete="cc-exp"]',
      'input[name*="card" i]',
      'input[name*="cvv" i]',
      'input[name*="cvc" i]',
    ];
    if ([...document.querySelectorAll(selectors.join(','))].some(visible)) return true;

    const sensitivePath = /\/(?:login|log-in|signin|sign-in|auth|checkout|payment|billing)(?:\/|$)/i;
    if (sensitivePath.test(location.pathname)) return true;

    return [...document.querySelectorAll('form')].some((form) => {
      if (!visible(form)) return false;
      const scope = clean([
        form.getAttribute('action'),
        form.getAttribute('aria-label'),
        form.querySelector('button[type="submit"]')?.innerText,
        form.querySelector('input[type="submit"]')?.value,
      ].join(' '));
      return /\b(?:sign in|log in|password|passcode|verify account|checkout|payment|card number)\b/i.test(scope);
    });
  }

  function canonicalUrl() {
    const canonical = document.querySelector('link[rel="canonical"]')?.href;
    return canonical && /^https?:/i.test(canonical) ? canonical : location.href;
  }

  function pageTitle(root) {
    return clean(
      root?.querySelector?.('h1')?.innerText ||
      document.querySelector('meta[property="og:title"]')?.content ||
      document.title
    ).slice(0, 500);
  }

  function extractXArticle() {
    const article = document.querySelector('[data-testid="twitterArticleReadView"]');
    const body = article?.querySelector('[data-testid="twitterArticleRichTextView"]');
    if (!article || !body) return null;

    const text = clean(body.innerText);
    if (text.length < 600) return null;

    const title = clean(
      article.querySelector('[data-testid="twitter-article-title"]')?.innerText
    );
    const articleId = location.pathname.match(/\/(?:article|status)\/(\d+)/)?.[1] || '';
    const focusMode = /\/article\/\d+/.test(location.pathname);
    const replyMarker = document.querySelector(
      '[data-testid="inline_reply_offscreen"], ' +
      '[data-testid="tweetTextarea_0"], ' +
      '[aria-label*="Post your reply"]'
    );
    const conversation = document.querySelector('[aria-label*="Timeline: Conversation"]');
    const replyPosts = [...document.querySelectorAll('article[data-testid="tweet"]')]
      .filter((node) => !article.contains(node) && !node.contains(article));

    return {
      bridge_version: BRIDGE_VERSION,
      url: location.href,
      article_id: articleId,
      title,
      text,
      source: 'x_dom',
      content_type: 'x_article',
      complete: true,
      truncated: false,
      clutter_filtered: true,
      comments_excluded: true,
      replies_detected: Boolean(replyMarker || replyPosts.length || (!focusMode && conversation)),
      boundary: focusMode ? 'x_focus_article_end' : 'x_article_body_end',
    };
  }

  function extractXPost() {
    const match = location.pathname.match(/^\/([^/]+)\/status\/(\d+)(?:\/|$)/);
    if (!match) return null;
    const [, handle, statusId] = match;
    const statusPath = `/${handle}/status/${statusId}`;
    const posts = [...document.querySelectorAll('article[data-testid="tweet"]')];
    const primary = posts.find((post) => [...post.querySelectorAll('a[href]')].some((link) => {
      try {
        const target = new URL(link.href, location.href);
        return target.pathname === statusPath && Boolean(link.querySelector('time'));
      } catch {
        return false;
      }
    })) || posts.find(visible);
    const textNode = primary?.querySelector('[data-testid="tweetText"]');
    const text = clean(textNode?.innerText);
    if (!primary || !text) return null;

    const authorText = clean(primary.querySelector('[data-testid="User-Name"]')?.innerText);
    const author = authorText.split('\n').filter(Boolean).slice(0, 2).join(' ') || `@${handle}`;
    return {
      bridge_version: BRIDGE_VERSION,
      url: location.href,
      article_id: statusId,
      title: `X post by ${author}`.slice(0, 500),
      text,
      source: 'x_post_dom',
      content_type: 'x_post',
      complete: true,
      truncated: false,
      clutter_filtered: true,
      comments_excluded: true,
      replies_detected: posts.some((post) => post !== primary),
      boundary: 'x_primary_post_end',
    };
  }

  function candidateScore(element) {
    if (!visible(element)) return -Infinity;
    const text = clean(element.innerText);
    if (text.length < MIN_PAGE_CHARS) return -Infinity;
    const linkText = clean(
      [...element.querySelectorAll('a')].map((link) => link.innerText).join(' ')
    );
    const semanticBonus = element.matches('article,[role="article"],[itemprop="articleBody"]') ? 2400 : 0;
    const mainBonus = element.matches('main,[role="main"],#main,#content') ? 1200 : 0;
    const paragraphBonus = Math.min(element.querySelectorAll('p').length, 30) * 80;
    const controlPenalty = element.querySelectorAll('button,input,select,textarea').length * 120;
    return text.length - (linkText.length * 1.5) + semanticBonus + mainBonus + paragraphBonus - controlPenalty;
  }

  function bestContentRoot() {
    const selectors = [
      '[itemprop="articleBody"]',
      '[role="article"]',
      'article',
      'main',
      '[role="main"]',
      '#main',
      '#content',
      '.main-content',
      '.page-content',
      '.story-body',
      '.story-content',
      '.article-body',
      '.article-content',
      '.entry-content',
      '.post-content',
    ];
    const candidates = [...new Set(document.querySelectorAll(selectors.join(',')))];
    let best = null;
    let bestScore = -Infinity;
    for (const candidate of candidates) {
      const score = candidateScore(candidate);
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
    if (best) return best;
    return candidateScore(document.body) > -Infinity ? document.body : null;
  }

  function prune(root) {
    const commentSelectors = [
      '[class*="comments"]', '[id*="comments"]',
      '[class*="comment-list"]', '[id*="comment-list"]',
      '[class*="discussion"]', '[id*="discussion"]',
      '[data-testid*="comment"]',
    ];
    const selectors = [
      'script', 'style', 'noscript', 'template', 'svg', 'canvas',
      'nav', 'aside', 'footer', 'form', 'button', 'input', 'select', 'textarea',
      '[role="navigation"]', '[role="dialog"]', '[aria-hidden="true"]',
      '[hidden]', '[inert]',
      '[class*="cookie"]', '[id*="cookie"]',
      '[class*="consent"]', '[id*="consent"]',
      '[class*="newsletter"]', '[id*="newsletter"]',
      '[class*="advert"]', '[id*="advert"]',
      '[class*="social-share"]', '[id*="social-share"]',
      '[class*="related-post"]', '[id*="related-post"]',
      '[class*="recommend"]', '[id*="recommend"]',
      '[class*="paywall"]', '[id*="paywall"]',
      ...commentSelectors,
    ];
    const commentsRemoved = root.querySelectorAll(commentSelectors.join(',')).length;
    let removed = 0;
    for (const node of root.querySelectorAll(selectors.join(','))) {
      node.remove();
      removed += 1;
    }
    return { removed, commentsRemoved };
  }

  function readableText(root) {
    const blocks = [...root.querySelectorAll(
      'h1,h2,h3,h4,h5,h6,p,li,blockquote,pre,figcaption,td,th'
    )];
    const parts = [];
    let previous = '';
    for (const block of blocks) {
      const text = clean(block.textContent);
      if (!text || text === previous) continue;
      parts.push(text);
      previous = text;
    }
    const structured = clean(parts.join('\n\n'));
    return structured.length >= MIN_PAGE_CHARS ? structured : clean(root.textContent);
  }

  function extractGenericPage() {
    const liveRoot = bestContentRoot();
    if (!liveRoot) return null;

    const rootKind = liveRoot.matches('[itemprop="articleBody"],[role="article"],article')
      ? 'article'
      : liveRoot.matches('main,[role="main"],#main,#content,.main-content,.page-content')
        ? 'main'
        : 'page';
    const title = pageTitle(liveRoot);
    const clone = liveRoot.cloneNode(true);
    const { removed, commentsRemoved } = prune(clone);
    const fullText = readableText(clone);
    if (fullText.length < MIN_PAGE_CHARS) return null;

    const truncated = fullText.length > MAX_PAGE_CHARS;
    const text = truncated
      ? clean(fullText.slice(0, MAX_PAGE_CHARS).replace(/\s+\S*$/, ''))
      : fullText;
    const boundary = rootKind === 'article'
      ? 'semantic_article_end'
      : rootKind === 'main'
        ? 'main_content_end'
        : 'document_body_end';

    return {
      bridge_version: BRIDGE_VERSION,
      url: canonicalUrl(),
      article_id: '',
      title,
      text,
      source: 'browser_dom',
      content_type: rootKind,
      complete: !truncated,
      truncated,
      clutter_filtered: removed > 0,
      comments_excluded: commentsRemoved > 0,
      replies_detected: false,
      boundary,
    };
  }

  function extractPage() {
    if (document.visibilityState !== 'visible') return { status: 'hidden' };
    if (!/^https?:$/.test(location.protocol)) return { status: 'unsupported' };
    if (RECEIVER_HOSTS.has(location.hostname) && location.port === '7860') {
      return { status: 'receiver' };
    }
    if (hasSensitiveContext()) return { status: 'blocked' };

    const xArticle = extractXArticle();
    if (xArticle) return { status: 'ready', page: xArticle };
    const xPost = extractXPost();
    if (xPost) return { status: 'ready', page: xPost };
    // X timelines are endless feeds, not one bounded document. Only a
    // dedicated long-form Article or one canonical status post is bounded.
    if (/(^|\.)x\.com$/i.test(location.hostname)) return { status: 'no-content' };

    const page = extractGenericPage();
    return page ? { status: 'ready', page } : { status: 'no-content' };
  }

  function markContextInvalidated(error) {
    if (!/extension context invalidated/i.test(error)) return false;
    extensionContextInvalidated = true;
    publishing = false;
    publishAfterCurrent = false;
    if (publishTimer !== null) clearTimeout(publishTimer);
    publishTimer = null;
    if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
    heartbeatTimer = null;
    observer?.disconnect();
    if (!invalidationWarned) {
      invalidationWarned = true;
      // One warning per page: every frame runs this script, but only the top
      // frame's note is actionable (reloads/re-injects cover the whole tab).
      if (window.top === window.self) {
        console.warn(
          '[Chatbot Page Bridge] Extension was reloaded. Reload this page once to activate the new content script.'
        );
      }
    }
    return true;
  }

  function runtimeMessage(message, callback = () => {}) {
    if (extensionContextInvalidated) return;
    try {
      chrome.runtime.sendMessage(message, (response) => {
        let error = '';
        try {
          error = chrome.runtime.lastError?.message || '';
        } catch (caught) {
          error = caught instanceof Error ? caught.message : String(caught);
        }
        markContextInvalidated(error);
        callback(response, error);
      });
    } catch (caught) {
      const error = caught instanceof Error ? caught.message : String(caught);
      markContextInvalidated(error);
      callback(undefined, error);
    }
  }

  function notifyStatus(state, detail = '') {
    runtimeMessage({ type: 'bridge-status', state, detail }, (_response, error) => {
      if (error && !extensionContextInvalidated) {
        console.warn('[Chatbot Page Bridge] status delivery failed:', error);
      }
    });
  }

  function fingerprint(page) {
    return [page.url, page.text.length, page.text.slice(0, 160), page.text.slice(-160)].join('|');
  }

  function publish(force = false) {
    if (extensionContextInvalidated) return;
    if (publishing) {
      // A tab activation must not be swallowed by an older in-flight publish.
      if (force) publishAfterCurrent = true;
      return;
    }
    const result = extractPage();
    if (result.status !== 'ready') {
      notifyStatus(result.status);
      return;
    }

    const mark = fingerprint(result.page);
    const now = Date.now();
    if (!force && mark === lastFingerprint && now - lastSentAt < HEARTBEAT_MS) return;

    publishing = true;
    notifyStatus('sending');
    runtimeMessage({ type: 'publish-page', page: result.page }, (response, error) => {
      publishing = false;
      if (extensionContextInvalidated) return;
      const republish = publishAfterCurrent;
      publishAfterCurrent = false;
      if (response?.disabled) {
        return;
      } else if (response?.hidden) {
        notifyStatus('hidden');
      } else if (error || !response?.ok) {
        const detail = error || response?.error || 'Local chatbot is unavailable.';
        console.warn('[Chatbot Page Bridge] page delivery failed:', detail);
        if (!error) notifyStatus('error', detail);
      } else {
        lastFingerprint = mark;
        lastSentAt = Date.now();
        notifyStatus('ready', `${result.page.text.length} characters ready`);
      }
      if (republish && document.visibilityState === 'visible') publish(true);
    });
  }

  function schedulePublish(force = false) {
    if (extensionContextInvalidated) return;
    if (publishTimer !== null) clearTimeout(publishTimer);
    publishTimer = setTimeout(() => {
      publishTimer = null;
      publish(force);
    }, DEBOUNCE_MS);
  }

  function hidePage() {
    if (extensionContextInvalidated) return;
    notifyStatus('hidden');
    runtimeMessage({ type: 'hide-page' }, (response, error) => {
      if (error && !extensionContextInvalidated) {
        console.warn('[Chatbot Page Bridge] hide delivery failed:', error);
      } else if (response && !response.ok) {
        console.warn('[Chatbot Page Bridge] hide request failed:', response.error || 'unknown error');
      }
    });
  }

  function handleVisibilityChange() {
    if (document.visibilityState === 'visible') {
      // Each content script remembers its own last fingerprint. Without a
      // forced publish here, switching A -> B -> A within the 30-second
      // heartbeat leaves B as the server's newest cached page.
      publish(true);
    } else {
      hidePage();
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== 'publish-now') return false;
    publish(true);
    sendResponse({ ok: true });
    return false;
  });

  observer = new MutationObserver(schedulePublish);
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
  document.addEventListener('visibilitychange', handleVisibilityChange);
  window.addEventListener('focus', () => publish(true));
  window.addEventListener('pageshow', () => publish(true));
  window.addEventListener('pagehide', hidePage);
  heartbeatTimer = setInterval(publish, HEARTBEAT_MS);
  schedulePublish();
})();
