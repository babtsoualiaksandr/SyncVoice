(() => {
  'use strict';

  const form = document.getElementById('upload-form');
  if (!form) return;
  const input = form.querySelector('input[type="file"]');
  const zone = document.getElementById('drop-zone');
  const list = document.getElementById('file-list');
  const submit = document.getElementById('upload-submit');
  const dateInput = form.querySelector('[name="call_started_at"]');

  // Same format as CALL_NAME_RE in player/models.py.
  const CALL_NAME = /^[a-z]+-(\d+)-(\d+)-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-\d+(?:_[A-Za-z0-9]+)?\.\d+/;

  function describe(file) {
    const m = CALL_NAME.exec(file.name);
    if (!m) return null;
    const [, phone, ext, y, mo, d, h, mi] = m;
    return `${d}.${mo}.${y} ${h}:${mi}, внутр. ${ext}, +${phone}`;
  }

  function render() {
    const files = [...input.files];
    list.replaceChildren();
    list.hidden = !files.length;
    let others = 0;
    for (const file of files) {
      const li = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'file-name';
      name.textContent = file.name;
      const info = document.createElement('span');
      const details = describe(file);
      if (!file.name.toLowerCase().endsWith('.wav')) {
        info.className = 'error-text';
        info.textContent = 'не WAV — не будет загружен';
      } else if (details) {
        info.className = 'hint';
        info.textContent = details;
      } else {
        others++;
        info.className = 'warn-text';
        info.textContent = 'нужна дата звонка ниже';
      }
      li.append(name, info);
      list.append(li);
    }
    dateInput.required = others > 0;
    submit.textContent = files.length > 1 ? `Загрузить и распознать (${files.length})` : 'Загрузить и распознать';
  }

  input.addEventListener('change', render);

  for (const type of ['dragenter', 'dragover']) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add('dragging'); });
  }
  for (const type of ['dragleave', 'drop']) {
    zone.addEventListener(type, () => zone.classList.remove('dragging'));
  }
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    const transfer = new DataTransfer();
    for (const file of e.dataTransfer.files) transfer.items.add(file);
    input.files = transfer.files;
    render();
  });

  form.addEventListener('submit', () => {
    submit.disabled = true;
    submit.textContent = 'Загружаю…';
  });

  render();
})();
