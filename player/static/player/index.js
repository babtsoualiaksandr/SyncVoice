(() => {
  'use strict';

  const statusEl = document.getElementById('sync-status');
  const warning = document.getElementById('worker-warning');
  if (!statusEl) return;

  let lastKey = '';

  function describe(data) {
    const parts = [];
    const s = data.sync;
    if (s) {
      if (s.status === 'error') {
        parts.push(`Загрузка за ${s.day}: ошибка — ${s.error}`);
      } else if (s.status === 'done' && s.failed) {
        parts.push(`${s.day}: скачано ${s.downloaded} из ${s.found}, не скачано ${s.failed}`
                   + (s.skipped ? `, уже были ${s.skipped}` : '') + ` — ${s.error}`);
      } else if (s.status === 'pending') {
        parts.push(`Загрузка за ${s.day}: в очереди…`);
      } else {
        const verb = s.status === 'running' ? 'идёт загрузка' : 'загружено';
        let line = `${s.day}: ${verb} — скачано ${s.downloaded} из ${s.found}`;
        if (s.skipped) line += `, уже были ${s.skipped}`;
        parts.push(line);
      }
    }
    if (data.queue) parts.push(`в очереди на распознавание: ${data.queue}`);
    const ai = data.ai;
    if (ai && (ai.analysis || ai.compare)) {
      const jobs = [];
      if (ai.analysis) jobs.push(`подсказки ${ai.analysis}`);
      if (ai.compare) jobs.push(`сверки ${ai.compare}`);
      parts.push(`ИИ в очереди: ${jobs.join(', ')}` + (ai.waiting ? ` (ждёт: ${ai.waiting})` : ''));
      if (ai.now) {
        const now = [];
        const describeModel = (title, m) => {
          if (!m) return;
          if (!m.model) { now.push(`${title} → все модели исчерпали дневной лимит`); return; }
          now.push(`${title} → ${m.model}` + (m.spare ? ' (запасная)' : '')
                   + (m.wait ? `, следующий запрос через ${m.wait} с` : ''));
        };
        if (ai.analysis) describeModel('подсказки', ai.now.analysis);
        if (ai.compare) describeModel('сверка', ai.now.compare);
        if (now.length) parts.push(`Сейчас: ${now.join(' · ')}`);
      }
    }
    if (ai && ai.exhausted && ai.exhausted.length) {
      parts.push('Дневной лимит Gemini исчерпан: '
                 + ai.exhausted.map((q) => `${q.model} до ${q.until}`).join(', '));
    }
    return parts.join(' · ');
  }

  async function poll() {
    try {
      const res = await fetch(statusEl.dataset.statusUrl, { headers: { Accept: 'application/json' } });
      const data = await res.json();
      warning.hidden = data.worker_alive;
      const text = describe(data);
      if (text) statusEl.textContent = text;
      statusEl.classList.toggle('flash-error', data.sync?.status === 'error' || !!data.sync?.failed);

      // Reload the call list once a running sync finishes with new calls.
      const key = data.sync ? data.sync.status : '';
      if (['pending', 'running'].includes(lastKey) && key === 'done' && data.sync.downloaded) {
        window.location.reload();
      }
      lastKey = key;
    } catch (e) {
      // server restarting — try again later
    }
    setTimeout(poll, 3000);
  }

  poll();
})();

// «Отчёт контролёра + ИИ»: send the chosen file at once; the answer is a download.
{
  const form = document.getElementById('report-ai-form');
  if (form) {
    form.querySelector('input[type="file"]').addEventListener('change', (event) => {
      if (!event.target.files.length) return;
      form.submit();
      setTimeout(() => form.reset(), 1000);  // the same file can be chosen again
    });
  }
}

// Year / month filter: go to the latest downloaded day of the chosen period.
{
  const form = document.getElementById('period-filter');
  if (form) {
    for (const select of form.querySelectorAll('select')) {
      select.addEventListener('change', () => {
        window.location.search = `?${select.dataset.param}=${encodeURIComponent(select.value)}`;
      });
    }
  }
}
