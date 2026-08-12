(() => {
  const MIN_PAGE_CHARS = 200;
  const MAX_PAGE_CHARS = 60000;
  const DEBOUNCE_MS = 700;
  const HEARTBEAT_MS = 30000;
  const RECEIVER_HOSTS = new Set(['127.0.0.1', 'localhost']);

  let publishTimer = null;
  let publishing = false;
  let lastFingerprint = '';
  let lastSentAt = 0;

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

  function hasSensitiveForm() {
    const selectors = [
      'input[type="password"]',
      'input[autocomplete="current-password"]',
      'input[autocomplete="new-password"]',
      'input[autocomplete="cc-number"]',
      'input[autocomplete="cc-csc"]',
    ];
    return [...document.querySelectorAll(selectors.join(','))].some(visible);
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

  function candidateScore(element) {
    const text = clean(element.innerText);
    if (text.length < MIN_PAGE_CHARS) return -Infinity;
    const linkText = clean(
      [...element.querySelectorAll('a')].map((link) => link.innerText).join(' ')
    );
    const semanticBonus = element.matches('article,[itemprop="articleBody"]') ? 2000 : 0;
    return text.length - (linkText.length * 1.5) + semanticBonus;
  }

  function bestContentRoot() {
    const selectors = [
      '[itemprop="articleBody"]',
      'article',
      'main',
      '[role="main"]',
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

    const rootKind = liveRoot.matches('[itemprop="articleBody"],article')
      ? 'article'
      : liveRoot.matches('main,[role="main"]')
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
    if (hasSensitiveForm()) return { status: 'blocked' };

    const xArticle = extractXArticle();
    if (xArticle) return { status: 'ready', page: xArticle };
    // X timelines are endless feeds, not one bounded document. Only X's
    // dedicated long-form Article view is safe to cache as a complete page.
    if (/(^|\.)x\.com$/i.test(location.hostname)) return { status: 'no-content' };

    const page = extractGenericPage();
    return page ? { status: 'ready', page } : { status: 'no-content' };
  }

  function notifyStatus(state, detail = '') {
    chrome.runtime.sendMessage({ type: 'bridge-status', state, detail }, () => {
      void chrome.runtime.lastError;
    });
  }

  function fingerprint(page) {
    return [page.url, page.text.length, page.text.slice(0, 160), page.text.slice(-160)].join('|');
  }

  function publish(force = false) {
    if (publishing) return;
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
    chrome.runtime.sendMessage({ type: 'publish-page', page: result.page }, (response) => {
      publishing = false;
      if (chrome.runtime.lastError || !response?.ok) {
        notifyStatus('error', response?.error || 'Local chatbot is unavailable.');
        return;
      }
      lastFingerprint = mark;
      lastSentAt = Date.now();
      notifyStatus('ready', `${result.page.text.length} characters ready`);
    });
  }

  function schedulePublish() {
    if (publishTimer !== null) clearTimeout(publishTimer);
    publishTimer = setTimeout(() => {
      publishTimer = null;
      publish();
    }, DEBOUNCE_MS);
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === 'publish-now') publish(true);
  });

  new MutationObserver(schedulePublish).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
  document.addEventListener('visibilitychange', schedulePublish);
  window.addEventListener('pageshow', schedulePublish);
  setInterval(publish, HEARTBEAT_MS);
  schedulePublish();
})();
