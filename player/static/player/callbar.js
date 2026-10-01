(() => {
  'use strict';

  // Click on the respondent's number in the call header copies it (digits only).
  const button = document.getElementById('phone-copy');
  if (!button) return;
  const note = document.getElementById('phone-copy-note');
  let timer = null;

  async function copy(text) {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return;
    }
    // Fallback for browsers without the Clipboard API.
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    if (!ok) throw new Error('execCommand failed');
  }

  button.addEventListener('click', async () => {
    try {
      await copy(button.dataset.copy);
      note.textContent = 'Скопировано ✓';
      note.className = 'copy-note ok-text';
    } catch (e) {
      note.textContent = 'Не удалось скопировать';
      note.className = 'copy-note error-text';
    }
    note.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(() => { note.hidden = true; }, 1500);
  });
})();
