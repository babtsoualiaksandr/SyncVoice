(() => {
  'use strict';

  const container = document.getElementById('panes');
  if (!container) return;

  const STORE_KEY = 'syncvoice.panes';
  const MIN_WIDTH = 260;
  const COLLAPSED_WIDTH = 34;
  const DEFAULT_WIDTHS = { main: 520, survey: 560, review: 380 };
  const TABS_BELOW = 1200; // survey + review share a column as tabs
  const STACK_BELOW = 1000; // everything stacked (CSS)

  const panes = [...container.querySelectorAll(':scope > .pane')];
  const byName = Object.fromEntries(panes.map((p) => [p.dataset.pane, p]));

  function load() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY)) || {}; } catch (e) { return {}; }
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* storage unavailable */ }
  }
  const state = Object.assign({ widths: {}, collapsed: {}, tab: 'survey' }, load());

  // ---------- widths & collapsing ----------

  function isFlex(pane) { return pane.classList.contains('pane-flex'); }

  function apply() {
    for (const pane of panes) {
      const name = pane.dataset.pane;
      const collapsed = !!state.collapsed[name];
      pane.classList.toggle('collapsed', collapsed);
      if (collapsed) {
        pane.style.flex = `0 0 ${COLLAPSED_WIDTH}px`;
      } else if (isFlex(pane)) {
        pane.style.flex = '1 1 0';
      } else {
        pane.style.flex = `0 0 ${state.widths[name] || DEFAULT_WIDTHS[name]}px`;
      }
    }
    applyTabs();
  }

  for (const pane of panes) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'pane-toggle';
    toggle.title = 'Свернуть / развернуть';
    toggle.textContent = '–';
    toggle.addEventListener('click', () => {
      state.collapsed[pane.dataset.pane] = !state.collapsed[pane.dataset.pane];
      save();
      apply();
    });
    const label = document.createElement('button');
    label.type = 'button';
    label.className = 'pane-label';
    label.textContent = pane.dataset.title;
    label.addEventListener('click', () => toggle.click());
    pane.prepend(toggle, label);
  }

  for (const splitter of container.querySelectorAll(':scope > .splitter')) {
    const left = splitter.previousElementSibling;
    const right = splitter.nextElementSibling;
    // Resize the pane with a fixed width; the flexible one takes the rest.
    const target = isFlex(left) ? right : left;
    const sign = target === left ? 1 : -1;

    const resize = (dx) => {
      const name = target.dataset.pane;
      const start = state.widths[name] || DEFAULT_WIDTHS[name];
      const max = container.clientWidth - MIN_WIDTH * 2;
      state.widths[name] = Math.round(Math.min(Math.max(start + sign * dx, MIN_WIDTH), max));
      state.collapsed[name] = false;
      apply();
    };

    splitter.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      splitter.setPointerCapture(e.pointerId);
      container.classList.add('resizing'); // stops the iframe from swallowing pointer events
      let lastX = e.clientX;
      const move = (ev) => { resize(ev.clientX - lastX); lastX = ev.clientX; };
      const up = () => {
        splitter.removeEventListener('pointermove', move);
        container.classList.remove('resizing');
        save();
      };
      splitter.addEventListener('pointermove', move);
      splitter.addEventListener('pointerup', up, { once: true });
      splitter.addEventListener('pointercancel', up, { once: true });
    });
    splitter.addEventListener('keydown', (e) => {
      const dx = { ArrowLeft: -20, ArrowRight: 20 }[e.key];
      if (dx) { e.preventDefault(); resize(dx); save(); }
    });
  }

  // ---------- survey + review as tabs on medium screens ----------

  // The same tab bar sits at the top of both panes; only the active pane shows.
  const tabBars = [];
  if (byName.survey) {
    for (const pane of [byName.survey, byName.review]) {
      const bar = document.createElement('div');
      bar.className = 'pane-tabs';
      for (const [name, title] of [['survey', 'Анкета оператора'], ['review', 'Контроль']]) {
        const b = document.createElement('button');
        b.type = 'button';
        b.dataset.tab = name;
        b.textContent = title;
        b.addEventListener('click', () => { state.tab = name; save(); applyTabs(); });
        bar.append(b);
      }
      pane.querySelector('.pane-label').after(bar);
      tabBars.push(bar);
    }
  }

  function applyTabs() {
    const survey = byName.survey;
    const tabs = !!survey && window.innerWidth < TABS_BELOW && window.innerWidth >= STACK_BELOW;
    container.classList.toggle('tabs-mode', tabs);
    if (!survey) return;
    survey.classList.toggle('tab-hidden', tabs && state.tab !== 'survey');
    byName.review.classList.toggle('tab-hidden', tabs && state.tab !== 'review');
    for (const bar of tabBars) {
      for (const b of bar.children) b.classList.toggle('active', b.dataset.tab === state.tab);
    }
  }
  window.addEventListener('resize', applyTabs);
  // Also when the width changes without a resize event (zoom, devtools emulation).
  for (const width of [TABS_BELOW, STACK_BELOW]) {
    window.matchMedia(`(max-width: ${width - 1}px)`).addEventListener('change', applyTabs);
  }

  // Panes fill the window below the header, whatever its height.
  function fitHeight() {
    if (window.innerWidth < STACK_BELOW) {
      container.style.height = '';
      return;
    }
    const top = container.getBoundingClientRect().top + window.scrollY;
    container.style.height = `${Math.max(480, window.innerHeight - top - 12)}px`;
  }
  window.addEventListener('resize', fitHeight);

  // Dropdown menus (⋯, ?) close on a click elsewhere.
  document.addEventListener('click', (e) => {
    for (const menu of document.querySelectorAll('details.more-menu[open]')) {
      if (!menu.contains(e.target)) menu.open = false;
    }
  });

  apply();
  fitHeight();

  // ---------- operator's survey (CRM) ----------

  const surveyBox = document.getElementById('survey');
  if (!surveyBox) return;
  let url = surveyBox.dataset.url; // shown in the panel
  let windowUrl = url; // opened by «Окно рядом» (the CRM itself, under the controller's own login)
  const frame = document.getElementById('survey-frame');
  const blocked = document.getElementById('survey-blocked');
  const blockedReason = document.getElementById('survey-blocked-reason');
  const preferWindow = document.getElementById('survey-prefer-window');
  const focusBack = document.getElementById('focus-back');
  const PREFER_KEY = 'syncvoice.survey.window';

  function prefer() {
    try { return localStorage.getItem(PREFER_KEY) === '1'; } catch (e) { return false; }
  }

  function openWindow(target = windowUrl) {
    const half = Math.round(screen.availWidth / 2);
    // The same window name: an open survey window is reused, not duplicated.
    const win = window.open(target, 'syncvoice-survey',
      `left=${half},top=0,width=${half},height=${screen.availHeight}`);
    if (!win) {
      showBlocked('Браузер заблокировал всплывающее окно — разрешите всплывающие окна для этого адреса.');
    }
    return win;
  }

  // In window mode the survey window follows the controller to the next call.
  // Must run inside the click/key gesture, or the browser blocks the window.
  window.SyncVoiceSurvey = {
    follow(nextUrl) {
      if (prefer() && nextUrl) openWindow(nextUrl);
    },
  };
  for (const link of document.querySelectorAll('#prev-call, #next-call')) {
    link.addEventListener('click', () => window.SyncVoiceSurvey.follow(link.dataset.surveyUrl));
  }

  function showBlocked(reason) {
    frame.hidden = true;
    frame.removeAttribute('src');
    blockedReason.textContent = reason;
    blocked.hidden = false;
  }

  function showFrame() {
    blocked.hidden = true;
    frame.hidden = false;
    frame.src = url;
  }

  // ---- CRM mode: SyncVoice finds the survey of this call and shows it via its gateway ----
  const choiceRow = document.getElementById('survey-choice-row');
  const choice = document.getElementById('survey-choice');
  const choiceNote = document.getElementById('survey-choice-note');
  let surveys = [];

  function selectSurvey(id) {
    const s = surveys.find((x) => String(x.id) === String(id));
    if (!s) return;
    url = s.view_url;
    windowUrl = s.direct_url;
    if (!prefer()) showFrame();
  }

  async function startCrm() {
    let data;
    try {
      const res = await fetch(surveyBox.dataset.lookupUrl);
      data = await res.json();
    } catch (e) {
      data = { ok: false, message: 'не удалось связаться с SyncVoice' };
    }
    if (!data.ok) {
      showBlocked(`Не удалось найти анкету в CRM: ${data.message}`);
      return;
    }
    surveys = data.surveys;
    choice.replaceChildren();
    for (const s of surveys) {
      const option = document.createElement('option');
      option.value = s.id;
      option.textContent = `${s.time || 'без даты'} · оператор ${s.operator_user_id ?? '?'}`
        + (s.id === data.selected ? ' — этот звонок' : '');
      choice.append(option);
    }
    choiceRow.hidden = !surveys.length;
    if (!surveys.length) {
      showBlocked('В CRM нет анкет с этим телефоном.');
      return;
    }
    if (!data.gateway) {
      showBlocked('Показ анкет внутри SyncVoice работает, только когда SyncVoice запущен ярлыком '
                  + '(manage.py run_app). Пока можно открыть анкету в окне рядом.');
    }
    const initial = data.selected ?? surveys[0].id;
    choice.value = initial;
    choiceNote.textContent = data.selected ? '' : 'анкета за день звонка не найдена — показана последняя';
    if (prefer()) {
      windowUrl = surveys.find((x) => x.id === initial).direct_url;
      showBlocked('Анкета открывается отдельным окном и сама переключается на нужного респондента, '
                  + 'когда вы переходите к другому звонку (‹ ›, Alt+←/→, «Готово → следующий»).');
      return;
    }
    if (data.gateway) selectSurvey(initial);
  }

  choice.addEventListener('change', () => selectSurvey(choice.value));

  async function start() {
    preferWindow.checked = prefer();
    if (surveyBox.dataset.lookupUrl) {
      startCrm();
      return;
    }
    if (preferWindow.checked) {
      showBlocked('Анкета открывается отдельным окном и сама переключается на нужного респондента, '
                  + 'когда вы переходите к другому звонку (‹ ›, Alt+←/→, «Готово → следующий»).');
      return;
    }
    try {
      const res = await fetch(`${surveyBox.dataset.checkUrl}?url=${encodeURIComponent(url)}`);
      const data = await res.json();
      if (data.embeddable === false) {
        showBlocked(`${data.reason}. Откройте анкету в окне рядом.`);
        return;
      }
    } catch (e) { /* check failed — just try to embed */ }
    showFrame();
  }

  document.getElementById('survey-reload').addEventListener('click', () => {
    if (!frame.hidden) frame.src = url;
  });
  document.getElementById('survey-window').addEventListener('click', () => openWindow());
  document.getElementById('survey-blocked-open').addEventListener('click', () => openWindow());
  preferWindow.addEventListener('change', () => {
    try { localStorage.setItem(PREFER_KEY, preferWindow.checked ? '1' : '0'); } catch (e) { /* ignore */ }
    start();
  });

  // Keys typed while the CRM has focus never reach the player: say so.
  window.addEventListener('blur', () => {
    setTimeout(() => { focusBack.hidden = document.activeElement !== frame; }, 0);
  });
  window.addEventListener('focus', () => { focusBack.hidden = true; });
  focusBack.addEventListener('click', () => {
    focusBack.hidden = true;
    document.getElementById('play').focus();
  });

  start();
})();
