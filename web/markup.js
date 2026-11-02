/* Item text as markdown that cannot run. An item's summary and body are store text that people
 * and agents write, so the node panel never takes them as markup: raw HTML (block and inline)
 * shows as text, and a link or an image keeps only an http, https or mailto address (any other
 * address shows as plain text). Attributes are escaped. Loaded after vendor/marked.min.js.
 * marked v12 (vendored) calls a renderer with strings; v13 and later can call it with a token,
 * so each renderer below takes either.
 */
(function () {
  'use strict';

  var SAFE_URL = /^(?:https?|mailto):/i;

  // Every markup character as an entity: for text that was never markup.
  function escapeAll(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // The same, but an entity marked already wrote stays as it is (marked escapes most text before
  // a renderer sees it, and leaves text inside <pre>, <code>, <kbd> or <script> raw).
  function escapeOnce(s) {
    return String(s == null ? '' : s)
      .replace(/&(?!#?[a-zA-Z0-9]+;)/g, '&amp;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  // The address to keep, or null when its scheme is not http, https or mailto.
  function safeUrl(href) {
    var url = String(href == null ? '' : href).trim();
    if (!SAFE_URL.test(url)) return null;
    try { return encodeURI(url).replace(/%25/g, '%'); } catch (_) { return null; }
  }
  function isToken(value) { return !!value && typeof value === 'object'; }
  function titleAttr(title) { return title ? ' title="' + escapeOnce(title) + '"' : ''; }

  var renderer = {
    html: function (html, block) {
      if (isToken(html)) { block = html.block; html = html.text; }
      var text = escapeAll(html);
      return block ? '<p>' + text.trim() + '</p>\n' : text;
    },
    link: function (href, title, text) {
      if (isToken(href)) { var token = href; href = token.href; title = token.title; text = this.parser.parseInline(token.tokens); }
      var url = safeUrl(href);
      // text is the link's own words, already rendered by this renderer.
      if (url === null) return text;
      return '<a href="' + escapeOnce(url) + '"' + titleAttr(title) + '>' + text + '</a>';
    },
    image: function (href, title, text) {
      if (isToken(href)) { var token = href; href = token.href; title = token.title; text = token.text; }
      var url = safeUrl(href);
      if (url === null) return escapeOnce(text);
      return '<img src="' + escapeOnce(url) + '" alt="' + escapeOnce(text) + '"' + titleAttr(title) + '>';
    },
    text: function (text) {
      if (isToken(text)) {
        if (text.tokens) return this.parser.parseInline(text.tokens);
        text = text.text;
      }
      return escapeOnce(text);
    },
  };

  function engine(lib) {
    if (!lib || typeof lib.parse !== 'function') return null;
    // A private instance when marked has one, so the global marked keeps its defaults.
    var md = typeof lib.Marked === 'function' ? new lib.Marked() : lib;
    md.use({ renderer: renderer });
    return md;
  }
  var md = engine(window.marked);

  window.GroveMarkup = {
    // Markdown as safe HTML; with no marked at all, the text escaped.
    render: function (text) { return md ? md.parse(String(text == null ? '' : text)) : escapeAll(text); },
    renderer: renderer,
    safeUrl: safeUrl,
  };
})();
