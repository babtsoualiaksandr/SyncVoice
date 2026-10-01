(() => {
  'use strict';

  const form = document.getElementById('review-form');
  if (!form) return;
  const state = document.getElementById('save-state');
  const hasErrors = form.querySelector('[name=has_errors]');

  let timer = null;
  let saving = Promise.resolve();
  let dirty = false;

  function setState(text, kind = '') {
    state.textContent = text;
    state.className = `save-state ${kind}`;
  }

  async function save(complete = false) {
    clearTimeout(timer);
    const body = new FormData(form);
    if (complete) body.set('complete', '1');
    setState('Сохраняю…');
    const res = await fetch(form.action, { method: 'POST', body });
    const data = await res.json().catch(() => ({ ok: false }));
    if (!res.ok || !data.ok) {
      const errors = data.errors ? Object.values(data.errors).flat().join(' ') : '';
      setState(`Не сохранено. ${errors}`, 'error-text');
      throw new Error('save failed');
    }
    dirty = false;
    setState(data.completed ? 'Проверен ✓' : 'Черновик сохранён', 'ok-text');
    return data;
  }

  function scheduleSave() {
    dirty = true;
    setState('Изменено…');
    clearTimeout(timer);
    timer = setTimeout(() => { saving = save().catch(() => {}); }, 700);
  }

  form.addEventListener('input', scheduleSave);
  form.addEventListener('change', (e) => {
    // «ошибка» / «брак» usually means there are errors to comment on.
    if (e.target.name === 'result' && e.target.value !== 'ок' && !hasErrors.checked) {
      hasErrors.checked = true;
    }
    scheduleSave();
  });

  async function complete() {
    await saving;
    try {
      const data = await save(true);
      if (data.next_url) {
        window.SyncVoiceSurvey?.follow(data.next_survey_url); // still within the key/click gesture
        window.location.href = data.next_url;
      }
    } catch (e) { /* message already shown */ }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    complete();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      complete();
    }
  });

  // Leaving the page with unsaved edits (e.g. Alt+→): send them in the background.
  window.addEventListener('pagehide', () => {
    if (dirty) navigator.sendBeacon(form.action, new FormData(form));
  });
})();
