(() => {
  'use strict';

  const panel = document.getElementById('ai-panel');
  if (!panel) return;
  const form = document.getElementById('review-form');
  const statusEl = document.getElementById('ai-status');
  const notesEl = document.getElementById('ai-notes');
  const runBtn = document.getElementById('ai-run');
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]').value;
  const POLL_MS = 4000;
  const POLL_LIMIT = 150; // ~10 minutes: transcription of a long call

  let ai = JSON.parse(document.getElementById('ai-data').textContent);
  let polls = 0;
  let timer = null;

  function input(field) {
    return form.querySelector(`[name="${field}"]`);
  }

  function renderHints() {
    for (const hint of form.querySelectorAll('.ai-hint')) {
      const field = hint.dataset.aiField;
      const value = ai && ai.status === 'done' ? ai.fields[field] : '';
      const current = input(field).value.trim();
      hint.replaceChildren();
      hint.hidden = !value || value === current;
      if (hint.hidden) continue;
      const label = document.createElement('span');
      label.textContent = `ИИ: ${value}`;
      const apply = document.createElement('button');
      apply.type = 'button';
      apply.className = 'link-btn';
      apply.textContent = current ? 'заменить' : 'подставить';
      apply.addEventListener('click', () => {
        const el = input(field);
        el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true })); // autosave (review.js)
        renderHints();
      });
      hint.append(label, apply);
    }
  }

  function render() {
    const status = ai ? ai.status : null;
    statusEl.textContent = {
      pending: ai && ai.error ? `ждёт повтора: ${ai.error}` : 'анализирую…',
      error: `ошибка: ${ai && ai.error}`,
      done: '',
    }[status] ?? (runBtn.disabled ? 'после расшифровки' : 'ещё не запускался');
    statusEl.classList.toggle('error-text', status === 'error');
    notesEl.textContent = status === 'done' ? ai.notes : '';
    notesEl.hidden = !notesEl.textContent;
    runBtn.textContent = status === 'done' ? 'Заново' : 'Заполнить с ИИ';
    runBtn.disabled = runBtn.disabled && status !== 'done';
    renderHints();
  }

  async function request(method) {
    const res = await fetch(panel.dataset.url, {
      method,
      headers: method === 'POST' ? { 'X-CSRFToken': csrf } : {},
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.message || `HTTP ${res.status}`);
    return data.analysis;
  }

  function schedulePoll() {
    clearTimeout(timer);
    const waiting = !ai || ai.status === 'pending';
    if (waiting && polls++ < POLL_LIMIT) timer = setTimeout(poll, POLL_MS);
  }

  async function poll() {
    try {
      ai = await request('GET');
      if (ai) runBtn.disabled = false; // queued => the call is transcribed
      render();
    } catch (e) { /* server restarting — keep polling */ }
    schedulePoll();
  }

  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    statusEl.textContent = 'ставлю в очередь…';
    try {
      ai = await request('POST');
      polls = 0;
    } catch (e) {
      statusEl.textContent = e.message;
      statusEl.classList.add('error-text');
    }
    runBtn.disabled = false;
    render();
    schedulePoll();
  });

  // Hints disappear once the field matches the suggestion (typed or applied).
  form.addEventListener('input', (e) => {
    if (['city', 'listen_city', 'stations'].includes(e.target.name)) renderHints();
  });

  render();
  schedulePoll();
})();
