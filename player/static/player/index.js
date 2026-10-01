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
