(() => {
  'use strict';

  const root = document.getElementById('player');
  if (!root) return;

  const $ = (id) => document.getElementById(id);
  const audio = $('audio');
  const els = {
    play: $('play'), back: $('back'), forward: $('forward'),
    prevSeg: $('prev-seg'), nextSeg: $('next-seg'),
    loopSeg: $('loop-seg'), mute: $('mute'), volume: $('volume'),
    speed: $('speed'), timeline: $('timeline'), progress: $('progress'),
    buffered: $('buffered'), thumb: $('thumb'), tooltip: $('tooltip'),
    marks: $('segment-marks'), current: $('current-time'), duration: $('duration'),
    caption: $('caption'), transcript: $('transcript'), empty: $('transcript-empty'),
    search: $('search'), follow: $('follow'), status: $('status'),
    saveMenu: $('save-menu'),
  };

  const SKIP = 5;
  const RATES = [...els.speed.querySelectorAll('button')].map((b) => Number(b.dataset.rate));
  // Jumping to "previous phrase" within this many seconds of a phrase start
  // goes to the phrase before; later it restarts the current one.
  const RESTART_THRESHOLD = 1.5;

  let segments = [];
  let rows = [];
  let activeIndex = -1;
  let loopIndex = -1;
  let dragging = false;
  let followPausedUntil = 0;

  // ---------- helpers ----------

  function fmt(sec) {
    if (!Number.isFinite(sec) || sec < 0) sec = 0;
    const s = Math.floor(sec % 60);
    const m = Math.floor(sec / 60) % 60;
    const h = Math.floor(sec / 3600);
    const ss = String(s).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
  }

  function duration() {
    if (Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
    return Number(root.dataset.duration) || 0;
  }

  function seek(t) {
    const d = duration();
    audio.currentTime = Math.max(0, d ? Math.min(t, d) : t);
    render();
  }

  function store(key, value) {
    try { localStorage.setItem(`syncvoice.${key}`, value); } catch (e) { /* storage unavailable */ }
  }
  function load(key) {
    try { return localStorage.getItem(`syncvoice.${key}`); } catch (e) { return null; }
  }

  // Index of the last segment that started at or before t (binary search).
  function segmentAt(t) {
    let lo = 0, hi = segments.length - 1, found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (segments[mid].start <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return found;
  }

  // ---------- playback ----------

  function togglePlay() {
    if (audio.paused) audio.play(); else audio.pause();
  }

  function setRate(rate) {
    audio.playbackRate = rate;
    audio.preservesPitch = true;
    for (const b of els.speed.querySelectorAll('button')) {
      b.classList.toggle('active', Number(b.dataset.rate) === rate);
    }
    store('rate', rate);
  }

  function stepRate(dir) {
    const i = RATES.indexOf(audio.playbackRate);
    const next = RATES[Math.max(0, Math.min(RATES.length - 1, (i < 0 ? RATES.indexOf(1) : i) + dir))];
    setRate(next);
  }

  function setVolume(v) {
    audio.volume = v;
    audio.muted = v === 0;
    els.volume.value = v;
    store('volume', v);
    renderMute();
  }

  function renderMute() {
    els.mute.textContent = audio.muted || audio.volume === 0 ? '🔇' : '🔊';
  }

  function gotoSegment(i, play = true) {
    if (!segments.length) return;
    i = Math.max(0, Math.min(segments.length - 1, i));
    if (loopIndex >= 0) loopIndex = i;
    seek(segments[i].start);
    followPausedUntil = 0;
    if (play) audio.play();
  }

  function prevSegment() {
    const i = segmentAt(audio.currentTime);
    if (i < 0) return gotoSegment(0);
    const intoSegment = audio.currentTime - segments[i].start;
    gotoSegment(intoSegment > RESTART_THRESHOLD ? i : i - 1);
  }

  function nextSegment() {
    gotoSegment(segmentAt(audio.currentTime) + 1);
  }

  function toggleLoop() {
    loopIndex = loopIndex >= 0 ? -1 : Math.max(segmentAt(audio.currentTime), 0);
    els.loopSeg.setAttribute('aria-pressed', String(loopIndex >= 0));
  }

  // ---------- rendering ----------

  function render() {
    const d = duration();
    const t = audio.currentTime;
    const pct = d ? (t / d) * 100 : 0;
    els.progress.style.width = `${pct}%`;
    els.thumb.style.left = `${pct}%`;
    els.current.textContent = fmt(t);
    els.duration.textContent = fmt(d);
    els.timeline.setAttribute('aria-valuemax', String(Math.round(d)));
    els.timeline.setAttribute('aria-valuenow', String(Math.round(t)));
    els.timeline.setAttribute('aria-valuetext', `${fmt(t)} из ${fmt(d)}`);
    els.play.textContent = audio.paused ? '▶' : '⏸';
    renderActive(t);
  }

  function renderBuffered() {
    const d = duration();
    const b = audio.buffered;
    els.buffered.style.width = d && b.length ? `${(b.end(b.length - 1) / d) * 100}%` : '0';
  }

  function renderActive(t) {
    const i = segmentAt(t);
    const seg = segments[i];
    // Between phrases (silence) the caption goes blank.
    const inside = seg && t <= seg.end + 0.25;
    els.caption.textContent = inside ? seg.text : '';
    els.caption.classList.toggle('empty', !inside);

    if (i === activeIndex) return;
    if (rows[activeIndex]) rows[activeIndex].classList.remove('active');
    activeIndex = i;
    const row = rows[i];
    if (!row) return;
    row.classList.add('active');
    if (els.follow.getAttribute('aria-pressed') === 'true' && Date.now() > followPausedUntil) {
      scrollToRow(row);
    }
  }

  function scrollToRow(row) {
    const box = els.transcript;
    const top = row.offsetTop - box.offsetTop - box.clientHeight / 3;
    box.scrollTo({ top, behavior: 'smooth' });
  }

  function renderSegments() {
    els.transcript.replaceChildren();
    els.marks.replaceChildren();
    rows = [];
    activeIndex = -1;

    if (!segments.length) {
      els.empty.textContent = 'В записи не найдено речи.';
      els.transcript.append(els.empty);
      return;
    }

    const d = duration();
    const frag = document.createDocumentFragment();
    segments.forEach((seg, i) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'line';
      row.dataset.index = i;
      const time = document.createElement('span');
      time.className = 'line-time';
      time.textContent = fmt(seg.start);
      const text = document.createElement('span');
      text.className = 'line-text';
      text.textContent = seg.text;
      row.append(time, text);
      rows.push(row);
      frag.append(row);

      if (d) {
        const mark = document.createElement('span');
        mark.className = 'segment-mark';
        mark.style.left = `${(seg.start / d) * 100}%`;
        mark.style.width = `${Math.max(((seg.end - seg.start) / d) * 100, 0.2)}%`;
        els.marks.append(mark);
      }
    });
    els.transcript.append(frag);
    applySearch();
    render();
  }

  function applySearch() {
    const q = els.search.value.trim().toLowerCase();
    let shown = 0;
    rows.forEach((row, i) => {
      const hit = !q || segments[i].text.toLowerCase().includes(q);
      row.hidden = !hit;
      if (hit) shown++;
    });
    let none = els.transcript.querySelector('.no-results');
    if (q && !shown) {
      if (!none) {
        none = document.createElement('p');
        none.className = 'hint no-results';
        els.transcript.append(none);
      }
      none.textContent = `Ничего не найдено по запросу «${els.search.value.trim()}».`;
    } else if (none) {
      none.remove();
    }
  }

  // ---------- subtitles loading ----------

  async function loadSubtitles() {
    let data;
    try {
      const res = await fetch(root.dataset.subtitlesUrl, { headers: { Accept: 'application/json' } });
      data = await res.json();
    } catch (e) {
      els.empty.textContent = 'Не удалось загрузить субтитры. Повторяю…';
      return setTimeout(loadSubtitles, 5000);
    }

    els.status.textContent = data.status_display;
    els.status.className = `status status-${data.status}`;

    els.saveMenu.hidden = data.status !== 'done' || !data.segments.length;
    if (data.status === 'done') {
      segments = data.segments;
      renderSegments();
    } else if (data.status === 'error') {
      els.empty.textContent = `Распознавание не удалось: ${data.error}`;
    } else {
      els.empty.textContent = 'Идёт распознавание речи… Слушать можно уже сейчас, субтитры появятся автоматически.';
      setTimeout(loadSubtitles, 3000);
    }
  }

  // ---------- timeline (click, drag, hover, keyboard) ----------

  function timeFromPointer(e) {
    const rect = els.timeline.getBoundingClientRect();
    const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
    return { t: (x / rect.width) * duration(), x };
  }

  function showTooltip(e) {
    const { t, x } = timeFromPointer(e);
    const i = segmentAt(t);
    const seg = segments[i];
    const text = seg && t <= seg.end ? seg.text : '';
    els.tooltip.hidden = false;
    els.tooltip.style.left = `${x}px`;
    els.tooltip.replaceChildren();
    const time = document.createElement('strong');
    time.textContent = fmt(t);
    els.tooltip.append(time);
    if (text) {
      const p = document.createElement('span');
      p.textContent = text.length > 80 ? `${text.slice(0, 80)}…` : text;
      els.tooltip.append(p);
    }
  }

  els.timeline.addEventListener('pointerdown', (e) => {
    if (!duration()) return;
    dragging = true;
    els.timeline.setPointerCapture(e.pointerId);
    els.timeline.classList.add('dragging');
    seek(timeFromPointer(e).t);
  });
  els.timeline.addEventListener('pointermove', (e) => {
    if (!duration()) return;
    showTooltip(e);
    if (dragging) seek(timeFromPointer(e).t);
  });
  const endDrag = () => {
    dragging = false;
    els.timeline.classList.remove('dragging');
  };
  els.timeline.addEventListener('pointerup', endDrag);
  els.timeline.addEventListener('pointercancel', endDrag);
  els.timeline.addEventListener('pointerleave', () => { if (!dragging) els.tooltip.hidden = true; });
  els.timeline.addEventListener('keydown', (e) => {
    if (e.key === 'Home') { seek(0); e.preventDefault(); }
    if (e.key === 'End') { seek(duration()); e.preventDefault(); }
  });

  // ---------- audio events ----------

  audio.addEventListener('timeupdate', () => {
    if (loopIndex >= 0 && segments[loopIndex] && audio.currentTime >= segments[loopIndex].end) {
      audio.currentTime = segments[loopIndex].start;
    }
    if (!dragging) render();
  });
  audio.addEventListener('play', render);
  audio.addEventListener('pause', render);
  audio.addEventListener('ended', render);
  audio.addEventListener('progress', renderBuffered);
  audio.addEventListener('loadedmetadata', () => {
    render();
    renderBuffered();
    if (segments.length) renderSegments(); // redraw timeline marks with the real duration
  });
  audio.addEventListener('volumechange', renderMute);

  // ---------- controls ----------

  els.play.addEventListener('click', togglePlay);
  els.back.addEventListener('click', () => seek(audio.currentTime - SKIP));
  els.forward.addEventListener('click', () => seek(audio.currentTime + SKIP));
  els.prevSeg.addEventListener('click', prevSegment);
  els.nextSeg.addEventListener('click', nextSegment);
  els.loopSeg.addEventListener('click', toggleLoop);
  els.speed.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-rate]');
    if (b) setRate(Number(b.dataset.rate));
  });
  els.mute.addEventListener('click', () => {
    if (audio.muted || audio.volume === 0) setVolume(Number(load('volume')) || 1);
    else { audio.muted = true; }
  });
  els.volume.addEventListener('input', () => setVolume(Number(els.volume.value)));

  els.transcript.addEventListener('click', (e) => {
    const row = e.target.closest('.line');
    if (row) gotoSegment(Number(row.dataset.index));
  });
  // Manual scrolling pauses auto-follow for a few seconds.
  const pauseFollow = () => { followPausedUntil = Date.now() + 4000; };
  els.transcript.addEventListener('wheel', pauseFollow, { passive: true });
  els.transcript.addEventListener('touchmove', pauseFollow, { passive: true });

  els.follow.addEventListener('click', () => {
    const on = els.follow.getAttribute('aria-pressed') !== 'true';
    els.follow.setAttribute('aria-pressed', String(on));
    followPausedUntil = 0;
    if (on && rows[activeIndex]) scrollToRow(rows[activeIndex]);
  });
  els.search.addEventListener('input', applySearch);

  // Close the save menu after choosing a format or clicking elsewhere.
  els.saveMenu.addEventListener('click', (e) => {
    if (e.target.closest('a')) els.saveMenu.open = false;
  });
  document.addEventListener('click', (e) => {
    if (els.saveMenu.open && !els.saveMenu.contains(e.target)) els.saveMenu.open = false;
  });

  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const actions = {
      ' ': togglePlay,
      k: togglePlay,
      ArrowLeft: () => seek(audio.currentTime - SKIP),
      ArrowRight: () => seek(audio.currentTime + SKIP),
      ArrowUp: prevSegment,
      ArrowDown: nextSegment,
      '[': () => stepRate(-1),
      ']': () => stepRate(1),
      l: toggleLoop,
      m: () => els.mute.click(),
    };
    const action = actions[e.key] || actions[e.key.toLowerCase()];
    if (!action) return;
    // preventDefault also stops Space from "clicking" a focused button.
    e.preventDefault();
    action();
  });

  const deleteForm = $('delete-form');
  deleteForm?.addEventListener('submit', (e) => {
    if (!window.confirm('Удалить запись и её субтитры?')) e.preventDefault();
  });

  // ---------- init ----------

  setRate(RATES.includes(Number(load('rate'))) ? Number(load('rate')) : 1);
  const savedVolume = load('volume');
  if (savedVolume !== null) setVolume(Number(savedVolume));
  render();
  loadSubtitles();
})();
