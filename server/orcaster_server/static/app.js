// app.js — all client-side logic of the dashboard.
// The JS does NOT depend on visual classes: it only selects by ID or by
// data-* attributes and toggles states (.is-active, .hidden, .show,
// .task-highlight). Changing the CSS should not break this file.

(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  // ---- Polling pause during interaction -----------------------------------
  // #tasks-panel polls with morph:innerHTML. If a swap lands between the
  // mousedown and the mouseup of a selection click, idiomorph rewrites the
  // target node and the browser does not emit the `click`: the click is
  // "eaten". To avoid it, the panel trigger gates ONLY the polling tick (not
  // `load` nor `refresh`) with the filter `every 5s [dashTasksIdle(this)]`.
  // htmx resolves `dashTasksIdle` against the global namespace, so it is
  // published on window; it must exist BEFORE the first tick, so the
  // assignment lives in the synchronous body of the IIFE, not in ready().
  //
  // The bulk selection size lives in the initBulk closure; we publish it in
  // this module variable so dashTasksIdle can read it without stray globals
  // or coupling to the DOM.
  let tasksBulkSelectionSize = 0;

  // Programmatic entry point to the confirm dialog (see initConfirmDialog).
  // initConfirmDialog assigns its `open(opts)` to it; the bulk handlers call
  // it instead of window.confirm()/prompt() to confirm with the same toned
  // dialog (danger/warn), safe focus and reason capture. It lives in the
  // synchronous body of the IIFE (not in ready()) to avoid the TDZ: the bulk
  // handlers only read it on a click, long after init.
  let confirmModal = null;

  // true  => the panel is "idle": the polling tick may proceed.
  // false => an interaction is in progress: skip the tick.
  function dashTasksIdle(el) {
    // htmx passes the element as `this`; if it is not the expected panel we
    // resolve it by id so we do not depend on the filter binding.
    const panel = el && el.id === 'tasks-panel'
      ? el
      : document.getElementById('tasks-panel');
    if (!panel) return true;                       // no panel, nothing to pause
    if (panel.matches(':hover')) return false;     // click gesture in progress
    if (panel.matches(':focus-within')) return false; // keyboard navigation
    if (tasksBulkSelectionSize > 0) return false;  // batch half assembled
    return true;
  }
  window.dashTasksIdle = dashTasksIdle;

  // Visible mirror of the polling pause state. It derives from the SAME
  // dashTasksIdle() predicate so the logic is not duplicated; it reflects
  // live/paused in data-state and updates the label (a DOM content change =>
  // aria-live announces it to screen readers; pure CSS would not). The
  // equality guard makes only real changes get announced.
  function syncPollIndicator() {
    const el = document.getElementById('poll-status');
    if (!el) return;
    const live = dashTasksIdle();
    const state = live ? 'live' : 'paused';
    if (el.dataset.state === state) return;
    el.dataset.state = state;
    const label = el.querySelector('.poll-status-label');
    if (label) label.textContent = live ? 'Live' : 'Paused';
  }

  function initPollIndicator() {
    const el = document.getElementById('poll-status');
    if (!el) return;
    // The pill is a pure function of dashTasksIdle() (hover, focus-within,
    // bulk size): instead of wiring it to every input (mouseenter/leave/focus
    // + refreshBar + heal in afterSwap), we re-derive it on a steady tick.
    // This covers any future pause condition without new plumbing and
    // subsumes the afterSwap heal (it re-reads the real :hover, which the
    // browser updates even when it does not emit mouseleave as the panel
    // shrinks).
    syncPollIndicator();
    // role=status implies aria-live=polite. We enable it AFTER the first
    // sample so the initial live->paused flip (pointer already over the list
    // on load) does not trigger a spurious announcement; only later changes
    // are announced. Without JS there are no state changes, so the role adds
    // nothing.
    el.setAttribute('role', 'status');
    setInterval(syncPollIndicator, 500);
  }

  // ---- htmx error toast --------------------------------------------------
  // Single surface for reporting failures of the dashboard's htmx actions.
  // The listener sits at `body` level and is not wired to specific buttons:
  // it covers at once all the actions that currently fail silently
  // (pause/resume with a 409 from an online->offline race, delete,
  // force-delete, clear cache). The confirm dialog flow (mode 1) has no error
  // handling of its own, and the alert() of mode 2 only reaches the buttons
  // with data-confirm-url.
  //
  // The auto-dismiss constant lives INSIDE the function on purpose: this
  // file loads with `defer`, so ready() runs synchronously and a module-level
  // `const` declared further down would hit the TDZ when invoked from there.
  //
  // The stack (#toast-stack) is NOT created here: initErrorToast() creates it
  // at startup, as an empty node already attached to the DOM. If it were
  // created on this first call, the appendChild below would happen in the
  // same synchronous task as the `role="status"` registration, which is the
  // classic live region gotcha: the screen reader needs the region to be
  // already registered in the accessibility tree BEFORE the mutation, or the
  // announcement is lost. With the pre-created stack, the first error of the
  // page load is announced too.
  //
  // Cap + dedupe: without this, an ungated poller (workers every 3s, tasks
  // every 5s) against a sustained 502 (restart with MIGRATE_ON_STARTUP) or an
  // expired session (401 indefinitely) fills the corner with toasts renewing
  // forever and covers the #kbd-toggle fixed right there.
  function showErrorToast(message) {
    const DISMISS_MS = 8000;
    const MAX_TOASTS = 3;
    const stack = document.getElementById('toast-stack');
    if (!stack) return; // without initErrorToast() there is nowhere to append.
    // Dedupe: if the same message is already on top, do not stack another.
    const lastText = stack.lastElementChild && stack.lastElementChild.querySelector('.toast-text');
    if (lastText && lastText.textContent === message) return;
    // Cap: at most MAX_TOASTS visible at once; the oldest leaves first.
    while (stack.children.length >= MAX_TOASTS) stack.firstElementChild.remove();
    const toast = document.createElement('div');
    toast.className = 'toast toast-error';
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
      // The timer is kept so it can be cancelled if the operator closes the
      // toast by hand; without this, closing did not prevent the setTimeout
      // from attempting a redundant remove() later.
    const timer = setTimeout(function () { toast.remove(); }, DISMISS_MS);
    close.addEventListener('click', function () {
      clearTimeout(timer);
      toast.remove();
    });
    toast.appendChild(text);
    toast.appendChild(close);
    stack.appendChild(toast);
  }

  // FastAPI serializes HTTPException as {"detail": "<text>"}; those texts
  // are written to be read ("worker in status 'offline' cannot be paused").
  // A 422 carries `detail` as a list and a proxy response is not JSON: in
  // those cases we fall back to the HTTP code.
  function htmxErrorMessage(xhr) {
    if (!xhr) return 'Request failed';
    try {
      const parsed = JSON.parse(xhr.responseText);
      if (parsed && typeof parsed.detail === 'string') return parsed.detail;
    } catch (_) { /* non-JSON response */ }
    return 'Request failed (HTTP ' + (xhr.status || '?') + ')';
  }

  function initErrorToast() {
    // Empty node attached to the DOM since startup (see the note in
    // showErrorToast): an empty <div> costs nothing visually and leaves the
    // live region registered before the first mutation.
    const stack = document.createElement('div');
    stack.id = 'toast-stack';
    stack.className = 'toast-stack';
    // role=status implies aria-live=polite: the message is announced without
    // stealing focus from the operator.
    stack.setAttribute('role', 'status');
    document.body.appendChild(stack);
    document.body.addEventListener('htmx:responseError', function (evt) {
      showErrorToast(htmxErrorMessage(evt.detail && evt.detail.xhr));
    });
  }

  ready(function () {
    configureIdiomorph();
    initHeaderDisclosures();
    initThemeToggle();
    initConfirmDialog();
    initErrorToast();
    initInfoTipsFloating();
    initCopyButtons();
    initRevealDialog();
    initDashboard();
    initPollIndicator();
    initTimestamps(document);
    initTzNotes(document);
    setInterval(refreshRelativeTimestamps, 30000);
    document.body.addEventListener('htmx:afterSwap', function (evt) {
      initTimestamps(evt.target || document);
      initTzNotes(evt.target || document);
    });
  });

  // ---- Inline substitutes: no `on*=`, no `style=`, no eval ---
  // The /admin CSP (`script-src 'self'; style-src 'self'`) forbids inline
  // handlers, `style=` attributes and the htmx eval (hx-on, hx-trigger
  // filters). Each one has its declarative replacement here:
  //
  //   data-select-on-click        → selects the input text on click
  //   data-after-request="..."    → action after a successful htmx request:
  //                                 reload | dashboard | refresh-workers
  //   data-pct="0.42"             → --pct on the element (progress bars)
  //   data-w="42"                 → width: 42% (storage bars)
  //
  // Assigning `el.style.*` through the CSSOM is NOT blocked by style-src (it
  // only blocks the attribute in the markup and inline <style>), which is why
  // the bars are painted from JS using the data-* values.
  function initSelectOnClick() {
    document.addEventListener('click', function (evt) {
      const el = evt.target;
      if (el && el.matches && el.matches('input[data-select-on-click]')) el.select();
    });
  }

  function initAfterRequest() {
    document.addEventListener('htmx:afterRequest', function (evt) {
      const d = evt.detail;
      if (!d || !d.successful || !d.elt || !d.elt.dataset) return;
      const action = d.elt.dataset.afterRequest;
      if (action === 'reload') window.location.reload();
      else if (action === 'dashboard') window.location = '/admin/';
      else if (action === 'refresh-workers') {
        window.htmx.trigger('.workers-pane-body', 'refresh');
      }
    });
  }

  function applyBarValues(root) {
    if (!root || root.nodeType !== 1) return;
    const nodes = root.matches('[data-pct], [data-w]')
      ? [root] : [];
    root.querySelectorAll('[data-pct], [data-w]').forEach(function (n) { nodes.push(n); });
    nodes.forEach(function (n) {
      if (n.dataset.pct !== undefined) n.style.setProperty('--pct', n.dataset.pct);
      if (n.dataset.w !== undefined) n.style.width = n.dataset.w + '%';
    });
  }

  function initBarValues() {
    applyBarValues(document.body);
    // MutationObserver: covers innerHTML, morph and SSE regardless of which
    // htmx event fires; it runs as a microtask, before the next paint.
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        if (m.type === 'attributes') applyBarValues(m.target);
        else m.addedNodes.forEach(applyBarValues);
      });
    }).observe(document.body, {
      subtree: true, childList: true,
      attributes: true, attributeFilter: ['data-pct', 'data-w'],
    });
  }

  // Tasks panel polling. The trigger filter `every 5s [dashTasksIdle(this)]`
  // is evaluated by htmx with eval (forbidden with allowEval=false), so the
  // tick lives here: if the panel is idle it fires the `refresh` trigger that
  // the panel already listens to.
  function initTasksPolling() {
    setInterval(function () {
      const panel = document.getElementById('tasks-panel');
      if (!panel) return;
      if (dashTasksIdle(panel)) window.htmx.trigger(panel, 'refresh');
    }, 5000);
  }

  ready(function () {
    initSelectOnClick();
    initAfterRequest();
    initBarValues();
    initTasksPolling();
  });

  // ---- One-time secret reveal --------------------------------------------
  // The secret (invite URL / enrollment token) is shown only once and cannot
  // be recovered. The template renders it as <dialog open> (visible and
  // copyable without JS); here we elevate it to showModal() — a dedicated,
  // blocking step — and keep "Done" disabled until a Copy fires. This avoids
  // losing the secret through an accidental click outside the box.
  function initRevealDialog() {
    const dlg = document.getElementById('secret-reveal');
    if (!dlg) return;
    const doneBtn = dlg.querySelector('[data-reveal-done]');
    const hint = dlg.querySelector('[data-reveal-hint]');
    let copied = false;

    function markCopied() {
      if (copied) return;
      copied = true;
      if (doneBtn) doneBtn.disabled = false;
      if (hint) hint.hidden = true;
    }

    function reallyClose() {
      if (typeof dlg.close === 'function') {
        try { dlg.close(); } catch (_) { dlg.removeAttribute('open'); }
      } else {
        dlg.removeAttribute('open');
      }
    }

    // Elevates the inline fallback to a blocking modal. showModal() throws if
    // it is already open, so we remove the attribute first.
    if (typeof dlg.showModal === 'function') {
      if (dlg.open) dlg.removeAttribute('open');
      try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); }
    }

    dlg.addEventListener('click', (ev) => {
      if (ev.target.closest('[data-reveal-copy]')) { markCopied(); return; }
      // Click on the backdrop: only closes once copied (blocked until then).
      if (ev.target === dlg && copied) reallyClose();
    });
    if (doneBtn) doneBtn.addEventListener('click', () => { if (copied) reallyClose(); });
    // Escape: blocked until copied; afterwards it closes cleanly.
    dlg.addEventListener('cancel', (ev) => { ev.preventDefault(); if (copied) reallyClose(); });
  }

  // ---- Inline copy-to-clipboard ------------------------------------------
  // Buttons [data-copy="<value>"] copy their attribute to the clipboard and
  // show inline feedback in `.copy-btn-feedback` for 1.6s. No toast: the
  // confirmation lives next to the trigger so as not to steal focus.
  // Delegation at document level so it survives htmx swaps.
  function initCopyButtons() {
    document.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button[data-copy]');
      if (!btn) return;
      ev.preventDefault();
      const value = btn.getAttribute('data-copy') || '';
      const feedback = btn.querySelector('.copy-btn-feedback');
      try {
        await navigator.clipboard.writeText(value);
        if (feedback) feedback.textContent = 'Copied';
      } catch {
        if (feedback) feedback.textContent = 'Copy failed';
      }
      btn.classList.add('is-copied');
      setTimeout(() => {
        btn.classList.remove('is-copied');
        if (feedback) feedback.textContent = '';
      }, 1600);
    });
  }

  // ---- Idiomorph defaults ------------------------------------------------
  // The two dashboard panels poll every 3s with hx-swap="morph:*".
  // By default idiomorph syncs ALL attributes of the new node onto the old
  // one: that destroys live UI state the server does not know about.
  // In particular:
  //   1. `open` on <details class="row-actions">: the open kebab would close
  //      instantly because the server always emits it without open.
  //   2. class `task-highlight` on <tr>: initHighlight adds it after the
  //      first swap; without preservation, the next morph removes it and
  //      the highlight flickers.
  // beforeAttributeUpdated allows vetoing specific updates while keeping
  // the merge of the other attributes (badges, hrefs, counts, etc).
  function configureIdiomorph() {
    if (typeof window.Idiomorph === 'undefined') return;
    const defaults = window.Idiomorph.defaults || {};
    defaults.callbacks = defaults.callbacks || {};
    const prev = defaults.callbacks.beforeAttributeUpdated;
    defaults.callbacks.beforeAttributeUpdated = function (attr, node, mutationType) {
      if (typeof prev === 'function' && prev(attr, node, mutationType) === false) {
        return false;
      }
      // Preserve `open` on any <details data-keep-open>: its state is
      // controlled by the user, not the server, so the poll morph must not
      // override it. Generic marker (it used to be an OR of row-actions/tele-pop
      // classes that had to be extended by hand for every new disclosure).
      // Templates that need this add data-keep-open to the <details>.
      if (attr === 'open'
          && node.tagName === 'DETAILS'
          && node.hasAttribute('data-keep-open')) {
        return false;
      }
      // `data-pct`/`data-w` bars: their `style` is written by JS (CSP without
      // inline style=), so the server markup never carries it. Without this
      // veto the morph would erase it on every poll and the bar would flicker
      // to 0.
      if (attr === 'style'
          && mutationType === 'remove'
          && node.hasAttribute
          && (node.hasAttribute('data-pct') || node.hasAttribute('data-w'))) {
        return false;
      }
      // Preserve `task-highlight` on <tr>: initHighlight paints it, not the
      // server. If the morph brings a class attribute without that class but
      // the current node has it, vetoing the update would lose the server's
      // new classes (status, etc). Instead of vetoing the attr, we let it
      // apply and re-add the class right after via afterNodeMorphed.
      return undefined;
    };
    const prevAfter = defaults.callbacks.afterNodeMorphed;
    defaults.callbacks.afterNodeMorphed = function (oldNode, newNode) {
      if (typeof prevAfter === 'function') prevAfter(oldNode, newNode);
      // Re-apply task-highlight if marked in window.__lastCreatedTaskId.
      // The "official" reapply is done by initHighlight on htmx:afterSwap, but
      // that listener runs only once after the whole swap; during the morph
      // the class is removed and reappears, which can break the CSS animation.
      // Here we hold it in place.
      if (oldNode && oldNode.nodeType === 1
          && oldNode.tagName === 'TR'
          && oldNode.id
          && window.__lastCreatedTaskId
          && oldNode.id === 'task-row-' + window.__lastCreatedTaskId) {
        oldNode.classList.add('task-highlight');
      }
    };
  }

  // ---- Header disclosures (navbar popovers) ------------------------------
  // The header popovers (.tele-pop telemetry, ws-switcher, user-menu) are
  // native <details>: they open/close with their summary, but by default they
  // do NOT close on an outside click or on Escape. We add that closing — a
  // gesture users expect from a popover — uniformly for the three. It only
  // applies to FLOATING popovers (desktop); below 1100px they open inline in
  // the hamburger accordion, where closing them on an outside tap would be
  // annoying, so we skip it when the mobile panel (.nav-collapse[open]) is
  // active.
  function initHeaderDisclosures() {
    // Idempotence guard: the listeners live on `document` and are not cleaned
    // up. Today initHeaderDisclosures is called only once from ready(), but if
    // someone wires it to htmx:afterSwap (like the timestamps init) the
    // listeners would stack. Same pattern as the rest of the inits in the file.
    if (document.__headerDisclosuresInit) return;
    document.__headerDisclosuresInit = true;

    var header = document.querySelector('body > header');
    if (!header) return;
    // Header popovers are marked with data-header-popover; this way adding a
    // new one does not require touching this selector. (The templates apply it
    // to .tele-pop, .ws-switcher-details and .user-menu-details.)
    var SEL = 'details[data-header-popover][open]';

    function inMobilePanel() {
      // The hamburger only opens inline below 1100px; above that the popovers
      // float and must auto-close. matchMedia queries the breakpoint
      // directly, without coupling to HOW the CSS hides the .nav-toggle.
      var burger = header.querySelector('details.nav-collapse');
      return !!(burger && burger.hasAttribute('open')
                && window.matchMedia('(max-width: 1100px)').matches);
    }

    document.addEventListener('pointerdown', function (ev) {
      if (inMobilePanel()) return;
      header.querySelectorAll(SEL).forEach(function (d) {
        if (!d.contains(ev.target)) d.removeAttribute('open');
      });
    });

    document.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Escape') return;
      if (inMobilePanel()) return;
      // Close ALL open header popovers (not only the first one),
      // symmetric with the pointerdown handler.
      header.querySelectorAll(SEL).forEach(function (d) {
        var hadFocusInside = d.contains(document.activeElement);
        d.removeAttribute('open');
        // Return focus to the summary ONLY if focus lived inside the popover
        // we closed. If the user was elsewhere (an input, another link), we
        // do not steal it.
        if (hadFocusInside) {
          var summary = d.querySelector('summary');
          if (summary) summary.focus();
        }
      });
    });
  }

  // ---- Theme toggle ------------------------------------------------------
  // States: "light" / "dark" persisted in localStorage. With no entry in
  // storage we fall back to the system prefers-color-scheme (resolved by
  // CSS). On click the "current" theme is computed by combining override +
  // system and the opposite one is persisted.
  function initThemeToggle() {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const root = document.documentElement;

    function currentTheme() {
      const forced = root.getAttribute('data-theme');
      if (forced === 'dark' || forced === 'light') return forced;
      return window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark' : 'light';
    }

    function applyAria() {
      const t = currentTheme();
      btn.setAttribute('aria-pressed', t === 'dark' ? 'true' : 'false');
      btn.setAttribute(
        'title',
        t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'
      );
    }

    btn.addEventListener('click', () => {
      const next = currentTheme() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('to-theme', next); } catch (_) { /* noop */ }
      applyAria();
    });

    // If the user never chose, we follow the system live.
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', () => {
      if (!root.getAttribute('data-theme')) applyAria();
    });

    applyAria();
  }

  // ---- Confirm dialog ---------------------------------------------------
  // Replaces the native window.confirm()/prompt(). Any button / link with
  //   data-confirm
  //   data-confirm-title="..."
  //   data-confirm-message="..."
  //   data-confirm-action="cancel|delete|destructive|primary"  (OK style)
  //   data-confirm-tone="warn|danger"                          (OK style)
  //   data-confirm-reason="Reason label"  (optional; visible textarea)
  //   data-confirm-url="/path"            (optional; if present, JS fires fetch)
  //   data-confirm-method="POST|DELETE"   (default by data-confirm-action)
  //   data-confirm-after="reload|dashboard|none"   (post-action)
  // Mode 1: the element IS an htmx trigger (hx-post/hx-delete + hx-confirm).
  //   The dialog intercepts htmx:confirm, asks for confirmation and, if OK,
  //   adds the HX-Prompt header with the reason before issueRequest(true).
  // Mode 2: the button is NOT an htmx trigger (it has data-confirm-url). The
  //   click opens the dialog; on OK, JS does a direct fetch and resolves
  //   data-confirm-after.
  function initConfirmDialog() {
    const dlg = document.getElementById('confirm-dialog');
    if (!dlg) return;
    const titleEl   = dlg.querySelector('#confirm-dialog-title');
    const messageEl = dlg.querySelector('#confirm-dialog-message');
    const reasonWrap = dlg.querySelector('#confirm-dialog-reason-wrap');
    const reasonLbl  = dlg.querySelector('#confirm-dialog-reason-label');
    const reasonInp  = dlg.querySelector('#confirm-dialog-reason');
    const okBtn      = dlg.querySelector('#confirm-dialog-ok');
    const cancelBtn  = dlg.querySelector('[data-confirm-cancel]');
    const form       = dlg.querySelector('.confirm-dialog-form');

    // Per-invocation state
    let resolver = null;

    function open(opts) {
      titleEl.textContent = opts.title || 'Confirm';
      messageEl.textContent = opts.message || '';
      messageEl.classList.toggle('hidden', !opts.message);

      // ALWAYS clear the textarea, not only when there is a reasonLabel:
      // close() returns reasonInp.value unconditionally, so a residual value
      // from a previous confirmation with a reason (e.g. bulkCancel) would
      // leak into the next confirmation WITHOUT a reason (e.g. a declarative
      // delete), which would resend it as HX-Prompt and pollute the audit.
      reasonInp.value = '';
      if (opts.reasonLabel) {
        reasonLbl.textContent = opts.reasonLabel;
        reasonWrap.classList.remove('hidden');
      } else {
        reasonWrap.classList.add('hidden');
      }

      okBtn.textContent = opts.okLabel || 'Confirm';
      okBtn.className = 'confirm-dialog-ok ' + (opts.okClass || '');
      // When the action IS "cancel", the OK says "Cancel task" — using
      // "Keep task" on the dismiss avoids the "Cancel" / "Cancel task"
      // collision.
      cancelBtn.textContent = opts.dismissLabel || 'Cancel';

      return new Promise(resolve => {
        resolver = resolve;
        if (typeof dlg.showModal === 'function') dlg.showModal();
        else dlg.setAttribute('open', '');
        // Safe initial focus. For destructive actions (btn-danger: delete /
        // force-delete) we focus the dismiss button, NOT the OK: a hasty Enter
        // must not fire an irreversible action — the operator must move to the
        // OK deliberately. Reversible (warn) or neutral actions keep the fast
        // flow: reason textarea if there is one (deletes have none), otherwise
        // the OK for an immediate Enter.
        const destructive = (opts.okClass || '').includes('btn-danger');
        // focusVisible:true forces the :focus-visible ring even if the dialog
        // was opened with the mouse: the operator MUST see which button is the
        // default (especially the dismiss on destructive actions). Safari
        // ignores the option and focuses without forcing the ring: clean
        // degradation.
        setTimeout(() => {
          if (destructive) cancelBtn.focus({ focusVisible: true });
          else if (opts.reasonLabel) reasonInp.focus();
          else okBtn.focus({ focusVisible: true });
        }, 0);
      });
    }

    function close(result) {
      const reasonValue = reasonInp.value.trim();
      if (typeof dlg.close === 'function') {
        try { dlg.close(); } catch (_) { dlg.removeAttribute('open'); }
      } else {
        dlg.removeAttribute('open');
      }
      const r = resolver;
      resolver = null;
      if (r) r({ ok: result === true, reason: reasonValue });
    }

    // form submit = OK; method=dialog fires submit but we want a custom path
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      close(true);
    });
    cancelBtn.addEventListener('click', () => close(false));
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(false); });
    // Click outside the article = close
    dlg.addEventListener('click', (e) => {
      if (e.target === dlg) close(false);
    });

    function optsFromElement(el) {
      const tone = el.dataset.confirmTone || '';
      const action = el.dataset.confirmAction || '';
      const okClass =
        tone === 'danger' || action === 'delete' ? 'btn-danger' :
        tone === 'warn'   || action === 'cancel' ? 'btn-warning' :
        '';
      let okLabel = el.dataset.confirmOkLabel || '';
      if (!okLabel) {
        okLabel = action === 'delete' ? 'Delete'
               : action === 'cancel' ? 'Cancel task'
               : 'Confirm';
      }
      const dismissLabel = el.dataset.confirmDismissLabel
        || (action === 'cancel' ? 'Keep task' : 'Cancel');
      return {
        title: el.dataset.confirmTitle || el.getAttribute('aria-label') || 'Confirm',
        message: el.dataset.confirmMessage || '',
        reasonLabel: el.dataset.confirmReason || '',
        okLabel: okLabel,
        okClass: okClass,
        dismissLabel: dismissLabel,
      };
    }

    // Mode 1 — intercept htmx:confirm
    document.body.addEventListener('htmx:confirm', function (evt) {
      const el = evt.detail.elt;
      if (!el || !el.hasAttribute || !el.hasAttribute('data-confirm')) return;
      evt.preventDefault();
      open(optsFromElement(el)).then(({ ok, reason }) => {
        if (!ok) return;
        if (reason) el.dataset.confirmReasonValue = reason;
        else delete el.dataset.confirmReasonValue;
        evt.detail.issueRequest(true);
      });
    });

    // Inject the HX-Prompt header with the reason. ALWAYS percent-encoded:
    // setRequestHeader throws (SyntaxError) on characters outside Latin-1,
    // so a reason with an emoji or typographic quotes would kill the request
    // before it leaves, with no toast (htmx:sendError, not htmx:responseError).
    // The server decodes it in reason_from_request
    // (orcaster_server/api/_reason.py).
    document.body.addEventListener('htmx:configRequest', function (evt) {
      const el = evt.detail.elt;
      if (!el || !el.dataset) return;
      const reason = el.dataset.confirmReasonValue;
      if (reason) {
        evt.detail.headers['HX-Prompt'] = encodeURIComponent(reason);
        delete el.dataset.confirmReasonValue;
      }
    });

    // Mode 2 — click handler for non-htmx buttons (data-confirm-url)
    document.body.addEventListener('click', async function (evt) {
      const el = evt.target.closest('[data-confirm][data-confirm-url]');
      if (!el) return;
      // if it is an anchor with hx-*, htmx:confirm would handle it: skip
      if (el.hasAttribute('hx-post') || el.hasAttribute('hx-delete') ||
          el.hasAttribute('hx-put')  || el.hasAttribute('hx-patch')) return;
      evt.preventDefault();
      const { ok, reason } = await open(optsFromElement(el));
      if (!ok) return;
      const url = el.dataset.confirmUrl;
      const method =
        el.dataset.confirmMethod
        || (el.dataset.confirmAction === 'delete' ? 'DELETE' : 'POST');
      const headers = { 'Accept': 'text/html, */*;q=0.5' };
      // Same percent-encoding as the htmx:configRequest listener above:
      // fetch() also rejects header values outside Latin-1.
      if (reason) headers['HX-Prompt'] = encodeURIComponent(reason);
      try {
        const resp = await fetch(url, { method, headers, credentials: 'same-origin' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const after = el.dataset.confirmAfter || 'reload';
        if (after === 'dashboard') window.location.href = '/admin/';
        else if (after === 'reload') window.location.reload();
        else if (after === 'none') { /* noop */ }
        else if (after.startsWith('navigate:')) {
          window.location.href = after.slice('navigate:'.length);
        }
      } catch (err) {
        alert('Action failed: ' + (err && err.message ? err.message : err));
      }
    });

    // Exposes the programmatic open() for callers without data-* (e.g. the
    // bulk handlers, which confirm from JS and then fetch). It returns the
    // same Promise<{ ok, reason }> as the declarative flow.
    confirmModal = open;
  }

  function initDashboard() {
    const modal = document.getElementById('new-task-modal');
    const tasksPanel = document.getElementById('tasks-panel');

    // initRowActions applies to ANY page with .table-wrap (workers,
    // tasks, admin-actions), not only the dashboard. That is why it is called
    // before the early return.
    initRowActions();
    initFork();
    initRetry();

    if (!modal && !tasksPanel) return;

    if (tasksPanel) initBulk(tasksPanel);
    if (tasksPanel) initRowNav(tasksPanel);
    if (modal)      initModal(modal);
    if (modal)      initNewTaskForm(modal);
    if (modal)      initCommandEditor();
    if (tasksPanel) initHighlight(tasksPanel);
    if (tasksPanel) initFilters(tasksPanel);
    if (tasksPanel) initSortSync(tasksPanel);
    initKeyboardShortcuts(modal);
    initFleetAdapt();
  }

  // ---- Sort persistence -------------------------------------------------
  // The _tasks_table.html partial emits sort headers as <button> with
  // hx-vals={"sort":..,"dir":..}. The click request ALREADY travels with
  // those correct values (htmx serializes hx-vals + the hx-include of the
  // filter-form). The problem is polling: the panel polls every 3s with
  // hx-include="#filter-form" and that only resends what the form contains.
  // If we do not update the hidden inputs after a sort click, the next
  // poll resets the order to the form default.
  //
  // Solution: capture the click on .th-sort-btn BEFORE htmx fires the
  // request, read data-sort-col / data-next-dir and write them into the
  // hidden inputs. htmx processes hx-vals with precedence and sends those
  // values in the request — but the hidden inputs stay in sync for the poll.
  function initSortSync(panel) {
    const sortHidden = document.getElementById('filter-sort-val');
    const dirHidden  = document.getElementById('filter-dir-val');
    if (!sortHidden || !dirHidden) return;

    const pageHidden     = document.getElementById('filter-page-val');
    const pageSizeHidden = document.getElementById('filter-page-size-val');

    // Boot from URL: if we arrive at /admin/?sort=status&dir=asc from a link
    // or after a hard refresh, mirror it into the form so the first poll
    // keeps it. initFilters' bootFromUrl handles status/q; here we extend it.
    const params = new URLSearchParams(window.location.search);
    const sortParam = (params.get('sort') || '').trim();
    const dirParam  = (params.get('dir')  || '').trim();
    if (sortParam) sortHidden.value = sortParam;
    if (dirParam === 'asc' || dirParam === 'desc') dirHidden.value = dirParam;

    // Click-capture: runs before htmx's internal listener on the button.
    // We delegate on document because the partial is re-rendered every 3s and
    // the buttons change node identity.
    document.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.th-sort-btn');
      if (!btn) return;
      const col  = btn.dataset.sortCol;
      const next = btn.dataset.nextDir;
      if (col)  sortHidden.value = col;
      if (next === 'asc' || next === 'desc') dirHidden.value = next;
      // Reset to page 1 when the sort changes.
      if (pageHidden) pageHidden.value = '1';
    }, true);

    // Prev/Next: sync the hidden input before the next tick's poll, and
    // remember the focus intent. At the edge (first/last
    // page) the clicked button becomes `disabled` after the morph and the
    // browser drops focus to <body>; we restore it to the actionable sibling.
    let pendingPagerFocus = null; // 'prev' | 'next' | null
    document.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.pagination-prev, .pagination-next');
      if (!btn || btn.disabled) return;
      const targetPage = btn.dataset.page;
      if (targetPage && pageHidden) pageHidden.value = targetPage;
      pendingPagerFocus = btn.classList.contains('pagination-next') ? 'next' : 'prev';
    }, true);

    // Restore focus after the swap ONLY if there was a pager click. Gated by
    // pendingPagerFocus → the 3s poll (which leaves it null) does not steal
    // focus.
    if (panel) {
      panel.addEventListener('htmx:afterSwap', () => {
        if (!pendingPagerFocus) return;
        const which = pendingPagerFocus;
        pendingPagerFocus = null;
        const prevBtn = panel.querySelector('.pagination-prev');
        const nextBtn = panel.querySelector('.pagination-next');
        const want     = which === 'next' ? nextBtn : prevBtn;
        const fallback = which === 'next' ? prevBtn : nextBtn;
        const target = (want && !want.disabled) ? want
                     : (fallback && !fallback.disabled) ? fallback
                     : null;
        if (target) {
          try { target.focus({ preventScroll: true }); } catch (_) { /* noop */ }
        }
      });
    }

    // page_size select: update hidden + reset to page 1 + refresh.
    document.addEventListener('change', (ev) => {
      const sel = ev.target.closest('.pagination-size-select');
      if (!sel) return;
      if (pageSizeHidden) pageSizeHidden.value = sel.value;
      if (pageHidden) pageHidden.value = '1';
      if (window.htmx) {
        const tasksPanel = document.getElementById('tasks-panel');
        if (tasksPanel) window.htmx.trigger(tasksPanel, 'refresh');
      }
    });
  }

  // ---- Keyboard shortcuts -----------------------------------------------
  // Global shortcuts, gmail/linear style. We do not intercept keys while the
  // user is typing (input/textarea/select/contenteditable) except Escape.
  // Sequence "g w" / "g t": after `g` there is an 800ms window in which the
  // next key is interpreted as the destination; anything else breaks the
  // sequence.
  function initKeyboardShortcuts(modal) {
    let pendingGoto = false;
    let pendingGotoTimer = null;

    function clearGoto() {
      pendingGoto = false;
      if (pendingGotoTimer) {
        clearTimeout(pendingGotoTimer);
        pendingGotoTimer = null;
      }
    }

    function scrollPaneIntoView(selector) {
      const pane = document.querySelector(selector);
      if (!pane) return;
      // Temporary tabindex=-1 so .focus() works without altering the
      // document tab order. We remove it on the next tick.
      const hadTabindex = pane.hasAttribute('tabindex');
      if (!hadTabindex) pane.setAttribute('tabindex', '-1');
      try { pane.focus({ preventScroll: true }); } catch (_) { /* noop */ }
      pane.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (!hadTabindex) {
        setTimeout(() => pane.removeAttribute('tabindex'), 0);
      }
    }

    function toggleCheatsheet(force) {
      const sheet = document.getElementById('kbd-cheatsheet');
      if (!sheet) return;
      const willOpen = (typeof force === 'boolean')
        ? force
        : !sheet.classList.contains('is-open');
      if (willOpen) {
        sheet.hidden = false;
        // requestAnimationFrame: give the browser a frame to register
        // hidden=false before applying the class that triggers the fade-in.
        requestAnimationFrame(() => sheet.classList.add('is-open'));
      } else {
        sheet.classList.remove('is-open');
        // Match the duration of the CSS transition (~220ms).
        setTimeout(() => {
          if (!sheet.classList.contains('is-open')) sheet.hidden = true;
        }, 240);
      }
    }

    // "×" button of the cheatsheet (close).
    // Floating "?" .kbd-toggle button (open) — permanent affordance of the
    // shortcut. If the sheet is already open, it toggles (closes).
    document.addEventListener('click', (ev) => {
      if (ev.target.closest('.kbd-cheatsheet-close')) {
        toggleCheatsheet(false);
        return;
      }
      if (ev.target.closest('.kbd-toggle')) {
        toggleCheatsheet();
      }
    });

    document.addEventListener('keydown', (ev) => {
      // Modifiers reserved for the system/browser: we do not intercept.
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

      const target = ev.target;
      const isEditing = target && target.matches
        && target.matches('input, textarea, select, [contenteditable=true], [contenteditable=""]');

      // Esc always gets through: clears the g sequence and closes the
      // cheatsheet if open.
      if (ev.key === 'Escape') {
        clearGoto();
        const sheet = document.getElementById('kbd-cheatsheet');
        if (sheet && sheet.classList.contains('is-open')) {
          ev.preventDefault();
          toggleCheatsheet(false);
        }
        // We do not touch the modal/kebab: they have their own listeners.
        return;
      }

      if (isEditing) return;

      // Pending "g w" / "g t" sequence.
      if (pendingGoto) {
        if (ev.key === 'w') {
          ev.preventDefault();
          clearGoto();
          scrollPaneIntoView('.workers-pane');
          return;
        }
        if (ev.key === 't') {
          ev.preventDefault();
          clearGoto();
          scrollPaneIntoView('.tasks-pane');
          return;
        }
        // Any other key breaks the sequence (and falls through to the normal
        // switch).
        clearGoto();
      }

      switch (ev.key) {
        case '/': {
          const q = document.getElementById('filter-q');
          if (q) {
            ev.preventDefault();
            q.focus();
            // If there was a text selection, we clear it so the user starts
            // typing without accidentally overwriting it.
            try { q.setSelectionRange(q.value.length, q.value.length); } catch (_) {}
            const sel = window.getSelection && window.getSelection();
            if (sel && sel.removeAllRanges) sel.removeAllRanges();
          }
          break;
        }
        case 'n': {
          if (modal && typeof modal.__openModal === 'function') {
            ev.preventDefault();
            modal.__openModal();
          } else {
            const openBtn = document.getElementById('open-new-task-btn');
            if (openBtn) { ev.preventDefault(); openBtn.click(); }
          }
          break;
        }
        case 'g': {
          ev.preventDefault();
          pendingGoto = true;
          pendingGotoTimer = setTimeout(() => { pendingGoto = false; }, 800);
          break;
        }
        case '?': {
          ev.preventDefault();
          toggleCheatsheet();
          break;
        }
        default:
          break;
      }
    }, false);
  }

  // ---- Fork (create a new task descending from this one) ----------------
  // "Fork" button in task detail. It used to post straight to /admin/tasks
  // with a pre-built body, with no chance to edit. Now it opens the "New
  // task" modal prefilled with the original's parameters; the user adjusts
  // whatever they want and fires a submit to /admin/tasks/{id}/fork. The
  // difference with Retry: Retry reuses the original task_id; Fork creates a
  // new one that records the original as `parent_task_id` (lineage). The
  // task_id is discarded and `idempotency_key`/`run_at` are not copied (see
  // _build_fork_body in admin_ui.py).
  function initFork() {
    // Delegated on document (not bound to the button). #fork-btn lives in
    // #task-detail-actions, which polling re-renders via hx-select-oob
    // every 3s: a listener bound to the concrete button would die after the
    // first swap. Delegating on document survives the swaps and we read the
    // live button's dataset on every click. The guard avoids double-binding
    // if initFork is called more than once.
    if (document.__forkClickDelegated) return;
    document.__forkClickDelegated = true;

    document.addEventListener('click', function (ev) {
      const btn = ev.target.closest('[data-fork]');
      if (!btn) return;
      ev.preventDefault();
      const modal = document.getElementById('new-task-modal');
      if (!modal) return;
      const resultEl = document.getElementById('fork-result');
      const setResult = (cls, text) => {
        if (!resultEl) return;
        resultEl.className = 'form-result ' + cls + ' task-detail-fork-result';
        while (resultEl.firstChild) resultEl.removeChild(resultEl.firstChild);
        resultEl.appendChild(document.createTextNode(text));
      };

      let body;
      try {
        body = JSON.parse(btn.dataset.forkBody || '{}');
      } catch (_) {
        setResult('err', 'Fork data malformed.');
        return;
      }
      const sourceId = btn.dataset.sourceTaskId || '';
      setResult('muted', '');

      // We reset the form to a clean state before prefilling, so leftovers
      // from a previous opening (errors, workers picker) do not mix with the
      // fork values.
      resetNewTask();
      // Marks the modal as "fork mode": the title, the submit and the success
      // flow read this. It is cleared in __closeModal.
      modal.dataset.forkMode = '1';
      modal.dataset.forkSourceId = sourceId;
      prefillNewTaskForm(body);

      if (typeof modal.__openModal === 'function') modal.__openModal();
    });
  }

  // Prefills the inputs of the "New task" modal with the fork body built
  // server-side by _build_fork_body. It accepts the `target` formats the
  // backend produces: numeric ("3"), "all-available", "worker:<id>",
  // "workers:<id1>,<id2>", "all:<id1>,<id2>". Any preselected worker that is
  // no longer online will be left unchecked after the picker renders
  // (makeWorkerRow unchecks it), and those that no longer exist on the server
  // are purged from the set (renderWorkersPicker).
  function prefillNewTaskForm(body) {
    const payload = body.payload || {};
    const cmdsEl = document.getElementById('nt-commands');
    if (cmdsEl) {
      const cmds = Array.isArray(payload.commands) ? payload.commands : [];
      cmdsEl.value = cmds.join('\n');
    }
    const reqsEl = document.getElementById('nt-requirements');
    if (reqsEl) {
      const reqs = Array.isArray(payload.requirements) ? payload.requirements : [];
      reqsEl.value = reqs.join('\n');
    }
    const etoEl = document.getElementById('nt-exec-timeout');
    if (etoEl) {
      etoEl.value = (payload.timeout_seconds != null) ? String(payload.timeout_seconds) : '';
    }

    const prioEl = document.getElementById('nt-priority');
    if (prioEl && body.priority != null) prioEl.value = String(body.priority);
    const ctoEl = document.getElementById('nt-claim-timeout');
    if (ctoEl) {
      ctoEl.value = (body.claim_timeout_seconds != null)
        ? String(body.claim_timeout_seconds) : '';
    }
    // run_at is not copied in _build_fork_body (we want to run now), but we
    // clear it in case the form was left with a previous value.
    const runAtEl = document.getElementById('nt-run-at');
    if (runAtEl) runAtEl.value = '';

    // Artifacts: _build_fork_body copies the full payload, so the `artifacts`
    // block travels in the fork body. We reflect it in the form or
    // submitNewTask would rebuild the payload without it and the fork would
    // lose the collection. `artifacts:{}` (present but empty) counts as
    // enabled.
    const artEnabledEl = document.getElementById('nt-artifacts-enabled');
    const art = (payload.artifacts && typeof payload.artifacts === 'object'
      && !Array.isArray(payload.artifacts)) ? payload.artifacts : null;
    if (artEnabledEl) {
      artEnabledEl.checked = art !== null;
      const incEl = document.getElementById('nt-artifacts-include');
      if (incEl) incEl.value = (art && Array.isArray(art.include)) ? art.include.join('\n') : '';
      const excEl = document.getElementById('nt-artifacts-exclude');
      if (excEl) excEl.value = (art && Array.isArray(art.exclude)) ? art.exclude.join('\n') : '';
      // `required` is a boolean in the contract and the three producers
      // —REST API, UI and CLI— emit it that way. We read with the SAME coercion
      // as the consumer (`bool(block.get("required"))` in
      // ArtifactsSpec.from_payload) so the checkbox reflects exactly what the
      // worker will do.
      const reqEl = document.getElementById('nt-artifacts-required');
      if (reqEl) reqEl.checked = !!(art && art.required);
      updateArtifactsFields();
    }

    // Isolation: _build_fork_body copies the full payload, so
    // task_env/egress_allowlist travel in the fork. We reflect them in the
    // form or submitNewTask would rebuild the payload without them (same as
    // artifacts). Tri-state: the PRESENCE of the key (even []) = isolation
    // active. The toggle is single for both lists, so an original with only
    // one key present is normalized to both on resend (a limitation of the
    // agreed model).
    const isoEnabledEl = document.getElementById('nt-isolation-enabled');
    if (isoEnabledEl) {
      const hasEnv = Array.isArray(payload.task_env);
      const hasEgress = Array.isArray(payload.egress_allowlist);
      const isoOn = hasEnv || hasEgress;
      isoEnabledEl.checked = isoOn;
      const teEl = document.getElementById('nt-task-env');
      if (teEl) teEl.value = hasEnv ? payload.task_env.join('\n') : '';
      const egEl = document.getElementById('nt-egress-allowlist');
      if (egEl) egEl.value = hasEgress ? payload.egress_allowlist.join('\n') : '';
      updateIsolationFields();
      const isoDetails = document.querySelector('#new-task-form details.isolation');
      if (isoDetails && isoOn) isoDetails.open = true;
    }

    applyTargetToForm(body.target || '');

    // If the clone brings overrides in advanced options, we expand the
    // <details> so the user sees them without having to hunt for them.
    const hasAdvanced = (body.priority != null && body.priority !== 0)
      || body.claim_timeout_seconds != null
      || payload.timeout_seconds != null;
    const advEl = document.querySelector('#new-task-form details.advanced');
    if (advEl) advEl.open = hasAdvanced;
    // Same with artifacts: if the clone brings them, we open its <details>.
    const artDetails = document.querySelector('#new-task-form details.artifacts');
    if (artDetails && art !== null) artDetails.open = true;
  }

  // Shows/hides a block of sub-fields according to its checkbox. Idempotent;
  // it is called when opening the modal, on reset, when prefilling a fork and
  // when the checkbox changes. Common base of artifacts (Collect output
  // artifacts) and isolation (Enable isolation).
  function toggleFields(enabledId, fieldsId) {
    const enabled = document.getElementById(enabledId);
    const fields = document.getElementById(fieldsId);
    if (!enabled || !fields) return;
    fields.classList.toggle('hidden', !enabled.checked);
  }

  function updateArtifactsFields() {
    toggleFields('nt-artifacts-enabled', 'nt-artifacts-fields');
  }

  function updateIsolationFields() {
    toggleFields('nt-isolation-enabled', 'nt-isolation-fields');
  }

  // Parses the server-side `target` string into (mode, count or worker pool)
  // and moves the form controls. For modes with workers, it injects the set
  // into workerPickerSel BEFORE changing the radio: updateTargetMode fires
  // loadWorkersPicker, and the render reads from that Set to mark the
  // checkboxes.
  function applyTargetToForm(target) {
    const t = String(target || '').trim();
    let mode = 'count';
    let count = 1;
    let workers = [];

    if (t === 'all-available') {
      mode = 'all-available';
    } else if (t.startsWith('all:')) {
      mode = 'all-subset';
      workers = t.slice(4).split(',').map(s => s.trim()).filter(Boolean);
    } else if (t.startsWith('workers:')) {
      mode = 'worker';
      workers = t.slice(8).split(',').map(s => s.trim()).filter(Boolean);
    } else if (t.startsWith('worker:')) {
      mode = 'worker';
      workers = [t.slice(7).trim()].filter(Boolean);
    } else if (/^\d+$/.test(t)) {
      mode = 'count';
      count = parseInt(t, 10) || 1;
    }

    const countEl = document.getElementById('nt-target-count');
    if (countEl) countEl.value = String(count);

    workerPickerSel.clear();
    workers.forEach(id => workerPickerSel.add(id));

    const radio = document.querySelector(`input[name="target_mode"][value="${mode}"]`);
    if (radio) radio.checked = true;
    updateTargetMode();
  }

  // ---- Retry (reuse same task) ------------------------------------------
  // "Retry" button in task detail. It reuses the same task (same task_id) and
  // fires a new execution via POST /admin/tasks/<id>/retry. It preserves the
  // cache (ORCASTER_CACHE_DIR), idempotency_key and attempt history.
  // It is only enabled when the task is in a terminal state
  // (completed/cancelled/failed); the template sets disabled+aria-disabled
  // and JS adds an extra guard in case a forced click arrives.
  // Source of truth: backend `_TERMINAL_TASK_STATES` (admin_tasks.py) —
  // completed/failed/cancelled are terminal at the task level (a
  // 'completed' may carry an outcome, but it is still 'completed');
  // `expired` exists at the execution level, NOT the task level. The /retry
  // endpoint returns 409 for any other state, so the JS guard must mirror
  // that exact set.
  const TERMINAL_TASK_STATUSES = new Set(['completed', 'failed', 'cancelled']);

  function initRetry() {
    // Delegated on document (same reason as initFork): #retry-btn lives in
    // #task-detail-actions, re-rendered by polling every 3s. We read
    // taskId/status from the live button on every click, not at init time
    // (capturing them at init would leave them stale after the swap). Guard
    // against double-binding.
    if (document.__retryClickDelegated) return;
    document.__retryClickDelegated = true;

    document.addEventListener('click', async function (ev) {
      const btn = ev.target.closest('#retry-btn');
      if (!btn) return;
      ev.preventDefault();
      const resultEl = document.getElementById('retry-result');
      const taskId = btn.dataset.taskId;
      const status = btn.dataset.taskStatus || '';
      const setResult = (cls, text) => {
        if (!resultEl) return;
        resultEl.className = 'form-result ' + cls + ' task-detail-retry-result';
        while (resultEl.firstChild) resultEl.removeChild(resultEl.firstChild);
        resultEl.appendChild(document.createTextNode(text));
      };

      if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return;
      if (!TERMINAL_TASK_STATUSES.has(status)) {
        setResult('err', 'Retry is available only for tasks in a terminal state.');
        return;
      }
      if (!taskId) {
        setResult('err', 'Missing task id.');
        return;
      }
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      setResult('muted', 'Retrying…');
      // "Retry on the same worker" opt-in. The checkbox lives outside the
      // 3s-polled actions row so its state survives swaps; absent for
      // all_available tasks (the flag is ignored there by design) — then we
      // fall back to the default (any eligible worker).
      const sameWorkerBox = document.getElementById('retry-same-worker');
      const forceSameWorker = !!(sameWorkerBox && sameWorkerBox.checked);
      try {
        const r = await fetch(`/admin/tasks/${taskId}/retry`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force_same_worker: forceSameWorker }),
        });
        if (!r.ok) {
          const txt = await r.text();
          let detail = txt;
          try {
            const j = JSON.parse(txt);
            if (j && j.detail) {
              detail = Array.isArray(j.detail)
                ? j.detail.map(d => `${(d.loc || []).join('.')}: ${d.msg}`).join(' · ')
                : j.detail;
            }
          } catch (_) { /* no JSON */ }
          setResult('err', `Error ${r.status}: ${String(detail).slice(0, 300)}`);
          // No re-enable on 409 (task no longer terminal — the page will reflect that on reload).
          return;
        }
        const out = await r.json();
        if (resultEl) {
          resultEl.className = 'form-result ok task-detail-retry-result';
          while (resultEl.firstChild) resultEl.removeChild(resultEl.firstChild);
          const attempt = (out.attempt_number !== undefined && out.attempt_number !== null)
            ? `#${out.attempt_number}` : '';
          resultEl.appendChild(document.createTextNode(`Retried — attempt ${attempt} on `));
          if (out.target_worker_id) {
            const link = document.createElement('a');
            link.href = `/admin/workers/${out.target_worker_id}`;
            link.textContent = `worker ${String(out.target_worker_id).slice(0, 8)}`;
            link.className = 'retry-result-link';
            link.title = 'Attempt number is per-(task, worker), not global over the task.';
            resultEl.appendChild(link);
          } else {
            const span = document.createElement('span');
            span.textContent = 'any available worker';
            span.title = 'Attempt number is per-(task, worker). If a different worker picks this up, its attempt count starts at #1.';
            resultEl.appendChild(span);
          }
          resultEl.appendChild(document.createTextNode('.'));
        }
        // Refresh page to reflect new execution + updated status.
        setTimeout(() => { window.location.reload(); }, 700);
      } catch (e) {
        setResult('err', 'Network error: ' + e.message);
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
      }
    });
  }

  // ---- Fleet-adaptive layout --------------------------------------------
  // Workers-pane: vertical aside when the fleet is >= FLEET_LARGE_AT, compact
  // horizontal band when it is small. The decision is by number of
  // workers, not by viewport: a 4K screen with 1 worker must look the same
  // as a 1280px one with 1 worker. We re-evaluate on every swap of the
  // partial.
  function initFleetAdapt() {
    const shell = document.querySelector('.dashboard-shell');
    const body = document.querySelector('.workers-pane-body');
    if (!shell || !body) return;
    const FLEET_LARGE_AT = 4; // ≥ 4 → vertical aside; ≤ 3 → horizontal compact

    function apply() {
      const count = body.querySelectorAll('.worker-card').length;
      const isSmall = count > 0 && count < FLEET_LARGE_AT;
      shell.classList.toggle('fleet-small', isSmall);
      shell.classList.toggle('fleet-large', !isSmall);
    }

    apply();
    body.addEventListener('htmx:afterSwap', apply);
    // The first load of the partial may happen before DOMContentLoaded
    // if htmx arrives fast. The initial apply() covers that case; afterSwap
    // the subsequent ones.
  }

  // ---- Status chip filters + search ------------------------------------
  // The #filter-form form is the source of truth. Chips are <button
  // aria-pressed>: toggling the state updates aria-pressed and the hidden
  // input #filter-status-val with a CSV of the active chips. After each
  // toggle we fire the form's hx-get to refresh the table. The polling
  // of #tasks-panel already includes this form via hx-include, so the
  // state survives the 3s ticks.
  function initFilters(tasksPanel) {
    const form = document.getElementById('filter-form');
    if (!form) return;
    const hidden = document.getElementById('filter-status-val');
    const allChip = form.querySelector('.status-chip--all');
    // Excludes both "All" and the outcome chips (a separate mechanism, see
    // outcomeChips below): without :not([data-outcome]) an outcome would
    // slip into the `status` CSV and break the filter.
    const statusChips = form.querySelectorAll(
      '.status-chip:not(.status-chip--all):not([data-outcome])'
    );
    // Dynamic outcome chips: same toggle mechanism as status,
    // but a separate CSV (`#filter-outcome-val`). They are rendered only for
    // the outcomes present (dashboard.html), so the set may be empty.
    const outcomeHidden = document.getElementById('filter-outcome-val');
    const outcomeChips = form.querySelectorAll('.status-chip[data-outcome]');

    // ---- Chip block clamp -------------------------------------------------
    // The chip vocabulary grows at runtime (7 fixed statuses + up to 20
    // outcomes present in the workspace) and wrapping with no ceiling left the
    // toolbar with no vertical brake: measured with 20 outcomes, 344px at
    // 1512px and 566px at 390px — 67% of the viewport, with the whole table
    // below the fold. We bound the block to `--chip-lines` lines (the
    // threshold lives in CSS, next to the rest of the rail layout) by hiding
    // chips from the end and publishing the rest behind an `N more`
    // disclosure.
    //
    // Two invariants:
    //   1. Only OUTCOME chips are hidden. The 7 status chips are the
    //      dashboard's fixed vocabulary and are always in view.
    //   2. An ACTIVE chip is never hidden. An applied filter hidden behind a
    //      disclosure is a filtered table with no visible explanation; if an
    //      active outcome forces an extra line, the block takes it.
    // The server ordering outcomes by frequency (admin_ui.py) is what makes
    // "hide from the end" useful: what gets hidden is the long tail, not
    // whatever starts with a late letter.
    const chipsWrap = document.getElementById('status-chips-wrap');
    const chipMoreBtn = document.getElementById('chip-overflow-toggle');
    const chipMoreLabel = chipMoreBtn
      ? chipMoreBtn.querySelector('.chip-overflow-label')
      : null;
    // The button's `aria-expanded` IS the disclosure state. No mirror
    // variable: the attribute has to be right anyway (the screen reader reads
    // it and the CSS uses it to rotate the chevron), so duplicating it in JS
    // only adds a pair that can fall out of sync.
    const chipsExpanded = () => chipMoreBtn.getAttribute('aria-expanded') === 'true';

    function chipLineLimit() {
      const raw = getComputedStyle(chipsWrap).getPropertyValue('--chip-lines');
      const n = parseInt(raw, 10);
      return (Number.isFinite(n) && n > 0) ? n : 2;
    }

    // Counts the block's lines by distinct offsetTop among its visible
    // children. Measuring real positions (rather than estimating text width)
    // is the only thing that survives fonts, zoom and browser translations.
    // The discard goes by `offsetParent`, which is what the layout says, and
    // not by `el.hidden`, which is what we asked for: if the CSS rule for
    // `button[hidden]` were lost, trusting the attribute would count as
    // trimmed a chip that still takes up its line and the clamp would stop
    // believing it fits. The symptom would be an overflowing rail instead of
    // a loop that does not converge, with nothing in the JS to give it away.
    function chipVisibleLines() {
      const tops = new Set();
      Array.from(chipsWrap.children).forEach(el => {
        if (el.offsetParent === null) return;
        tops.add(el.offsetTop);
      });
      return tops.size;
    }

    function applyChipClamp() {
      if (!chipsWrap || !chipMoreBtn || outcomeChips.length === 0) return;
      const hideable = Array.from(outcomeChips).filter(
        c => c.getAttribute('aria-pressed') !== 'true'
      );
      // We always start from "everything visible": the clamp is recomputed in
      // full on every pass instead of accumulating state, so a resize that
      // widens brings chips back instead of leaving them hidden forever.
      hideable.forEach(c => { c.hidden = false; });
      chipMoreBtn.hidden = true;
      const limit = chipLineLimit();
      if (chipVisibleLines() <= limit) {
        // It fits entirely: there is nothing to reveal, not even if the user
        // came from expanding by hand.
        chipMoreBtn.setAttribute('aria-expanded', 'false');
        return;
      }
      // The button itself takes space on the last visible line: it is shown
      // BEFORE measuring the trim so it does not drop to an extra line.
      chipMoreBtn.hidden = false;
      const expanded = chipsExpanded();
      // ...and it is shown with its WIDEST POSSIBLE LABEL, not the one it
      // had. The final count is only known when the loop ends, so measuring
      // with the old label measures a button of a different width: coming
      // from "less" (4 characters) the block seemed to fit one line earlier
      // than it really did and the clamp ended at 3 lines with the ceiling at
      // 2 (measured: collapsing after expanding left 4 outcomes visible and
      // 168px of toolbar instead of 124px). `hideable.length` is the upper
      // bound of the count and `tabular-nums` fixes the width per digit, so
      // the final label is never wider than the one measured.
      if (chipMoreLabel) {
        chipMoreLabel.textContent = expanded ? 'less' : `${hideable.length} more`;
      }
      // How many chips the loop trimmed. Expanded, none is trimmed, so what
      // the disclosure reveals there is all the hideable ones.
      let hiddenCount = 0;
      if (!expanded) {
        for (let i = hideable.length - 1; i >= 0; i--) {
          if (chipVisibleLines() <= limit) break;
          hideable[i].hidden = true;
          hiddenCount++;
        }
      }
      if ((expanded ? hideable.length : hiddenCount) === 0) {
        // The block exceeds the ceiling but not because of hideable chips
        // (fixed statuses + active outcomes no longer fit). There is nothing
        // behind the disclosure: showing it would be a button that reveals
        // nothing.
        chipMoreBtn.hidden = true;
        return;
      }
      // Expanded, the label was already set to 'less' above and does not
      // change; only the collapsed-mode count is known once the loop ends.
      if (chipMoreLabel && !expanded) {
        chipMoreLabel.textContent = `${hiddenCount} more`;
      }
      chipMoreBtn.title = expanded
        ? 'Collapse the outcome filters back to the visible rows'
        : `Show ${hiddenCount} more outcome filter${hiddenCount === 1 ? '' : 's'}`;
    }

    if (chipMoreBtn) {
      chipMoreBtn.addEventListener('click', () => {
        chipMoreBtn.setAttribute('aria-expanded', chipsExpanded() ? 'false' : 'true');
        applyChipClamp();
      });
    }

    // Recompute when the available width changes (viewport, workers sidebar
    // in fleet-small/large, zoom). We observe the RAIL and not the chip
    // block: hiding chips changes the block's height and the observer would
    // re-fire itself. The width guard is the definitive loop cutoff — the
    // clamp does not alter the rail's width, only its height.
    const chipRail = chipsWrap
      ? (chipsWrap.closest('.filter-bar-main') || chipsWrap.parentElement)
      : null;
    if (chipRail && chipMoreBtn && window.ResizeObserver) {
      let lastRailWidth = -1;
      new ResizeObserver(entries => {
        const w = Math.round(entries[0].contentRect.width);
        if (w === lastRailWidth) return;
        lastRailWidth = w;
        applyChipClamp();
      }).observe(chipRail);
    }

    function syncOutcomeHidden() {
      const active = [];
      outcomeChips.forEach(c => {
        if (c.getAttribute('aria-pressed') === 'true') active.push(c.dataset.outcome);
      });
      if (outcomeHidden) outcomeHidden.value = active.join(',');
      return active.length;
    }

    function syncHidden() {
      const active = [];
      statusChips.forEach(c => {
        if (c.getAttribute('aria-pressed') === 'true') active.push(c.dataset.status);
      });
      if (hidden) hidden.value = active.join(',');
      const outcomeActive = syncOutcomeHidden();
      // "All" is marked when neither a status NOR an outcome is active.
      if (allChip) {
        allChip.setAttribute(
          'aria-pressed', (active.length === 0 && outcomeActive === 0) ? 'true' : 'false'
        );
      }
      return active.length;
    }

    function clearAllStatus() {
      statusChips.forEach(c => c.setAttribute('aria-pressed', 'false'));
      outcomeChips.forEach(c => c.setAttribute('aria-pressed', 'false'));
      if (allChip) allChip.setAttribute('aria-pressed', 'true');
      if (hidden) hidden.value = '';
      if (outcomeHidden) outcomeHidden.value = '';
      // Re-clamp: an active outcome was forced into view and might have cost
      // the rail an extra line; on reset it loses that right and the block
      // returns to its ceiling. Here it is safe to recompute —and not on the
      // toggle of a single chip— because the gesture is "clear everything":
      // the pointer is over "All" or over the empty-row button, never over a
      // chip that would disappear from under it.
      applyChipClamp();
    }

    function syncArtifactDisabled() {
      const sel = document.getElementById('filter-artifacts');
      const disabled = !!sel && sel.value === 'no';
      ['filter-artifact-path', 'filter-artifact-type', 'filter-artifact-sha'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = disabled;
      });
    }

    // Artifact filter disclosure: the main rail's trigger toggles the
    // advanced row and paints the count of active filters (visible even when
    // the row is collapsed, so an applied filter is never invisible).
    // Disabled inputs do not count: they are not submitted.
    const artRow = document.getElementById('filter-artifacts-row');
    const artToggle = document.getElementById('filter-artifacts-toggle');

    function countArtifactFilters() {
      let n = 0;
      const sel = document.getElementById('filter-artifacts');
      if (sel && sel.value !== 'any') n += 1;
      ['filter-artifact-path', 'filter-artifact-type', 'filter-artifact-sha'].forEach(id => {
        const el = document.getElementById(id);
        if (el && !el.disabled && el.value.trim() !== '') n += 1;
      });
      return n;
    }

    function syncArtifactBadge() {
      if (!artToggle) return;
      const n = countArtifactFilters();
      const countEl = document.getElementById('filter-artifacts-count');
      if (countEl) countEl.textContent = n > 0 ? String(n) : '';
      artToggle.classList.toggle('has-active', n > 0);
    }

    function setArtifactsRowOpen(open) {
      if (!artRow || !artToggle) return;
      artRow.hidden = !open;
      artToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    if (artToggle && artRow) {
      artToggle.addEventListener('click', () => setArtifactsRowOpen(artRow.hidden));
    }

    function clearAllFilters() {
      clearAllStatus();
      const q = document.getElementById('filter-q');
      if (q) q.value = '';
      const sel = document.getElementById('filter-artifacts');
      if (sel) sel.value = 'any';
      ['filter-artifact-path', 'filter-artifact-type', 'filter-artifact-sha'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      syncArtifactDisabled();
      syncArtifactBadge();
    }

    function submitForm() {
      // htmx.trigger does NOT emit 'submit' on the form; we use htmx.ajax so
      // the form builds the query string from its inputs (including the
      // hidden one and the search) and replaces #tasks-panel.
      if (window.htmx) {
        window.htmx.trigger(form, 'submit');
      } else {
        form.submit();
      }
    }

    // Toggle of a status chip (not "All").
    statusChips.forEach(chip => {
      chip.addEventListener('click', () => {
        const pressed = chip.getAttribute('aria-pressed') === 'true';
        chip.setAttribute('aria-pressed', pressed ? 'false' : 'true');
        syncHidden();
        const pageHidden = document.getElementById('filter-page-val');
        if (pageHidden) pageHidden.value = '1';
        submitForm();
      });
    });

    // Toggle of an outcome chip — identical to the status one.
    outcomeChips.forEach(chip => {
      chip.addEventListener('click', () => {
        const pressed = chip.getAttribute('aria-pressed') === 'true';
        chip.setAttribute('aria-pressed', pressed ? 'false' : 'true');
        syncHidden();
        const pageHidden = document.getElementById('filter-page-val');
        if (pageHidden) pageHidden.value = '1';
        submitForm();
      });
    });

    // "All" works as a reset of the status block.
    if (allChip) {
      allChip.addEventListener('click', () => {
        // If it is already marked and there is nothing else, no-op to avoid a
        // redundant request.
        const anyActive = Array.from(statusChips).some(
          c => c.getAttribute('aria-pressed') === 'true'
        ) || Array.from(outcomeChips).some(
          c => c.getAttribute('aria-pressed') === 'true'
        );
        if (!anyActive && allChip.getAttribute('aria-pressed') === 'true') return;
        clearAllStatus();
        const pageHidden = document.getElementById('filter-page-val');
        if (pageHidden) pageHidden.value = '1';
        submitForm();
      });
    }

    // "Clear filters" button in the empty row. The partial is re-rendered
    // every 3s + after every submit; we use delegation on document so
    // the listener survives the swap.
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('#filter-clear-btn');
      if (!btn) return;
      clearAllFilters();
      submitForm();
    });

    // Reset to page 1 when typing in search (before the HTMX request).
    const qInput = document.getElementById('filter-q');
    if (qInput) {
      qInput.addEventListener('input', () => {
        const pageHidden = document.getElementById('filter-page-val');
        if (pageHidden) pageHidden.value = '1';
      });
    }

    // Reset to page 1 when any artifact filter changes (same as q).
    ['filter-artifact-path', 'filter-artifact-type', 'filter-artifact-sha'].forEach(id => {
      const el = document.getElementById(id);
      if (el) {
        el.addEventListener('input', () => {
          const pageHidden = document.getElementById('filter-page-val');
          if (pageHidden) pageHidden.value = '1';
          syncArtifactBadge();
        });
      }
    });
    const artifactsSelect = document.getElementById('filter-artifacts');
    if (artifactsSelect) {
      artifactsSelect.addEventListener('change', () => {
        const pageHidden = document.getElementById('filter-page-val');
        if (pageHidden) pageHidden.value = '1';
        syncArtifactDisabled();
        syncArtifactBadge();
      });
    }

    // Variant lens: changing the version resets to page 1 (like any
    // filter) and re-syncs `data-off-current` (a subtle mark via CSS when you
    // are NOT on the live version). The <select>'s own hx-trigger="change"
    // fires the request; this listener only adjusts prior state.
    const variantSelect = document.getElementById('filter-variant');
    if (variantSelect) {
      variantSelect.addEventListener('change', () => {
        const pageHidden = document.getElementById('filter-page-val');
        if (pageHidden) pageHidden.value = '1';
        const v = variantSelect.value;
        variantSelect.dataset.offCurrent = (v && v !== 'current') ? 'true' : 'false';
      });
    }

    // Boot: apply filters from the URL if they come in ?status= and ?q=.
    // This lets /admin/?status=failed&q=pip or a redirect from
    // /admin/tasks?status=… start with the right chips.
    (function bootFromUrl() {
      const params = new URLSearchParams(window.location.search);
      const statusParam = (params.get('status') || '').trim();
      const outcomeParam = (params.get('outcome') || '').trim();
      const qParam = (params.get('q') || '').trim();
      let applied = false;
      if (statusParam) {
        const tokens = statusParam.split(',').map(s => s.trim()).filter(Boolean);
        statusChips.forEach(c => {
          c.setAttribute('aria-pressed', tokens.includes(c.dataset.status) ? 'true' : 'false');
        });
        applied = applied || tokens.length > 0;
      }
      if (outcomeParam) {
        const tokens = outcomeParam.split(',').map(s => s.trim()).filter(Boolean);
        outcomeChips.forEach(c => {
          c.setAttribute('aria-pressed', tokens.includes(c.dataset.outcome) ? 'true' : 'false');
        });
        applied = applied || tokens.length > 0;
      }
      if (qParam) {
        const qInput = document.getElementById('filter-q');
        if (qInput) qInput.value = qParam;
        applied = true;
      }
      const haParam = (params.get('has_artifacts') || '').trim();
      const apParam = (params.get('artifact_path') || '').trim();
      const atParam = (params.get('artifact_type') || '').trim();
      const asParam = (params.get('artifact_sha256') || '').trim();
      const selEl = document.getElementById('filter-artifacts');
      if (selEl && ['any', 'yes', 'no'].includes(haParam)) {
        selEl.value = haParam;
        applied = applied || haParam !== 'any';
      }
      const setIf = (id, val) => {
        const el = document.getElementById(id);
        if (el && val) { el.value = val; applied = true; }
      };
      setIf('filter-artifact-path', apParam);
      setIf('filter-artifact-type', atParam);
      setIf('filter-artifact-sha', asParam);
      syncHidden();
      syncArtifactDisabled();
      syncArtifactBadge();
      // Initial clamp AFTER applying the URL filters: only here do we know
      // which chips are active, and an active one cannot end up behind the
      // disclosure. The ResizeObserver also fires once on observe,
      // but its callback is asynchronous and there is no reason to leave the
      // first paint without a clamp waiting for it.
      applyChipClamp();
      // Deep link with active artifact filters: the row auto-opens
      // so the operator immediately sees why the table is filtered
      // (the inputs travel anyway with the row collapsed, but
      // the state must not be hidden behind the disclosure).
      if (countArtifactFilters() > 0) setArtifactsRowOpen(true);
      // If we applied any filter from the URL, re-fire the panel.
      // hx-include reads the form at request time: if htmx already
      // fired `load` before this boot (race), an extra refresh
      // guarantees the correct state. It is idempotent.
      if (applied && window.htmx) {
        const panel = document.getElementById('tasks-panel');
        if (panel) window.htmx.trigger(panel, 'refresh');
      }
    })();
  }

  // ---- Row actions (kebab) portal ---------------------------------------
  // The kebab <ul> lives inside <details>, and its ancestors may clip it
  // (`contain: paint` + `overflow-x: auto` on .table-wrap). To escape the
  // clip we move the <ul> to <body> on open (portal) and reposition it
  // with position: fixed, returning it to its place on close.
  //
  // Invariants:
  //   - At most ONE open menu: the state is a single `active`, not a map.
  //   - The keyboard and click listeners are delegated and attached only once;
  //     there are no per-menu listeners that could be left orphaned.
  //   - The htmx swap handling is idempotent: htmx emits one
  //     `htmx:afterSwap` per settled element (the deck fragment and
  //     its OOB), so the second event of the same swap must do nothing.
  //   - A <ul class="row-actions-portal"> in <body> never outlives its
  //     closed <details>: it is swept on close and after every swap.
  // Without JS the native <details> still opens and closes with its <summary>.
  function initRowActions() {
    // State declared BEFORE attaching any listener (avoids the TDZ).
    let active = null;       // { det, ul, placeholder } of the portaled menu
    let pendingSwap = null;  // { det, focusIndex, target, oob } between beforeSwap and afterSwap
    let focusedKebab = null; // { summary, scope }: kebab button with focus when a swap starts

    function summaryOf(det) {
      return det.querySelector(':scope > summary');
    }

    function menuItems(ul) {
      return Array.prototype.slice.call(
        ul.querySelectorAll('a[href], button:not([disabled])'));
    }

    // Menu open right now: portaled, or in place in the middle of a swap.
    function current() {
      if (active) return { det: active.det, ul: active.ul };
      if (pendingSwap && pendingSwap.det.isConnected && pendingSwap.det.open) {
        const ul = pendingSwap.det.querySelector(':scope > ul');
        if (ul) return { det: pendingSwap.det, ul: ul };
      }
      return null;
    }

    function focusSummary(det) {
      const summary = summaryOf(det);
      if (summary && summary.isConnected) summary.focus({ preventScroll: true });
    }

    function sweepOrphans() {
      const nodes = document.querySelectorAll('body > ul.row-actions-portal');
      Array.prototype.forEach.call(nodes, (ul) => {
        if (!active || active.ul !== ul) ul.remove();
      });
    }

    function repos() { position(); }

    function openPortal(det, afterSwap) {
      if (active && active.det === det) return;
      // One-at-a-time: close the other open kebab, without moving focus.
      if (current()) closeMenu(false);

      const ul = det.querySelector(':scope > ul');
      if (!ul) return;

      const placeholder = document.createComment('row-actions-portal');
      ul.parentNode.insertBefore(placeholder, ul);
      document.body.appendChild(ul);
      ul.classList.add('row-actions-portal');
      // Re-portal after a poll: without this the entry animation repeats
      // every 3s and the menu flickers.
      if (afterSwap) ul.style.animation = 'none';

      active = { det: det, ul: ul, placeholder: placeholder };
      window.addEventListener('scroll', repos, { passive: true, capture: true });
      window.addEventListener('resize', repos);
      position();
      sweepOrphans();
    }

    // Returns the <ul> to its <details> without touching `det.open`.
    function unportal() {
      if (!active) return;
      const st = active;
      active = null;
      window.removeEventListener('scroll', repos, true);
      window.removeEventListener('resize', repos);
      st.ul.classList.remove('row-actions-portal');
      st.ul.removeAttribute('style');
      if (st.placeholder.parentNode) {
        st.placeholder.parentNode.insertBefore(st.ul, st.placeholder);
        st.placeholder.remove();
      } else if (st.ul.parentNode === document.body) {
        // The original <details> no longer exists (htmx swap).
        st.ul.remove();
      }
    }

    function closePortal() {
      unportal();
      sweepOrphans();
    }

    // Closes the open menu. `returnFocus` returns focus to the kebab button
    // (Escape, chosen action); a click outside does NOT move it, so as not to
    // steal focus from the control the operator just pressed.
    function closeMenu(returnFocus) {
      const cur = current();
      pendingSwap = null;
      closePortal();
      if (!cur) return;
      cur.det.open = false;
      if (returnFocus) focusSummary(cur.det);
    }

    function position() {
      if (!active) return;
      const det = active.det;
      const ul = active.ul;
      // Defense: if the <details> was disconnected from the DOM (htmx swap
      // between scroll ticks), getBoundingClientRect returns 0,0,0,0 and the
      // menu jumps to the top-left corner. We close the portal instead.
      if (!det.isConnected) { closePortal(); return; }
      const summary = summaryOf(det);
      if (!summary) { closeMenu(false); return; }
      const r = summary.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) { closeMenu(false); return; }

      // We measure the ul with visibility:hidden so it does not flash in the
      // top-left corner while we compute the final position.
      const prevVisibility = ul.style.visibility;
      ul.style.visibility = 'hidden';
      ul.style.position = 'fixed';
      ul.style.top = '0';
      ul.style.left = '0';
      const ulR = ul.getBoundingClientRect();
      const ulW = ulR.width;
      const ulH = ulR.height;

      const margin = 4;
      const vw = document.documentElement.clientWidth;
      const vh = document.documentElement.clientHeight;

      // By default: align the ul's right edge with the summary's right edge,
      // open downwards. Clamp to the viewport on both axes.
      let left = r.right - ulW;
      if (left < margin) left = margin;
      if (left + ulW > vw - margin) left = vw - margin - ulW;

      let top = r.bottom + margin;
      if (top + ulH > vh - margin) {
        const upTop = r.top - margin - ulH;
        if (upTop >= margin) top = upTop;
        else top = Math.max(margin, vh - margin - ulH);
      }

      ul.style.top = top + 'px';
      ul.style.left = left + 'px';
      ul.style.visibility = prevVisibility || '';
    }

    // The `toggle` event does not bubble, so we use capture to
    // catch it on document. This covers fragments that htmx re-renders
    // every 3s without having to rebind listeners.
    document.addEventListener('toggle', (ev) => {
      const det = ev.target;
      if (!(det instanceof HTMLDetailsElement)) return;
      if (!det.classList.contains('row-actions')) return;
      if (det.open) {
        openPortal(det, false);
      } else {
        const cur = current();
        if (cur && cur.det === det) closeMenu(false);
      }
    }, true);

    // Choosing an action closes the menu. Capture on document: the <ul> has
    // to return to its <details> BEFORE htmx processes the click; otherwise
    // `hx-target="closest .worker-card"` does not resolve (the <a> is
    // in <body>, not in its card). Focus returns to the kebab button before
    // the confirm dialog opens, so the <dialog> restores it there on close.
    document.addEventListener('click', (ev) => {
      const cur = current();
      if (!cur || !(ev.target instanceof Element)) return;
      const item = ev.target.closest('a, button');
      if (item && cur.ul.contains(item)) closeMenu(true);
    }, true);

    // Click outside. The click that opens the menu lands on the <summary>,
    // inside the <details>, and is ignored: the old setTimeout is not needed.
    document.addEventListener('click', (ev) => {
      const cur = current();
      if (!cur || !(ev.target instanceof Node)) return;
      if (cur.ul.contains(ev.target) || cur.det.contains(ev.target)) return;
      closeMenu(false);
    });

    // Keyboard: Escape closes; arrows/Home/End walk the items; Tab enters
    // the menu from the button and leaves it by closing it. The portaled <ul>
    // sits at the end of <body>, so without this Tab would not reach the items.
    document.addEventListener('keydown', (ev) => {
      const cur = current();
      if (!cur) return;
      if (ev.key === 'Escape') {
        ev.preventDefault();
        closeMenu(true);
        return;
      }
      const items = menuItems(cur.ul);
      const summary = summaryOf(cur.det);
      const idx = items.indexOf(document.activeElement);
      const onSummary = document.activeElement === summary;
      if ((!onSummary && idx === -1) || !items.length) return;

      const last = items.length - 1;
      let next = null;
      if (ev.key === 'ArrowDown') next = items[idx >= last ? 0 : idx + 1];
      else if (ev.key === 'ArrowUp') next = items[idx <= 0 ? last : idx - 1];
      else if (ev.key === 'Home') next = items[0];
      else if (ev.key === 'End') next = items[last];
      else if (ev.key === 'Tab') {
        if (onSummary && !ev.shiftKey) next = items[0];
        else if (idx === 0 && ev.shiftKey) next = summary;
        else if (onSummary || (idx === last && !ev.shiftKey)) {
          // Leaves the menu (Shift+Tab from the button, Tab from the last
          // item): close and let Tab follow its natural course from the
          // kebab button. Shift+Tab on the last item does NOT leave: it falls
          // to the `return` below and the native order takes it to the
          // previous item.
          closeMenu(!onSummary);
          return;
        } else return;
      } else return;
      ev.preventDefault();
      if (next) next.focus();
    });

    // Deleted card (owner's decision): when a swap takes away the
    // card whose kebab button had focus (Delete confirmed, or the poll
    // that removes it from the deck), focus ends up on document.body. It is
    // not moved to a neighboring card nor left on another deck node. The
    // browser already drops focus from a removed node; here it becomes a
    // rule instead of a coincidence, in case the swap (or a morph focus
    // restore) relocates it in the deck.
    function rememberFocusedKebab() {
      const el = document.activeElement;
      if (!(el instanceof Element) || !el.matches('details.row-actions > summary')) return;
      const card = el.closest('.worker-card, tr');
      focusedKebab = {
        summary: el,
        scope: card && card.parentNode instanceof Element ? card.parentNode : null,
      };
    }

    function releaseFocusOfRemovedKebab() {
      const fk = focusedKebab;
      if (!fk) return;
      if (fk.summary.isConnected) {
        // Still alive: we only stop tracking it if focus is already elsewhere.
        if (document.activeElement !== fk.summary) focusedKebab = null;
        return;
      }
      focusedKebab = null;
      const el = document.activeElement;
      if (!el || el === document.body || typeof el.blur !== 'function') return;
      // A control outside the deck that the operator focused in the meantime
      // keeps its focus.
      if (!el.isConnected || !fk.scope || fk.scope.contains(el)) el.blur();
    }

    // Before a swap (including morph) that touches the open <details>,
    // we return the <ul> to its place without closing the kebab. If we do not:
    //   - innerHTML swap: the <details> disappears and the portal is orphaned.
    //   - morph swap: idiomorph sees a <details> with no <ul> and with a
    //     comment where the <ul> should be; it inserts a new one, duplicating
    //     nodes.
    // A swap that does not contain the <details> (e.g. `hx-swap="none"` of
    // Pause/Resume, whose target is the <a> itself) does not touch it.
    function beforeSwap(ev) {
      rememberFocusedKebab();
      if (!active) return;
      const detail = ev.detail || {};
      if (detail.shouldSwap === false) return;
      const det = active.det;
      const target = detail.target;
      if (target instanceof Node && det.isConnected && !target.contains(det)) return;
      const focusIndex = menuItems(active.ul).indexOf(document.activeElement);
      unportal();
      pendingSwap = {
        det: det,
        focusIndex: focusIndex,
        target: target || null,
        oob: ev.type === 'htmx:oobBeforeSwap',
      };
    }

    // After the swap, if the <details> is still alive and open, we re-portal.
    // The `pendingSwap` mark is consumed here: the second `htmx:afterSwap` of
    // the same swap finds `null` and only sweeps orphans. Only the event of
    // the SAME swap that set it consumes it: the platform summary's
    // `htmx:oobAfterSwap` arrives before the deck morph happens, and
    // re-portaling there would reintroduce the duplicated <ul>.
    function afterSwap(ev) {
      let pending = pendingSwap;
      if (pending && ev.type !== 'htmx:swapError') {
        const detail = ev.detail || {};
        const sameKind = pending.oob === (ev.type === 'htmx:oobAfterSwap');
        const sameTarget = !pending.target || !detail.target
          || detail.target === pending.target;
        if (!sameKind || !sameTarget) pending = null;
      }
      if (pending) pendingSwap = null;
      if (pending && !active && pending.det.isConnected && pending.det.open) {
        openPortal(pending.det, true);
        if (active && pending.focusIndex >= 0) {
          // Moving the <ul> removes focus from the item; we give it back.
          const items = menuItems(active.ul);
          const item = items[Math.min(pending.focusIndex, items.length - 1)];
          if (item) item.focus({ preventScroll: true });
          else focusSummary(pending.det);
        }
      }
      if (active && (!active.det.isConnected || !active.det.open)) closeMenu(false);
      if (!pendingSwap) sweepOrphans();
      releaseFocusOfRemovedKebab();
    }

    document.body.addEventListener('htmx:beforeSwap', beforeSwap);
    document.body.addEventListener('htmx:oobBeforeSwap', beforeSwap);
    document.body.addEventListener('htmx:afterSwap', afterSwap);
    document.body.addEventListener('htmx:oobAfterSwap', afterSwap);
    document.body.addEventListener('htmx:swapError', afterSwap);
  }

  // ---- Row navigation ------------------------------------------------------
  // The whole row of the tasks-table navigates to the detail (the col-actions
  // was removed; see _tasks_table.html). Delegated on the panel to survive
  // the polling swaps. Guards:
  //   - clicks on interactive controls (bulk checkbox, #id link,
  //     sort headers) follow their own path;
  //   - a text selection in progress must not navigate (the operator is
  //     copying a payload/stderr, not asking for the detail);
  //   - cmd/ctrl/shift+click and middle-click open in a new tab, parity with
  //     the native behavior of an <a>.
  // The .task-id-link remains the keyboard-accessible route.
  function initRowNav(panel) {
    function rowFor(e) {
      const t = e.target;
      if (!(t instanceof Element)) return null;
      if (t.closest('a, button, input, label, select, textarea, summary, dialog')) return null;
      return t.closest('tr.task-row[data-href]');
    }
    panel.addEventListener('click', (e) => {
      const row = rowFor(e);
      if (!row) return;
      const sel = window.getSelection();
      if (sel && sel.toString()) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey) window.open(row.dataset.href, '_blank');
      else window.location.assign(row.dataset.href);
    });
    panel.addEventListener('auxclick', (e) => {
      if (e.button !== 1) return;
      const row = rowFor(e);
      if (row) window.open(row.dataset.href, '_blank');
    });
  }

  // ---- Bulk selection ----------------------------------------------------
  // The panel is re-rendered every 5s (htmx polling); the set lives outside
  // the DOM and is re-applied after every swap to survive the repaint.
  function initBulk(panel) {
    const sel = new Set();
    const bar = document.getElementById('bulk-bar');
    const countEl = document.getElementById('bulk-count');
    const retryBtn = document.getElementById('bulk-retry-btn');
    const cancelBtn = document.getElementById('bulk-cancel-btn');
    const deleteBtn = document.getElementById('bulk-delete-btn');
    const resultEl = document.getElementById('bulk-result');

    function refreshBar() {
      const n = sel.size;
      countEl.textContent = String(n);
      retryBtn.disabled = n === 0;
      cancelBtn.disabled = n === 0;
      deleteBtn.disabled = n === 0;
      bar.classList.toggle('hidden', n === 0);
      // Publishes the size for dashTasksIdle: pauses polling while
      // a batch is half assembled, even if the pointer leaves the panel. The
      // Live/Paused pill reflects it only in its periodic sampling.
      tasksBulkSelectionSize = n;
    }

    function applySelectionToDom() {
      panel.querySelectorAll('.bulk-task-cbx').forEach(cb => {
        cb.checked = sel.has(cb.value);
      });
      const all = panel.querySelector('.bulk-select-all');
      const rows = panel.querySelectorAll('.bulk-task-cbx');
      if (all) {
        all.checked = rows.length > 0
          && Array.from(rows).every(cb => sel.has(cb.value));
        all.indeterminate = sel.size > 0 && !all.checked;
      }
    }

    // Shift+click range select: we track the last checkbox touched and on
    // receiving shift+click we iterate between both indices applying the same
    // checked state. The index is computed against the current DOM order
    // (which may have changed after a poll), so we read it fresh every
    // time instead of caching it.
    let lastCheckedIdx = -1;
    panel.addEventListener('click', (e) => {
      const t = e.target;
      if (!(t instanceof HTMLInputElement) || !t.classList.contains('bulk-task-cbx')) return;
      if (!e.shiftKey || lastCheckedIdx < 0) {
        const rows = Array.from(panel.querySelectorAll('.bulk-task-cbx'));
        lastCheckedIdx = rows.indexOf(t);
        return;
      }
      const rows = Array.from(panel.querySelectorAll('.bulk-task-cbx'));
      const curIdx = rows.indexOf(t);
      if (curIdx < 0) return;
      const [a, b] = curIdx < lastCheckedIdx ? [curIdx, lastCheckedIdx] : [lastCheckedIdx, curIdx];
      // The click already toggles t by itself (post-click t.checked is the
      // target state); we replicate that state across the range.
      const desired = t.checked;
      for (let i = a; i <= b; i++) {
        const cb = rows[i];
        if (cb.checked !== desired) {
          cb.checked = desired;
          if (desired) sel.add(cb.value);
          else sel.delete(cb.value);
        } else {
          if (desired) sel.add(cb.value);
          else sel.delete(cb.value);
        }
      }
      refreshBar();
      applySelectionToDom();
      lastCheckedIdx = curIdx;
    });

    panel.addEventListener('change', (e) => {
      const t = e.target;
      if (t.classList.contains('bulk-task-cbx')) {
        if (t.checked) sel.add(t.value);
        else sel.delete(t.value);
        refreshBar();
        applySelectionToDom();
      } else if (t.classList.contains('bulk-select-all')) {
        const rows = panel.querySelectorAll('.bulk-task-cbx');
        if (t.checked) rows.forEach(cb => sel.add(cb.value));
        else rows.forEach(cb => sel.delete(cb.value));
        refreshBar();
        applySelectionToDom();
      }
    });

    panel.addEventListener('htmx:afterSwap', () => {
      applySelectionToDom();
    });

    function selectedIds() {
      return Array.from(sel, v => Number(v));
    }

    // Bounded sample of the affected IDs for the confirm body: the
    // operator sees WHAT will be touched, not just how many (a safety net
    // with no undo). Up to 5 ids; the rest is summarized as "+N more".
    function idSample(ids) {
      const head = ids.slice(0, 5).map(id => '#' + id).join(', ');
      return ids.length > 5 ? `${head} +${ids.length - 5} more` : head;
    }

    // Unified confirmation via the confirm dialog (tone, safe focus, reason).
    // Fallback to window.confirm()/prompt() only if the dialog is not mounted,
    // so the action is never left without a safety net if base.html changes.
    function askConfirm(opts) {
      if (confirmModal) return confirmModal(opts);
      let ok = window.confirm(opts.message || opts.title || 'Confirm?');
      let reason = '';
      if (ok && opts.reasonLabel) {
        // prompt() === null = the operator cancelled → abort the action, the
        // same as dismissing the dialog (do not proceed with an empty reason).
        // '' = OK with no text → proceeds with an empty reason. It preserves
        // the previous native semantics of bulkCancel, where cancelling the
        // prompt aborted.
        const r = window.prompt(opts.reasonLabel, '');
        if (r === null) ok = false;
        else reason = r;
      }
      return Promise.resolve({ ok, reason });
    }

    function showResult(verb, body) {
      const parts = [];
      const affected = body.retried || body.cancelled || body.deleted || [];
      parts.push(`${verb}: ${affected.length}`);
      if ((body.skipped || []).length) parts.push(`skipped: ${body.skipped.length}`);
      if ((body.not_found || []).length) parts.push(`not_found: ${body.not_found.length}`);
      resultEl.textContent = parts.join(' · ');
      setTimeout(() => { resultEl.textContent = ''; }, 6000);
    }

    async function bulkPost(url, payload, verb) {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const txt = await r.text();
        resultEl.textContent = `Error ${r.status}: ${txt.slice(0, 200)}`;
        return;
      }
      const body = await r.json();
      showResult(verb, body);
      sel.clear();
      refreshBar();
      htmx.trigger('#tasks-panel', 'refresh');
    }

    // Plural helper for the messages ("1 task" vs "3 tasks").
    function nTasks(n) {
      return `${n} task${n === 1 ? '' : 's'}`;
    }

    async function bulkRetry() {
      const ids = selectedIds();
      if (!ids.length) return;
      const { ok } = await askConfirm({
        title: 'Retry tasks?',
        message: `Retry ${nTasks(ids.length)} (${idSample(ids)}). `
          + `Only failed and cancelled tasks will be retried. Retried tasks go back to the `
          + `general queue and may run on any eligible worker, not necessarily `
          + `the worker that failed.`,
        okLabel: 'Retry',
      });
      if (!ok) return;
      bulkPost('/admin/tasks/bulk/retry', { task_ids: ids }, 'retried');
    }

    async function bulkCancel() {
      const ids = selectedIds();
      if (!ids.length) return;
      const { ok, reason } = await askConfirm({
        title: 'Cancel tasks?',
        message: `Cancel ${nTasks(ids.length)} (${idSample(ids)}). `
          + `Running executions will receive the cancellation on their next `
          + `heartbeat.`,
        reasonLabel: 'Reason (optional, logged to audit)',
        okLabel: 'Cancel tasks',
        okClass: 'btn-warning',
        dismissLabel: 'Keep tasks',
      });
      if (!ok) return;
      bulkPost('/admin/tasks/bulk/cancel', { task_ids: ids, reason: reason || null }, 'cancelled');
    }

    async function bulkDelete() {
      const ids = selectedIds();
      if (!ids.length) return;
      const { ok } = await askConfirm({
        title: 'Delete tasks?',
        message: `Delete ${nTasks(ids.length)} (${idSample(ids)}). `
          + `Only tasks in a terminal state will be removed. `
          + `This cannot be undone.`,
        okLabel: 'Delete',
        okClass: 'btn-danger',
      });
      if (!ok) return;
      bulkPost('/admin/tasks/bulk/delete', { task_ids: ids }, 'deleted');
    }

    if (retryBtn) retryBtn.addEventListener('click', bulkRetry);
    if (cancelBtn) cancelBtn.addEventListener('click', bulkCancel);
    if (deleteBtn) deleteBtn.addEventListener('click', bulkDelete);

    refreshBar();
  }

  // ---- Modal control -----------------------------------------------------
  // The modal is used for two flows: "New task" (button on the dashboard) and
  // "Fork task" (button in task_detail). The second marks the dialog with
  // data-fork-mode before opening it and we reflect that in the
  // title and the submit button; closeModal clears the flag so the
  // next opening returns to the "New task" state.
  function initModal(modalEl) {
    const titleEl = document.getElementById('new-task-title');
    const submitEl = document.getElementById('nt-submit');
    const defaultTitle = titleEl ? titleEl.textContent : 'New task';
    const defaultSubmit = submitEl ? submitEl.textContent : 'Create task';

    function syncForkModeUI() {
      const isFork = modalEl.dataset.forkMode === '1';
      if (titleEl) {
        const src = modalEl.dataset.forkSourceId;
        titleEl.textContent = isFork
          ? (src ? `Fork task #${src}` : 'Fork task')
          : defaultTitle;
      }
      if (submitEl) {
        submitEl.textContent = isFork ? 'Fork task' : defaultSubmit;
      }
    }

    function openModal() {
      // We clear residual errors: if the user closed with Esc/backdrop
      // after a failed submit, we do not want the next opening
      // to start already in an invalid state.
      clearErrors();
      syncForkModeUI();
      if (typeof modalEl.showModal === 'function') {
        modalEl.showModal();
      } else {
        modalEl.setAttribute('open', '');
      }
      // The value of #nt-commands may have changed without firing 'input'
      // (fork prefill, resetNewTask); we refresh gutter and count on open.
      const cmdEditor = document.querySelector('#new-task-form .cmd-editor');
      if (cmdEditor && cmdEditor.__refresh) cmdEditor.__refresh();
      // The artifacts checkbox may have changed through prefill/reset without
      // firing 'change'; we sync the fields' visibility on open.
      updateArtifactsFields();
      setTimeout(() => {
        const cmd = document.getElementById('nt-commands');
        if (cmd) cmd.focus();
      }, 30);
    }

    // ---- Fullscreen toggle ----------------------------------------------
    // Toggles the .is-fullscreen class on the dialog (the CSS does the rest:
    // dimensions, visible icon and Commands editor height). It keeps
    // aria-pressed and aria-label in sync for screen readers.
    const fsBtn = modalEl.querySelector('[data-action="toggle-fullscreen"]');
    function syncFullscreenUI() {
      if (!fsBtn) return;
      const on = modalEl.classList.contains('is-fullscreen');
      fsBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      fsBtn.setAttribute('aria-label', on ? 'Exit full screen' : 'Expand to full screen');
    }
    function toggleFullscreen() {
      modalEl.classList.toggle('is-fullscreen');
      syncFullscreenUI();
      // The numbered gutter is repositioned according to the textarea's
      // height/scroll; after changing the editor height, we refresh it.
      const cmdEditor = document.querySelector('#new-task-form .cmd-editor');
      if (cmdEditor && cmdEditor.__refresh) cmdEditor.__refresh();
    }
    if (fsBtn) fsBtn.addEventListener('click', toggleFullscreen);

    function closeModal() {
      if (typeof modalEl.close === 'function') modalEl.close();
      else modalEl.removeAttribute('open');
      delete modalEl.dataset.forkMode;
      delete modalEl.dataset.forkSourceId;
      // We go back to window mode so the next opening is predictable.
      modalEl.classList.remove('is-fullscreen');
      syncFullscreenUI();
      syncForkModeUI();
    }

    // showModal opens a ::backdrop that receives clicks like the dialog
    // itself; we compare target with the dialog to tell backdrop from content.
    modalEl.addEventListener('click', (e) => {
      if (e.target === modalEl) closeModal();
    });

    // Delegation by attribute: the header has #open-new-task-btn, but the
    // table's empty-state injects #empty-new-task-btn every time the
    // list is re-rendered via polling/morph. Binding by id would not survive
    // the swap; we delegate on document and match [data-open-new-task].
    document.addEventListener('click', (e) => {
      const trigger = e.target.closest && e.target.closest('[data-open-new-task]');
      if (!trigger) return;
      e.preventDefault();
      openModal();
    });

    modalEl.querySelectorAll('[rel="prev"], [data-action="close-modal"]').forEach(el => {
      el.addEventListener('click', closeModal);
    });

    // Exposed to the closure by reference from submitNewTask.
    modalEl.__openModal = openModal;
    modalEl.__closeModal = closeModal;
  }

  // ---- New task: validation + errors ------------------------------------
  // setErr also syncs aria-invalid on the related input/textarea
  // (same prefix, without the `-err` suffix). This paints a red border and
  // speaks to screen readers without having to duplicate markup.
  function setErr(id, msg) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg || '';
    el.classList.toggle('show', !!msg);
    const fieldId = id.replace(/-err$/, '');
    const field = document.getElementById(fieldId);
    if (field) {
      if (msg) field.setAttribute('aria-invalid', 'true');
      else field.removeAttribute('aria-invalid');
    }
  }

  function clearErrors() {
    ['nt-commands-err', 'nt-requirements-err',
     'nt-target-count-err', 'nt-priority-err',
     'nt-claim-timeout-err', 'nt-exec-timeout-err', 'nt-run-at-err',
     'nt-workers-err', 'nt-task-env-err', 'nt-egress-allowlist-err']
      .forEach(id => setErr(id, ''));
    const r = document.getElementById('nt-result');
    if (r) { r.textContent = ''; r.className = 'form-result'; }
  }

  // The worker runs `commands` (one per line, via
  // /bin/sh -c) in an ephemeral venv with `requirements`. Both arrive as
  // lists of strings inside `payload`. Splitting by line: we discard
  // empty or whitespace-only lines so a trailing newline does not break it.
  function parseLines(raw) {
    return (raw || '')
      .split('\n')
      .map(s => s.trim())
      .filter(s => s.length > 0);
  }

  // ---- Commands editor: numbered gutter + live count --------------------
  // The #nt-commands textarea has wrap=off (see .cmd-editor in app.css): each
  // \n is a physical row = one command, and a long command is no longer
  // wrapped and mistaken for several. We number those rows in the gutter and
  // follow its vertical scroll with translateY(-scrollTop). The badge shows
  // the count of REAL commands (parseLines discards empty ones), which is
  // what gets dispatched. The gutter is decoration (aria-hidden); we only
  // inject integers, with no injection risk. We expose editor.__refresh
  // because setting .value from JS (the fork prefill, resetNewTask) does not
  // fire 'input'; openModal calls it.
  function initCommandEditor() {
    const ta = document.getElementById('nt-commands');
    if (!ta) return;
    const editor = ta.closest('.cmd-editor');
    const gutter = editor && editor.querySelector('.cmd-gutter-lines');
    if (!editor || !gutter) return;
    const countEl = document.getElementById('nt-commands-count');

    let lastRows = -1;
    function render() {
      const value = ta.value;
      // Physical row per \n; minimum 1 so a new line gets its number.
      const rows = value.length ? value.split('\n').length : 1;
      if (rows !== lastRows) {
        // Built via DOM (not innerHTML): only integers, no user text,
        // but we keep the safe pattern.
        const frag = document.createDocumentFragment();
        for (let i = 1; i <= rows; i++) {
          const span = document.createElement('span');
          span.textContent = String(i);
          frag.appendChild(span);
        }
        gutter.replaceChildren(frag);
        lastRows = rows;
      }
      if (countEl) {
        const n = parseLines(value).length;
        // 0 → empty badge (CSS :empty hides it): a freshly opened form
        // does not need to announce "0 commands".
        countEl.textContent = n === 0 ? '' : (n === 1 ? '1 command' : n + ' commands');
      }
    }
    function syncScroll() {
      gutter.style.transform = 'translateY(' + (-ta.scrollTop) + 'px)';
    }

    ta.addEventListener('input', () => { render(); syncScroll(); });
    ta.addEventListener('scroll', syncScroll, { passive: true });
    // Called by openModal: the value may have changed through prefill/reset
    // without 'input'.
    editor.__refresh = () => { lastRows = -1; render(); syncScroll(); };

    render();
    syncScroll();
  }

  function updateTargetMode() {
    const checked = document.querySelector('input[name="target_mode"]:checked');
    if (!checked) return;
    const mode = checked.value;
    const countRow = document.getElementById('target-count-row');
    const workersRow = document.getElementById('target-workers-row');
    // "target count" only applies to count mode. In worker(s) mode the count
    // is derived by the server from `workers:<ids>` (= |pool|); showing it
    // is confusing.
    if (countRow) countRow.classList.toggle('hidden', mode !== 'count');
    const showWorkers = mode === 'worker' || mode === 'all-subset';
    if (workersRow) workersRow.classList.toggle('hidden', !showWorkers);
    if (showWorkers) loadWorkersPicker();
    // run_at only applies to `count`: in the other modes the server ignores
    // it, so we hide the row so it is not a silent no-op.
    const runAtRow = document.getElementById('nt-run-at-row');
    if (runAtRow) runAtRow.classList.toggle('hidden', mode !== 'count');
    // One-line reminder of the active mode (see _new_task_modal.html): the
    // operator sees what they chose without opening a tooltip. The detail per
    // mode remains in the tooltips; here only the plain summary of the
    // SELECTED mode.
    const desc = document.getElementById('target-mode-desc');
    if (desc) {
      const TARGET_MODE_DESCS = {
        'count': 'Dispatch N copies; the first N online workers to claim them win.',
        'all-available': 'One copy per worker online at submit time. A snapshot fan-out.',
        'worker': 'Run only on the specific workers you select by id.',
        'all-subset': 'One copy per worker in the subset you select.',
      };
      desc.textContent = TARGET_MODE_DESCS[mode] || '';
    }
  }

  // ---- Worker picker (New task modal) -----------------------------------
  // State in a closure: the selected set survives re-renders of the
  // listing. If a worker disappears or goes paused/offline between loads,
  // it is automatically removed from the set (renderWorkersPicker filters).
  //
  // Compact↔list threshold: with fleets ≤ 5 online the toolbar (select-all /
  // clear / counter) is noise for 1-5 rows; we render inline chips. With
  // > 5, a vertical list + search + toolbar. A single point of change.
  const WORKERS_PICKER_COMPACT_MAX = 5;
  const workerPickerSel = new Set();
  let workersLoadInflight = null;

  function setWorkersErr(msg) { setErr('nt-workers-err', msg || ''); }

  function refreshWorkersPickerCount() {
    const el = document.getElementById('nt-workers-count');
    if (!el) return;
    const n = workerPickerSel.size;
    el.textContent = `${n} selected`;
  }

  // Vertical row (list mode, N > 5).
  function makeWorkerRow(w) {
    const disabled = w.status !== 'online';
    const checked = workerPickerSel.has(w.id);
    if (disabled && checked) workerPickerSel.delete(w.id);

    const label = document.createElement('label');
    label.className = `worker-pick worker-pick-${w.status}` + (disabled ? ' is-disabled' : '');
    label.dataset.hostname = (w.hostname || '').toLowerCase();
    label.dataset.workerId = String(w.id).toLowerCase();

    const cbx = document.createElement('input');
    cbx.type = 'checkbox';
    cbx.className = 'worker-pick-cbx';
    cbx.value = w.id;
    if (disabled) cbx.disabled = true;
    if (checked && !disabled) cbx.checked = true;

    const host = document.createElement('span');
    host.className = 'worker-pick-host';
    host.textContent = w.name ? `${w.hostname} · ${w.name}` : (w.hostname || '');

    const badge = document.createElement('span');
    badge.className = `badge badge-${w.status}`;
    badge.textContent = w.status;

    const idSpan = document.createElement('span');
    idSpan.className = 'muted worker-pick-id';
    idSpan.textContent = String(w.id).slice(0, 8);

    label.append(cbx, host, badge, idSpan);
    return label;
  }

  // Inline chip (compact mode, N ≤ 5). A toggle button with aria-pressed so
  // SR announces the state; the real checkbox stays hidden behind the label
  // to preserve the current contract (`.worker-pick-cbx` is read by the
  // submit).
  function makeWorkerChip(w) {
    const disabled = w.status !== 'online';
    const checked = workerPickerSel.has(w.id);
    if (disabled && checked) workerPickerSel.delete(w.id);

    const label = document.createElement('label');
    label.className = `worker-chip worker-chip-${w.status}` + (disabled ? ' is-disabled' : '');
    label.dataset.workerId = String(w.id).toLowerCase();

    const cbx = document.createElement('input');
    cbx.type = 'checkbox';
    cbx.className = 'worker-pick-cbx worker-chip-cbx';
    cbx.value = w.id;
    if (disabled) cbx.disabled = true;
    if (checked && !disabled) cbx.checked = true;

    const dot = document.createElement('span');
    dot.className = `worker-chip-dot worker-chip-dot--${w.status}`;
    dot.setAttribute('aria-hidden', 'true');

    const host = document.createElement('span');
    host.className = 'worker-chip-host';
    host.textContent = w.hostname || String(w.id).slice(0, 8);

    label.append(cbx, dot, host);
    if (disabled) {
      const status = document.createElement('span');
      status.className = 'worker-chip-status';
      status.textContent = w.status;
      label.appendChild(status);
    }
    return label;
  }

  function renderWorkersPicker(workers) {
    const box = document.getElementById('nt-workers-picker');
    if (!box) return;
    box.textContent = '';
    box.classList.remove('workers-picker--compact', 'workers-picker--list');

    // Purges ghost ids: a fork may preselect (applyTargetToForm)
    // workers of its original pool that have since been DELETED from the
    // server. Those ids do not come in workers.json, so no makeWorkerRow/Chip
    // unchecks them — they inflate the "N selected" counter and would slip
    // into the submit (which reads workerPickerSel, not the checkboxes). If
    // the id does not exist in the response, it is out of the set.
    const knownIds = new Set((workers || []).map(w => String(w.id)));
    for (const id of [...workerPickerSel]) {
      if (!knownIds.has(id)) workerPickerSel.delete(id);
    }

    const toolbar = document.getElementById('nt-workers-toolbar');
    const searchWrap = document.getElementById('nt-workers-search-wrap');
    const searchInput = document.getElementById('nt-workers-search');

    if (!workers || workers.length === 0) {
      if (toolbar) toolbar.classList.add('hidden');
      if (searchWrap) searchWrap.classList.add('hidden');
      const p = document.createElement('p');
      p.className = 'muted';
      p.textContent = 'No workers registered.';
      box.appendChild(p);
      refreshWorkersPickerCount();
      return;
    }

    // Online first (alphabetical by hostname), then paused, then offline.
    const rank = { online: 0, paused: 1, offline: 2 };
    const sorted = [...workers].sort((a, b) => {
      const r = (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
      if (r !== 0) return r;
      return (a.hostname || '').localeCompare(b.hostname || '');
    });

    const onlineCount = sorted.filter(w => w.status === 'online').length;
    const useCompact = onlineCount <= WORKERS_PICKER_COMPACT_MAX
                       && sorted.length <= WORKERS_PICKER_COMPACT_MAX;

    const frag = document.createDocumentFragment();
    if (useCompact) {
      box.classList.add('workers-picker--compact');
      for (const w of sorted) frag.appendChild(makeWorkerChip(w));
      if (toolbar) toolbar.classList.add('hidden');
      if (searchWrap) searchWrap.classList.add('hidden');
      if (searchInput) searchInput.value = '';
    } else {
      box.classList.add('workers-picker--list');
      for (const w of sorted) frag.appendChild(makeWorkerRow(w));
      if (toolbar) toolbar.classList.remove('hidden');
      if (searchWrap) searchWrap.classList.remove('hidden');
      if (searchInput) {
        searchInput.value = '';
        filterWorkersPicker('');
      }
    }
    box.appendChild(frag);
    refreshWorkersPickerCount();
  }

  // Client-side filter by hostname or id prefix. It only applies to list
  // mode — in compact the search-wrap is hidden.
  function filterWorkersPicker(query) {
    const box = document.getElementById('nt-workers-picker');
    if (!box) return;
    const q = (query || '').trim().toLowerCase();
    box.querySelectorAll('.worker-pick').forEach(row => {
      const host = row.dataset.hostname || '';
      const id = row.dataset.workerId || '';
      const visible = !q || host.includes(q) || id.includes(q);
      row.classList.toggle('is-filtered-out', !visible);
    });
  }

  async function loadWorkersPicker() {
    // De-dup of concurrent fetches (opening/closing the modal or jumping
    // between modes in a chain may fire several calls).
    if (workersLoadInflight) return workersLoadInflight;
    workersLoadInflight = (async () => {
      try {
        const r = await fetch('/admin/workers.json', { headers: { 'Accept': 'application/json' } });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        renderWorkersPicker(data);
      } catch (e) {
        const box = document.getElementById('nt-workers-picker');
        if (box) {
          box.textContent = '';
          const p = document.createElement('p');
          p.className = 'err show';
          p.textContent = `Failed to load workers: ${e.message}`;
          box.appendChild(p);
        }
      } finally {
        workersLoadInflight = null;
      }
    })();
    return workersLoadInflight;
  }

  function initWorkersPicker() {
    const box = document.getElementById('nt-workers-picker');
    if (!box) return;
    box.addEventListener('change', (e) => {
      const t = e.target;
      if (!t.classList.contains('worker-pick-cbx')) return;
      if (t.checked) workerPickerSel.add(t.value);
      else workerPickerSel.delete(t.value);
      refreshWorkersPickerCount();
      setWorkersErr('');
    });
    const allBtn = document.getElementById('nt-workers-select-all');
    const clearBtn = document.getElementById('nt-workers-clear');
    if (allBtn) allBtn.addEventListener('click', () => {
      box.querySelectorAll('.worker-pick-cbx:not(:disabled)').forEach(cb => {
        cb.checked = true;
        workerPickerSel.add(cb.value);
      });
      refreshWorkersPickerCount();
      setWorkersErr('');
    });
    if (clearBtn) clearBtn.addEventListener('click', () => {
      workerPickerSel.clear();
      box.querySelectorAll('.worker-pick-cbx').forEach(cb => { cb.checked = false; });
      refreshWorkersPickerCount();
    });
    const searchInput = document.getElementById('nt-workers-search');
    if (searchInput) searchInput.addEventListener('input', (e) => {
      filterWorkersPicker(e.target.value);
    });
  }

  function resetWorkersPicker() {
    workerPickerSel.clear();
    const box = document.getElementById('nt-workers-picker');
    if (box) box.querySelectorAll('.worker-pick-cbx').forEach(cb => { cb.checked = false; });
    const searchInput = document.getElementById('nt-workers-search');
    if (searchInput) {
      searchInput.value = '';
      filterWorkersPicker('');
    }
    refreshWorkersPickerCount();
    setWorkersErr('');
  }

  function resetNewTask() {
    const form = document.getElementById('new-task-form');
    if (form) form.reset();
    clearErrors();
    resetWorkersPicker();
    updateTargetMode();
    updateArtifactsFields();
    updateIsolationFields();
  }

  function initNewTaskForm(modalEl) {
    const form = document.getElementById('new-task-form');
    if (!form) return;

    document.querySelectorAll('input[name="target_mode"]').forEach(r => {
      r.addEventListener('change', updateTargetMode);
    });

    initWorkersPicker();
    initInfoTips(modalEl);
    form.addEventListener('submit', (ev) => { submitNewTask(ev, modalEl); });

    const artToggle = document.getElementById('nt-artifacts-enabled');
    if (artToggle) artToggle.addEventListener('change', updateArtifactsFields);

    const isoToggle = document.getElementById('nt-isolation-enabled');
    if (isoToggle) isoToggle.addEventListener('change', updateIsolationFields);

    updateTargetMode();
    updateArtifactsFields();
    updateIsolationFields();
  }

  // Accessible info tooltips (WCAG 2.2 SC 1.4.13). The hover/focus reveal and
  // the "hoverable/persistent" are handled by the CSS; here we cover the
  // "dismissible": Esc hides the visible bubble without moving focus. In a
  // native <dialog> Esc closes the modal, so we intercept in capture and
  // pre-empt it when a tooltip is in view. .dismissed is cleared on leaving
  // (blur/mouseleave) so the tip can reappear on the next interaction.
  // Floating positioner for the MODAL's info-tips. The dialog's <form> is
  // scrollable (overflow-y:auto) and clipped the absolute bubble; inside the
  // dialog we make it position:fixed and place it in the viewport relative
  // to the trigger. Outside the modal (task_detail) the absolute CSS is
  // enough and is proof against the poll's OOB swaps. The reveal (opacity) is
  // handled by the CSS via :hover / :focus-within; here we only compute
  // top/left.
  function initInfoTipsFloating() {
    document.documentElement.classList.add('info-tip-js');

    function place(tip) {
      const btn = tip.querySelector('.info-tip-btn');
      const bubble = tip.querySelector('.info-tip-bubble');
      if (!btn || !bubble) return;
      const r = btn.getBoundingClientRect();
      const bw = bubble.offsetWidth;   // visibility:hidden keeps the layout
      const bh = bubble.offsetHeight;
      const vw = document.documentElement.clientWidth;
      const vh = document.documentElement.clientHeight;
      const gap = 6, pad = 8;
      let left = r.left + r.width / 2 - bw / 2;
      left = Math.max(pad, Math.min(left, vw - bw - pad));
      let top = r.bottom + gap;              // prefer below (does not collide with headers)
      let above = false;
      if (top + bh > vh - pad) {             // does not fit below → above
        top = Math.max(pad, r.top - gap - bh);
        above = true;
      }
      bubble.style.left = Math.round(left) + 'px';
      bubble.style.top = Math.round(top) + 'px';
      tip.classList.toggle('info-tip--above', above);
    }

    // Only tips inside a <dialog> (the modal). We do not keep the "active"
    // node: we query it live via :hover / :focus-within to
    // reposition on modal scroll/resize.
    const dialogTip = (el) => {
      const t = (el && el.closest) ? el.closest('.info-tip') : null;
      return (t && t.closest('dialog')) ? t : null;
    };
    const onEnter = (e) => { const t = dialogTip(e.target); if (t) place(t); };
    document.addEventListener('mouseover', onEnter);
    document.addEventListener('focusin', onEnter);

    const repositionActive = () => {
      const t = document.querySelector('dialog .info-tip:hover, dialog .info-tip:focus-within');
      if (t) place(t);
    };
    // capture: the scroll is fired by the modal's inner <form>, not the window.
    window.addEventListener('scroll', repositionActive, true);
    window.addEventListener('resize', repositionActive);
  }

  function initInfoTips(modalEl) {
    if (!modalEl) return;
    modalEl.querySelectorAll('.info-tip').forEach(tip => {
      const clear = () => tip.classList.remove('dismissed');
      tip.addEventListener('mouseleave', clear);
      tip.addEventListener('focusout', clear);
    });
    modalEl.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const visible = modalEl.querySelector(
        '.info-tip:hover:not(.dismissed), .info-tip:focus-within:not(.dismissed)');
      if (visible) {
        visible.classList.add('dismissed');
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
  }

  async function submitNewTask(ev, modalEl) {
    ev.preventDefault();
    clearErrors();

    // Single-pass validation: we accumulate ALL the errors and only
    // afterwards paint them, open Advanced if needed, move focus to the
    // first invalid field (DOM order) and abort. We used to stop at the
    // first error, forcing the operator to resubmit N times for N problems.
    const errors = [];   // { errId, msg, focusId }
    const fail = (errId, msg, focusId) => errors.push({ errId, msg, focusId });

    // Payload contract: payload = { requirements: [str], commands: [str] }.
    // `commands` must be non-empty; `requirements` may be empty.
    const commands = parseLines(document.getElementById('nt-commands').value);
    if (commands.length === 0) {
      fail('nt-commands-err', 'At least one command is required', 'nt-commands');
    }
    const requirements = parseLines(document.getElementById('nt-requirements').value);

    let timeoutSeconds;
    const eto = document.getElementById('nt-exec-timeout').value.trim();
    if (eto !== '') {
      const t = parseInt(eto, 10);
      if (!Number.isInteger(t) || t < 1) fail('nt-exec-timeout-err', 'Integer >= 1', 'nt-exec-timeout');
      else timeoutSeconds = t;
    }

    const mode = document.querySelector('input[name="target_mode"]:checked').value;
    let target;
    if (mode === 'all-available') {
      target = 'all-available';
    } else if (mode === 'count') {
      const n = parseInt(document.getElementById('nt-target-count').value, 10);
      if (!Number.isInteger(n) || n < 1) fail('nt-target-count-err', 'Integer >= 1', 'nt-target-count');
      else target = String(n);
    } else if (mode === 'worker' || mode === 'all-subset') {
      const ids = Array.from(workerPickerSel);
      if (ids.length === 0) {
        fail('nt-workers-err', 'Select at least one online worker', 'nt-workers-picker');
      } else if (mode === 'all-subset') {
        target = 'all:' + ids.join(',');
      } else if (ids.length === 1) {
        // count=1 + 1 worker → PUSH-able syntax worker:<id>.
        target = 'worker:' + ids[0];
      } else {
        // The server derives target_count = |pool| from `workers:<ids>`. There
        // is no syntax for count < |pool|; if later we want to
        // express "race within the pool until covering K runs with K < N",
        // the server wire must be extended (see
        // server/orcaster_server/schemas/tasks.py).
        target = 'workers:' + ids.join(',');
      }
    } else {
      fail('nt-target-count-err', `Unknown mode: ${mode}`, 'nt-target-count');
    }

    let priority;
    const prio = document.getElementById('nt-priority').value.trim();
    if (prio !== '') {
      const p = parseInt(prio, 10);
      if (!Number.isInteger(p) || p < -32768 || p > 32767) fail('nt-priority-err', 'Integer in [-32768, 32767]', 'nt-priority');
      else priority = p;
    }

    let claimTimeout;
    const cto = document.getElementById('nt-claim-timeout').value.trim();
    if (cto !== '') {
      const c = parseInt(cto, 10);
      if (!Number.isInteger(c) || c < 1) fail('nt-claim-timeout-err', 'Integer >= 1', 'nt-claim-timeout');
      else claimTimeout = c;
    }

    // run_at only applies to `count` (the server ignores the rest). Outside
    // that mode we drop it silently instead of sending an inert value; the
    // row is hidden anyway (updateTargetMode).
    let runAtIso;
    if (mode === 'count') {
      const runAt = document.getElementById('nt-run-at').value;
      if (runAt) {
        const d = new Date(runAt);
        if (isNaN(d.getTime())) fail('nt-run-at-err', 'Invalid date', 'nt-run-at');
        else runAtIso = d.toISOString();
      }
    }

    // Per-task isolation — client-side parsing + validation. Appending to
    // the payload happens further below (after building it); here we only
    // accumulate errors because the errors block cuts off before assembling
    // the payload. The server rejects ORCH_ names with 422; we warn before
    // sending.
    let isolationEnabled = false;
    let taskEnv = [];
    let egressAllowlist = [];
    const isoEnabledEl = document.getElementById('nt-isolation-enabled');
    if (isoEnabledEl && isoEnabledEl.checked) {
      isolationEnabled = true;
      taskEnv = parseLines(document.getElementById('nt-task-env').value);
      egressAllowlist = parseLines(document.getElementById('nt-egress-allowlist').value);
      const badEnv = taskEnv.filter(n => n.startsWith('ORCH_'));
      if (badEnv.length > 0) {
        fail('nt-task-env-err',
          `ORCH_ names are reserved and rejected by the server: ${badEnv.join(', ')}`,
          'nt-task-env');
      }
    }

    if (errors.length > 0) {
      errors.forEach(e => setErr(e.errId, e.msg));
      const fields = errors
        .map(e => document.getElementById(e.focusId))
        .filter(Boolean)
        .sort((x, y) =>
          (x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
      const firstField = fields[0];
      if (firstField) {
        // If the field lives inside a closed <details> (Advanced), open it
        // so focus does not land on an invisible control.
        const wrap = firstField.closest('details');
        if (wrap && !wrap.open) wrap.open = true;
        try { firstField.focus(); } catch (_) { /* noop */ }
      }
      return false;
    }

    const payload = { requirements, commands };
    if (timeoutSeconds !== undefined) payload.timeout_seconds = timeoutSeconds;

    // Artifacts block: the worker ONLY collects/uploads if the payload
    // carries `artifacts`. Without the toggle we do not add the block (no-op,
    // as today). With the toggle but an empty include, we send artifacts:{} →
    // the worker collects everything (`**/*` by default, see
    // ArtifactsSpec.from_payload).
    const artEnabledEl = document.getElementById('nt-artifacts-enabled');
    if (artEnabledEl && artEnabledEl.checked) {
      const incEl = document.getElementById('nt-artifacts-include');
      const excEl = document.getElementById('nt-artifacts-exclude');
      const reqEl = document.getElementById('nt-artifacts-required');
      const include = parseLines(incEl ? incEl.value : '');
      const exclude = parseLines(excEl ? excEl.value : '');
      const artifacts = {};
      if (include.length > 0) artifacts.include = include;
      if (exclude.length > 0) artifacts.exclude = exclude;
      if (reqEl && reqEl.checked) artifacts.required = true;
      payload.artifacts = artifacts;
    }

    // Isolation: with the toggle active we ALWAYS send both keys,
    // even empty (strict mode: clean env / egress denied). Omitting them —toggle
    // off— leaves the task in legacy mode with no isolation. Sending [] is NOT
    // omitting.
    if (isolationEnabled) {
      payload.task_env = taskEnv;
      payload.egress_allowlist = egressAllowlist;
    }

    const body = { payload, target };
    if (priority !== undefined) body.priority = priority;
    if (claimTimeout !== undefined) body.claim_timeout_seconds = claimTimeout;
    if (runAtIso !== undefined) body.run_at = runAtIso;

    const submitBtn = document.getElementById('nt-submit');
    const resultEl = document.getElementById('nt-result');
    submitBtn.disabled = true;
    submitBtn.setAttribute('aria-busy', 'true');
    resultEl.className = 'form-result muted';
    resultEl.textContent = 'Submitting…';

    try {
      const isForkSubmit = modalEl && modalEl.dataset.forkMode === '1';
      const submitUrl = isForkSubmit
        ? `/admin/tasks/${modalEl.dataset.forkSourceId}/fork`
        : '/admin/tasks';
      const r = await fetch(submitUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const txt = await r.text();
        let detail = txt;
        try {
          const j = JSON.parse(txt);
          if (j && j.detail) {
            detail = Array.isArray(j.detail)
              ? j.detail.map(d => `${(d.loc || []).join('.')}: ${d.msg}`).join(' · ')
              : j.detail;
          }
        } catch (_) { /* no JSON */ }
        resultEl.className = 'form-result err';
        resultEl.textContent = `Error ${r.status}: ${String(detail).slice(0, 300)}`;
        return false;
      }
      const out = await r.json();
      const isFork = modalEl && modalEl.dataset.forkMode === '1';
      resultEl.className = 'form-result ok';
      resultEl.textContent = isFork
        ? `Forked as #${out.task_id} (${out.status}, ${out.target_mode}, target_count=${out.target_count}). Redirecting…`
        : `Created #${out.task_id} (${out.status}, ${out.target_mode}, target_count=${out.target_count}).`;
      if (isFork) {
        // We are in task_detail (there is no tasks-panel to refresh). The fork
        // is a new task with parent_task_id; we take the user to its detail
        // so they see its progress right away.
        setTimeout(() => {
          window.location = `/admin/tasks/${out.task_id}`;
        }, 600);
        return false;
      }
      // Mark so initHighlight highlights the row after the next swap.
      window.__lastCreatedTaskId = String(out.task_id);
      resetNewTask();
      htmx.trigger('#tasks-panel', 'refresh');
      setTimeout(() => {
        if (modalEl && typeof modalEl.__closeModal === 'function') modalEl.__closeModal();
      }, 700);
    } catch (e) {
      resultEl.className = 'form-result err';
      resultEl.textContent = 'Network error: ' + e.message;
    } finally {
      submitBtn.disabled = false;
      submitBtn.removeAttribute('aria-busy');
    }
    return false;
  }

  // ---- Highlight of a newly created row ---------------------------------
  // The panel is re-rendered every 5s and loses the class between swaps;
  // we reapply over 2 consecutive swaps to hold visibility for ~6s
  // (CSS animation + one polling cycle). The first swap does
  // scrollIntoView; the following ones only refresh the class.
  function initHighlight(panel) {
    let swapsLeft = 0;
    let scrolled = false;
    panel.addEventListener('htmx:afterSwap', () => {
      const id = window.__lastCreatedTaskId;
      if (!id) return;
      const cbx = document.querySelector('.bulk-task-cbx[value="' + id + '"]');
      if (!cbx) return;
      const row = cbx.closest('tr');
      if (!row) return;
      row.classList.add('task-highlight');
      if (!scrolled) {
        row.scrollIntoView({ block: 'center', behavior: 'smooth' });
        scrolled = true;
        swapsLeft = 2;
      } else {
        swapsLeft--;
      }
      if (swapsLeft <= 0) {
        window.__lastCreatedTaskId = null;
        scrolled = false;
      }
    });
  }

  // ---- Unified timestamp rendering (compact/datetime/relative) -----------
  // Rewrites <time class="js-ts"> with data-ts-format to local time per format.
  // The server emits ISO UTC in datetime=, a visible fallback, a title with readable UTC.

  // Adaptive compact (RUNTIME column): today -> "HH:MM"; another day -> "Mmm DD HH:MM".
  function formatCompactLocal(d) {
    const now = new Date();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate();
    if (sameDay) return `${hh}:${mm}`;
    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const dd = String(d.getDate()).padStart(2, '0');
    return `${MONTHS[d.getMonth()]} ${dd} ${hh}:${mm}`;
  }

  // Full absolute in local time: "YYYY-MM-DD HH:MM:SS" (mirrors the server's dt_human).
  function formatDateTimeLocal(d) {
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
           `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  // Timezone-agnostic relative: mirrors the server's _relative_time (admin_ui.py).
  function formatRelativeLocal(d) {
    const seconds = Math.floor((Date.now() - d.getTime()) / 1000);
    if (seconds < 5) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  function applyTimestamp(el) {
    const iso = el.getAttribute('datetime');
    if (!iso) return;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return;
    const fmt = el.getAttribute('data-ts-format') || 'datetime';
    let out;
    if (fmt === 'compact') out = formatCompactLocal(d);
    else if (fmt === 'relative') out = formatRelativeLocal(d);
    else out = formatDateTimeLocal(d);
    if (out) el.textContent = out;
  }

  function initTimestamps(root) {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll('time.js-ts').forEach(applyTimestamp);
  }

  // Refreshes only the relative ones so "just now" -> "1m ago" ages without reloading.
  function refreshRelativeTimestamps() {
    document.querySelectorAll('time.js-ts[data-ts-format="relative"]').forEach(applyTimestamp);
  }

  // ---- Timezone context note (_tz_note.html) -----------------------------
  // Visible timestamps are rewritten to local time (see applyTimestamp);
  // the server does not know the browser's zone, so the per-context legend
  // is resolved here. Server-side fallback = "Times shown in UTC." (without JS
  // the timestamps stay in UTC); with JS it switches to local time. The UTC
  // detail remains available in each timestamp's title attribute (see ts() in
  // _macros.html), but the note no longer promises a hover that not everyone
  // can do.
  function localZoneLabel() {
    try {
      const parts = new Intl.DateTimeFormat('en', { timeZoneName: 'short' })
        .formatToParts(new Date());
      const z = parts.find((p) => p.type === 'timeZoneName');
      return z ? z.value : '';
    } catch (_) {
      return '';
    }
  }

  function initTzNotes(root) {
    const scope = root && root.querySelectorAll ? root : document;
    const notes = scope.querySelectorAll('.js-tz-note');
    if (!notes.length) return;
    const zone = localZoneLabel();
    const text = zone
      ? `Times shown in local time (${zone}).`
      : 'Times shown in local time.';
    notes.forEach((el) => { el.textContent = text; });
  }

})();
