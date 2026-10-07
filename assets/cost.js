/* OpenRouter Exact Cost — WebUI extension asset.
 *
 * Replaces the session cost in the composer's context tooltip (desktop
 * #ctxTooltipCost, mobile #composerMobileContextCost) with the amount
 * OpenRouter billed for this session, reconciled by this extension's sidecar
 * from OpenRouter's own generation records — and adds a per-model breakdown.
 *
 * The sidecar is reached through the WebUI's token-guarded proxy
 * (/api/extensions/<id>/sidecar/...); the WebUI injects the auth token on the
 * proxied request, so no secret is present in this file.
 *
 * WHY THERE IS A MutationObserver
 * The WebUI re-renders that cost line on every streaming tick, so a polling-only
 * correction loses the race and the tooltip visibly flickers between its own
 * "Estimated cost" text and ours. Observer callbacks are microtasks — they run
 * before the browser paints — so correcting there is invisible. A slow interval
 * remains as a safety net for what an observer cannot cover (element replaced
 * wholesale, tooltip not yet in the DOM).
 *
 * LABEL RULE
 *   "OpenRouter: $X"                                 normal case
 *   "OpenRouter: $X (+N calls from other providers)"  session also used another provider
 *   "OpenRouter: $X (+N unpriced)"                    a billed call had no usable record
 *   nothing at all                                    session never used OpenRouter
 *
 * The figure is only ever OpenRouter's own billing. When other providers are
 * involved it says so, rather than quietly implying it is the whole session.
 */
(function () {
  'use strict';

  var EXT = 'hermes-openrouter-exact-cost';
  var BASE = '/api/extensions/' + EXT + '/sidecar';
  var SAFETY_MS = 1500;    // re-attach observers / re-check (the observer does the real work)
  var REFRESH_MS = 15000;  // how often to re-ask the sidecar for the total
  var COST_IDS = ['ctxTooltipCost', 'composerMobileContextCost'];
  var TOOLTIP_COST_ID = 'ctxTooltipCost';
  var LABEL = 'OpenRouter: ';
  var BD_CLASS = 'orx-breakdown';

  var cache = { session: null, data: null, at: 0 };
  var inflight = false;
  var observers = [];

  function sessionId() {
    try {
      if (typeof state !== 'undefined' && state && state.sessionId) return String(state.sessionId);
    } catch (e) { /* not defined in this scope */ }
    var el = document.querySelector('[data-session-id]');
    if (el) return el.getAttribute('data-session-id');
    var m = /(?:session[=/])([A-Za-z0-9_.:-]{6,128})/i.exec(location.hash + location.search);
    return m ? m[1] : null;
  }

  function fmt(n) {
    if (typeof n !== 'number' || isNaN(n)) return null;
    return n < 0.01 ? n.toFixed(4) : n.toFixed(2);
  }

  function shortModel(name) {
    if (!name) return '?';
    var base = String(name).split('/').pop();
    return base.replace(/-\d{8}$/, '');
  }

  function refresh() {
    var sid = sessionId();
    if (!sid || inflight) return;
    if (cache.session === sid && Date.now() - cache.at < REFRESH_MS) return;
    inflight = true;
    fetch(BASE + '/api/session-cost?session=' + encodeURIComponent(sid), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && typeof d.total_usd === 'number') {
          cache = { session: sid, data: d, at: Date.now() };
          paint();
        }
      })
      .catch(function () { /* sidecar down, refused, or not consented yet */ })
      .then(function () { inflight = false; });
  }

  function buildLabel(d) {
    var label = LABEL + '$' + fmt(d.total_usd);
    if (d.other_provider_calls > 0) {
      label += ' (+' + d.other_provider_calls + ' call' +
        (d.other_provider_calls === 1 ? '' : 's') + ' from other providers)';
    }
    if (d.unpriced_calls > 0) {
      label += ' (+' + d.unpriced_calls + ' unpriced)';
    }
    return label;
  }

  function buildNote(d) {
    var note = 'Amount billed by OpenRouter for this session (' +
      d.generations + ' generations).';
    if (d.other_provider_calls > 0) {
      note += ' ' + d.other_provider_calls +
        ' call(s) were served by another provider and are not included.';
    }
    if (d.unpriced_calls > 0) {
      note += ' ' + d.unpriced_calls + ' OpenRouter call(s) could not be priced.';
    }
    return note;
  }

  function alreadyPainted(text) {
    return text.indexOf(LABEL) === 0;
  }

  function renderBreakdown(d) {
    var anchor = document.getElementById(TOOLTIP_COST_ID);
    if (!anchor || !anchor.parentNode) return;

    var models = [];
    var by = d.by_model || {};
    for (var k in by) {
      if (Object.prototype.hasOwnProperty.call(by, k)) {
        models.push({ name: k, cost: by[k].cost_usd, calls: by[k].calls });
      }
    }
    if (!models.length) return;
    models.sort(function (a, b) { return b.cost - a.cost; });

    var box = anchor.parentNode.querySelector('.' + BD_CLASS);
    if (!box) {
      box = document.createElement('div');
      box.className = BD_CLASS;
      if (anchor.nextSibling) anchor.parentNode.insertBefore(box, anchor.nextSibling);
      else anchor.parentNode.appendChild(box);
    }

    var sig = JSON.stringify(models);
    if (box.getAttribute('data-orx-sig') === sig) return;  // no needless DOM churn
    box.setAttribute('data-orx-sig', sig);

    while (box.firstChild) box.removeChild(box.firstChild);
    for (var i = 0; i < models.length; i++) {
      var row = document.createElement('div');
      row.className = 'orx-bd-row';
      var nm = document.createElement('span');
      nm.className = 'orx-bd-name';
      nm.textContent = shortModel(models[i].name);
      var cs = document.createElement('span');
      cs.className = 'orx-bd-cost';
      cs.textContent = '$' + fmt(models[i].cost) + ' \u00b7 ' + models[i].calls;
      row.appendChild(nm);
      row.appendChild(cs);
      box.appendChild(row);
    }
  }

  function paint() {
    var sid = sessionId();
    if (!sid || !cache.data || cache.session !== sid) return;
    var d = cache.data;
    if (!d.has_data) return;  // never used OpenRouter: leave the native line alone

    var label = buildLabel(d);
    var note = buildNote(d);

    for (var i = 0; i < COST_IDS.length; i++) {
      var el = document.getElementById(COST_IDS[i]);
      if (!el) continue;
      var cur = el.textContent || '';
      if (cur.indexOf('$') === -1) continue;      // no cost on this line yet
      if (alreadyPainted(cur)) continue;          // already corrected
      var tail = '';
      var sep = cur.indexOf('\u00b7');            // preserve "· 75% cached"
      if (sep !== -1) tail = ' ' + cur.slice(sep);
      el.textContent = label + tail;
      el.classList.add('orx-exact-cost');
      el.title = note + ' (' + cache.session + ')';
    }

    renderBreakdown(d);
  }

  function syncObservers() {
    var wanted = [];
    for (var i = 0; i < COST_IDS.length; i++) {
      var el = document.getElementById(COST_IDS[i]);
      var parent = el && el.parentNode;
      if (parent && wanted.indexOf(parent) === -1) wanted.push(parent);
    }
    // drop observers whose node no longer holds a cost line
    observers = observers.filter(function (o) {
      if (wanted.indexOf(o.node) !== -1) return true;
      o.obs.disconnect();
      return false;
    });
    // attach to any newly relevant node
    for (var w = 0; w < wanted.length; w++) {
      var node = wanted[w];
      var have = false;
      for (var j = 0; j < observers.length; j++) {
        if (observers[j].node === node) { have = true; break; }
      }
      if (have) continue;
      var obs = new MutationObserver(function () { paint(); });
      obs.observe(node, { childList: true, subtree: true, characterData: true });
      observers.push({ node: node, obs: obs });
    }
  }

  function tick() { syncObservers(); paint(); refresh(); }

  function start() {
    setInterval(tick, SAFETY_MS);
    tick();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
