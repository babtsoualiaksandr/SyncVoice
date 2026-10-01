(() => {
  'use strict';

  // «Сверка с анкетой»: discrepancies between the operator's CRM survey and the call.
  const panel = document.getElementById('compare-panel');
  if (!panel) return;
  const form = document.getElementById('review-form');
  const statusEl = document.getElementById('compare-status');
  const summaryEl = document.getElementById('compare-summary');
  const list = document.getElementById('compare-list');
  const runBtn = document.getElementById('compare-run');
  const csrf = form.querySelector('[name=csrfmiddlewaretoken]').value;
  const POLL_MS = 5000;
  const POLL_LIMIT = 150;

  let data = JSON.parse(document.getElementById('compare-data').textContent);
  let polls = 0;
  let timer = null;

  function seconds(time) {
    const parts = String(time || '').split(':').map(Number);
    if (parts.some(Number.isNaN) || parts.length < 2) return null;
    return parts.reduce((acc, n) => acc * 60 + n, 0);
  }

  function addToComment(item) {
    const area = form.querySelector('[name="error_comment"]');
    const text = item.comment || `${item.field}: в анкете «${item.survey_value}», в разговоре «${item.call_value}»`;
    area.value = area.value.trim() ? `${area.value.trim()}\n${text}` : text;
    area.dispatchEvent(new Event('input', { bubbles: true })); // autosave (review.js)
    if (item.severity === 'ошибка') {
      const box = form.querySelector('[name="has_errors"]');
      if (!box.checked) {
        box.checked = true;
        box.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  }

  function renderItem(item) {
    const li = document.createElement('li');
    li.className = `compare-item severity-${item.severity}`;
    const head = document.createElement('div');
    head.className = 'compare-item-head';
    const badge = document.createElement('span');
    badge.className = 'compare-badge';
    badge.textContent = item.severity;
    const field = document.createElement('strong');
    field.textContent = item.field;
    head.append(badge, field);
    const at = seconds(item.time);
    if (at !== null) {
      const jump = document.createElement('button');
      jump.type = 'button';
      jump.className = 'link-btn';
      jump.textContent = `▶ ${item.time}`;
      jump.title = 'Перемотать запись сюда';
      jump.addEventListener('click', () => window.SyncVoicePlayer?.seek(at));
      head.append(jump);
    }
    const values = document.createElement('div');
    values.className = 'compare-values';
    values.textContent = `анкета: ${item.survey_value || '—'} · разговор: ${item.call_value || '—'}`;
    const comment = document.createElement('div');
    comment.className = 'hint';
    comment.textContent = item.comment;
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'link-btn';
    add.textContent = 'в комментарий';
    add.addEventListener('click', () => {
      addToComment(item);
      add.textContent = 'добавлено ✓';
      add.disabled = true;
    });
    li.append(head, values, comment, add);
    return li;
  }

  function render() {
    const status = data ? data.status : null;
    statusEl.textContent = {
      pending: data && data.error ? `ждёт повтора: ${data.error}` : 'сверяю…',
      error: `не удалось: ${data && data.error}`,
      done: data && data.discrepancies.length ? `расхождений: ${data.discrepancies.length}` : 'расхождений нет',
    }[status] ?? (runBtn.disabled ? 'после расшифровки' : 'ещё не запускалась');
    statusEl.classList.toggle('error-text', status === 'error');
    runBtn.textContent = status === 'done' || status === 'error' ? 'Заново' : 'Сверить';
    summaryEl.textContent = status === 'done' ? data.summary : '';
    summaryEl.hidden = !summaryEl.textContent;
    list.replaceChildren(...(status === 'done' ? data.discrepancies.map(renderItem) : []));
  }

  async function request(method) {
    const res = await fetch(panel.dataset.url, { method, headers: method === 'POST' ? { 'X-CSRFToken': csrf } : {} });
    const body = await res.json();
    if (!body.ok) throw new Error(body.message || `HTTP ${res.status}`);
    return body.comparison;
  }

  function schedulePoll() {
    clearTimeout(timer);
    if ((!data || data.status === 'pending') && polls++ < POLL_LIMIT) timer = setTimeout(poll, POLL_MS);
  }

  async function poll() {
    try {
      data = await request('GET');
      if (data) runBtn.disabled = false;
      render();
    } catch (e) { /* server restarting — keep polling */ }
    schedulePoll();
  }

  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    statusEl.textContent = 'ставлю в очередь…';
    try {
      data = await request('POST');
      polls = 0;
    } catch (e) {
      statusEl.textContent = e.message;
    }
    runBtn.disabled = false;
    render();
    schedulePoll();
  });

  render();
  schedulePoll();
})();
