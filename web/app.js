'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  playlists: [],
  jobId: null,
  poll: null,
  review: null,
  lowOnly: false,
  oauth: null,      // { device_code, interval, timer, deadline }
  hasOauthClient: false,
  connected: false,   // YouTube Music session is live
  ready: false,       // at least one classifier is configured
  reviewReady: false, // the job has produced classifications
};

// ---------------------------------------------------------------------------
// tiny fetch helper
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

function setStatus(el, message, kind = '') {
  el.textContent = message;
  el.className = `status ${kind}`;
}

// ---------------------------------------------------------------------------
// navigation
// ---------------------------------------------------------------------------

/** Why each step is or isn't reachable yet. Order matters: first unmet wins. */
function stepGate(name) {
  switch (name) {
    case 'configure':
      if (!state.connected) return 'Connect YouTube Music first.';
      return null;
    case 'progress':
      if (!state.connected) return 'Connect YouTube Music first.';
      if (!state.jobId) return 'Start a sort from Configure first.';
      return null;
    case 'review':
      if (!state.connected) return 'Connect YouTube Music first.';
      if (!state.jobId) return 'Start a sort from Configure first.';
      if (!state.reviewReady) return 'Still sorting — this unlocks when it finishes.';
      return null;
    default:
      return null;
  }
}

/** Paint the lock state onto the rail. Cheap, so it runs after any change. */
function updateSteps() {
  $$('.step').forEach((btn) => {
    const why = stepGate(btn.dataset.screen);
    btn.classList.toggle('locked', !!why);
    btn.setAttribute('aria-disabled', why ? 'true' : 'false');
    if (why) btn.dataset.why = why; else delete btn.dataset.why;
  });
}

let navHintTimer = null;

function flashNavHint(message) {
  const el = $('#navHint');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(navHintTimer);
  navHintTimer = setTimeout(() => el.classList.remove('show'), 3200);
}

function show(name) {
  $$('.screen').forEach((s) => s.classList.toggle('active', s.id === name));
  $$('.step').forEach((b) => b.classList.toggle('active', b.dataset.screen === name));
  updateSteps();
}

// Locked steps stay clickable on purpose: a disabled button can't explain
// itself, and "why is this greyed out" is the question we want to answer.
$$('.step').forEach((btn) => {
  btn.addEventListener('click', () => {
    const why = stepGate(btn.dataset.screen);
    if (why) return flashNavHint(why);
    show(btn.dataset.screen);
  });
});

// ---------------------------------------------------------------------------
// status bar
// ---------------------------------------------------------------------------

async function refreshStatus() {
  let s;
  try {
    s = await api('/api/status');
  } catch (e) {
    $('#providerBar').innerHTML = `<span class="pill bad">server unreachable</span>`;
    return null;
  }

  const pill = (label, ok, extra = '') =>
    `<span class="pill ${ok ? 'on' : 'off'}">${label}${extra ? ' · ' + extra : ''}</span>`;

  $('#providerBar').innerHTML = [
    pill('Gemini', s.gemini.configured, s.gemini.configured ? s.gemini.model : 'no key'),
    pill('Claude', s.claude.configured, s.claude.configured ? s.claude.model : 'unavailable'),
    pill('Last.fm', s.lastfm.configured, s.lastfm.configured ? 'tags on' : 'optional'),
    pill('YouTube Music', s.ytmusic.configured,
         s.ytmusic.configured ? (s.ytmusic.mode || '') : 'not connected'),
  ].join('');

  state.hasOauthClient = !!s.ytmusic?.oauth_client;
  state.connected = !!s.ytmusic?.configured;
  state.ready = !!s.ready;
  renderOauthPanel();
  updateSteps();
  return s;
}

// ---------------------------------------------------------------------------
// setup
// ---------------------------------------------------------------------------

$('#setupBtn').addEventListener('click', async () => {
  show('setup');
  await loadSettings();
});

$('#oauthToSetup').addEventListener('click', async () => {
  show('setup');
  await loadSettings();
});

async function loadSettings() {
  let s;
  try {
    s = await api('/api/settings');
  } catch (e) {
    return setStatus($('#setupStatus'), e.message, 'bad');
  }

  // Placeholders show what's already stored; empty inputs mean "leave alone".
  $('#setGeminiKey').placeholder = s.gemini_api_key_set
    ? `Saved (${s.gemini_api_key}) — type to replace` : 'Paste your key';
  $('#setGeminiModel').value = s.gemini_model || '';
  $('#setLastfmKey').placeholder = s.lastfm_api_key_set
    ? `Saved (${s.lastfm_api_key}) — type to replace` : 'Paste your key';
  $('#setOauthId').value = s.ytm_oauth_client_id || '';
  $('#setOauthSecret').placeholder = s.ytm_oauth_client_set
    ? `Saved (${s.ytm_oauth_client_secret}) — type to replace` : 'Paste the secret';

  $('#claudeState').textContent = s.claude_available
    ? 'The claude CLI is installed and on PATH — fallback is available.'
    : 'The claude CLI was not found on PATH. Gemini alone will be used.';
  $('#claudeState').className = `hint ${s.claude_available ? 'ok' : ''}`;

  const st = await api('/api/status').catch(() => null);
  const connected = !!st?.ytmusic?.configured;
  $('#ytmState').textContent = connected
    ? `Connected via ${st.ytmusic.mode}.` : 'Not connected yet.';
  $('#disconnectBtn').classList.toggle('hidden', !connected);
}

$('#saveSettingsBtn').addEventListener('click', async () => {
  const body = {
    gemini_api_key: $('#setGeminiKey').value.trim() || null,
    gemini_model: $('#setGeminiModel').value.trim() || null,
    lastfm_api_key: $('#setLastfmKey').value.trim() || null,
    ytm_oauth_client_id: $('#setOauthId').value.trim() || null,
    ytm_oauth_client_secret: $('#setOauthSecret').value.trim() || null,
  };

  setStatus($('#setupStatus'), 'Saving…');
  $('#saveSettingsBtn').disabled = true;
  try {
    const r = await api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
    setStatus($('#setupStatus'), r.message, 'ok');
    $('#setGeminiKey').value = '';
    $('#setLastfmKey').value = '';
    $('#setOauthSecret').value = '';
    await refreshStatus();
    await loadSettings();
  } catch (e) {
    setStatus($('#setupStatus'), e.message, 'bad');
  } finally {
    $('#saveSettingsBtn').disabled = false;
  }
});

$('#disconnectBtn').addEventListener('click', async () => {
  if (!confirm('Sign out of YouTube Music on this machine?')) return;
  try {
    await api('/api/auth/ytm/disconnect', { method: 'POST' });
    await refreshStatus();
    await loadSettings();
  } catch (e) {
    setStatus($('#setupStatus'), e.message, 'bad');
  }
});

// ---------------------------------------------------------------------------
// 1. connect — google sign-in
// ---------------------------------------------------------------------------

$$('.method').forEach((btn) => {
  btn.addEventListener('click', () => {
    $$('.method').forEach((b) => b.classList.toggle('active', b === btn));
    const oauth = btn.dataset.method === 'oauth';
    $('#methodOauth').classList.toggle('hidden', !oauth);
    $('#methodHeaders').classList.toggle('hidden', oauth);
  });
});

/** Show whichever of the three OAuth states applies right now. */
function renderOauthPanel() {
  const waiting = !!state.oauth;
  $('#oauthNoClient').classList.toggle('hidden', state.hasOauthClient);
  $('#oauthIdle').classList.toggle('hidden', !state.hasOauthClient || waiting);
  $('#oauthWaiting').classList.toggle('hidden', !waiting);
}

$('#oauthStartBtn').addEventListener('click', async () => {
  setStatus($('#oauthStatus'), '');
  $('#oauthStartBtn').disabled = true;
  try {
    const c = await api('/api/auth/ytm/oauth/start', { method: 'POST' });
    $('#oauthCode').textContent = c.user_code;
    $('#oauthLink').href = c.full_url;
    state.oauth = {
      device_code: c.device_code,
      interval: Math.max(2, c.interval || 5),
      deadline: Date.now() + (c.expires_in || 1800) * 1000,
      timer: null,
    };
    renderOauthPanel();
    window.open(c.full_url, '_blank', 'noreferrer');
    scheduleOauthPoll();
  } catch (e) {
    setStatus($('#oauthStatus'), e.message, 'bad');
  } finally {
    $('#oauthStartBtn').disabled = false;
  }
});

$('#oauthCancelBtn').addEventListener('click', () => {
  cancelOauth();
  setStatus($('#oauthStatus'), 'Sign-in cancelled.');
});

function cancelOauth() {
  if (state.oauth?.timer) clearTimeout(state.oauth.timer);
  state.oauth = null;
  renderOauthPanel();
}

function scheduleOauthPoll() {
  if (!state.oauth) return;
  state.oauth.timer = setTimeout(pollOauth, state.oauth.interval * 1000);
}

async function pollOauth() {
  if (!state.oauth) return;

  if (Date.now() > state.oauth.deadline) {
    cancelOauth();
    return setStatus($('#oauthStatus'), 'That code expired. Start again.', 'bad');
  }

  let r;
  try {
    r = await api('/api/auth/ytm/oauth/poll', {
      method: 'POST',
      body: JSON.stringify({ device_code: state.oauth.device_code }),
    });
  } catch (e) {
    cancelOauth();
    return setStatus($('#oauthStatus'), e.message, 'bad');
  }

  if (r.status === 'pending') return scheduleOauthPoll();

  cancelOauth();
  setStatus($('#oauthStatus'), r.message, 'ok');
  await refreshStatus();
  try {
    await loadPlaylists();
    show('configure');
  } catch (e) {
    setStatus($('#oauthStatus'), e.message, 'bad');
  }
}

// ---------------------------------------------------------------------------
// 1b. connect — pasted headers
// ---------------------------------------------------------------------------

$('#connectBtn').addEventListener('click', async () => {
  const raw = $('#headersInput').value.trim();
  if (!raw) return setStatus($('#connectStatus'), 'Paste your headers first.', 'bad');

  setStatus($('#connectStatus'), 'Connecting…');
  $('#connectBtn').disabled = true;
  try {
    const r = await api('/api/auth/ytm', {
      method: 'POST',
      body: JSON.stringify({ headers_raw: raw }),
    });
    setStatus($('#connectStatus'), r.message, 'ok');
    $('#headersInput').value = '';
    await refreshStatus();
    await loadPlaylists();
    show('configure');
  } catch (e) {
    setStatus($('#connectStatus'), e.message, 'bad');
  } finally {
    $('#connectBtn').disabled = false;
  }
});

// ---------------------------------------------------------------------------
// 2. configure
// ---------------------------------------------------------------------------

async function loadPlaylists() {
  const { playlists } = await api('/api/playlists');
  state.playlists = playlists;

  $('#sourceSelect').innerHTML = playlists
    .map((p) => `<option value="${p.playlist_id}">${escape(p.name)}${
      p.count != null ? ` (${p.count})` : ''}</option>`)
    .join('');

  renderBuckets();
}

function renderBuckets() {
  const sourceId = $('#sourceSelect').value;
  $('#bucketList').innerHTML = state.playlists
    .filter((p) => p.playlist_id !== sourceId)
    .map((p) => `
      <label class="bucket" data-id="${p.playlist_id}">
        <input type="checkbox" class="bucket-check">
        <div>
          <div class="name">${escape(p.name)}${
            p.count != null ? `<span class="count">${p.count} songs</span>` : ''}</div>
          <input type="text" class="bucket-desc"
                 placeholder="Describe the vibe — e.g. mellow, low-tempo, for winding down">
        </div>
      </label>
    `).join('');

  $$('.bucket-check').forEach((cb) => {
    cb.addEventListener('change', () => {
      cb.closest('.bucket').classList.toggle('checked', cb.checked);
    });
  });

  // Typing a description shouldn't toggle the surrounding label's checkbox.
  $$('.bucket-desc').forEach((input) => {
    input.addEventListener('click', (e) => e.preventDefault());
  });
}

$('#sourceSelect').addEventListener('change', renderBuckets);

$('#startBtn').addEventListener('click', async () => {
  if (!state.ready) {
    return setStatus($('#configStatus'),
      'No classifier configured. Add a Gemini API key on the Setup screen first.', 'bad');
  }

  const buckets = $$('.bucket')
    .filter((el) => el.querySelector('.bucket-check').checked)
    .map((el) => ({
      playlist_id: el.dataset.id,
      name: el.querySelector('.name').childNodes[0].textContent.trim(),
      description: el.querySelector('.bucket-desc').value.trim(),
    }));

  if (!buckets.length) {
    return setStatus($('#configStatus'), 'Pick at least one destination playlist.', 'bad');
  }

  const missing = buckets.filter((b) => !b.description).length;
  if (missing && !confirm(
    `${missing} playlist(s) have no description.\n\n` +
    `Descriptions are the biggest accuracy lever — without them the model is ` +
    `guessing from the playlist name alone.\n\nStart anyway?`
  )) return;

  setStatus($('#configStatus'), 'Starting…');
  $('#startBtn').disabled = true;
  try {
    const { job_id } = await api('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        source_playlist_id: $('#sourceSelect').value,
        buckets,
      }),
    });
    state.jobId = job_id;
    state.reviewReady = false;  // a fresh job re-locks Review until it produces results
    setStatus($('#configStatus'), '');
    show('progress');
    startPolling();
  } catch (e) {
    setStatus($('#configStatus'), e.message, 'bad');
  } finally {
    $('#startBtn').disabled = false;
  }
});

// ---------------------------------------------------------------------------
// 3. progress
// ---------------------------------------------------------------------------

function startPolling() {
  stopPolling();
  state.poll = setInterval(pollJob, 1200);
  pollJob();
}

function stopPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = null;
}

async function pollJob() {
  if (!state.jobId) return;
  let job;
  try {
    job = await api(`/api/jobs/${state.jobId}`);
  } catch {
    return;
  }

  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  $('#progressBar').firstElementChild.style.width = `${pct}%`;
  $('#progressLabel').textContent =
    job.total ? `${job.done} / ${job.total} · ${pct}%` : 'Working…';
  $('#phaseMsg').textContent = job.message || job.phase;

  $('#providerDetail').innerHTML = (job.providers?.chain || [])
    .filter((p) => p.available)
    .map((p) => `
      <div class="provider-card ${p.exhausted ? 'exhausted' : ''}">
        <div>${p.name}${p.exhausted ? ' — out of quota' : ''}</div>
        <div class="n">${p.count} request${p.count === 1 ? '' : 's'}</div>
      </div>
    `).join('');

  $('#eventLog').innerHTML = (job.providers?.events || [])
    .map((e) => `<div>${escape(e)}</div>`).join('');

  const paused = job.phase === 'paused';
  const finished = ['classified', 'done'].includes(job.phase);

  // Review opens once there is something to review — including a paused job,
  // which has partial results worth looking at.
  state.reviewReady = finished || paused;
  updateSteps();

  $('#pauseBtn').classList.toggle('hidden', paused || finished);
  $('#resumeBtn').classList.toggle('hidden', !paused);
  $('#toReviewBtn').classList.toggle('hidden', !finished && !paused);

  if (finished || job.phase === 'error') stopPolling();
}

$('#pauseBtn').addEventListener('click', async () => {
  await api(`/api/jobs/${state.jobId}/pause`, { method: 'POST' });
});

$('#resumeBtn').addEventListener('click', async () => {
  await api(`/api/jobs/${state.jobId}/resume`, { method: 'POST' });
  startPolling();
});

$('#toReviewBtn').addEventListener('click', () => {
  show('review');
  loadReview();
});

// ---------------------------------------------------------------------------
// 4. review
// ---------------------------------------------------------------------------

$('#refreshReview').addEventListener('click', loadReview);
$('#lowOnly').addEventListener('change', (e) => {
  state.lowOnly = e.target.checked;
  renderReview();
});

async function loadReview() {
  if (!state.jobId) return;
  $('#reviewBody').innerHTML = '<div class="empty">Loading…</div>';
  state.review = await api(`/api/jobs/${state.jobId}/review`);
  renderReview();
}

function renderReview() {
  const data = state.review;
  if (!data) return;

  const s = data.stats;
  $('#reviewStats').innerHTML = [
    stat(s.total, 'classified'),
    stat(s.assigned, 'assigned'),
    stat(s.unmatched, 'matched nothing'),
    stat(s.low_confidence, 'low confidence'),
    ...Object.entries(s.providers || {}).map(([k, v]) => stat(v, `${k} calls`)),
  ].join('');

  const allNames = data.buckets.map((b) => b.name);
  const groups = [];

  for (const bucket of data.buckets) {
    const rows = state.lowOnly
      ? bucket.songs.filter((r) => r.low_confidence)
      : bucket.songs;
    groups.push(group(bucket.name, bucket.description, rows, allNames));
  }

  if (data.unmatched.length) {
    groups.push(group(
      'Matched nothing',
      'These did not clearly fit any playlist. Assign them by hand or leave them out.',
      state.lowOnly ? data.unmatched.filter((r) => r.low_confidence) : data.unmatched,
      allNames,
    ));
  }

  $('#reviewBody').innerHTML = groups.join('');
  wireChips();
}

function stat(value, label) {
  return `<div class="stat"><div class="v">${value}</div><div class="k">${label}</div></div>`;
}

function group(name, description, rows, allNames) {
  if (!rows.length) {
    return `<div class="bucket-group"><h4>${escape(name)} <span class="n">0 songs</span></h4>
            <div class="empty">Nothing here.</div></div>`;
  }

  const body = rows.map((r) => `
    <tr class="${r.low_confidence ? 'low' : ''}">
      <td>
        <div class="song-title">${escape(r.title)}</div>
        <div class="song-artist">${escape(r.artists)}</div>
      </td>
      <td class="conf ${r.low_confidence ? 'low' : ''}">${r.confidence.toFixed(2)}</td>
      <td class="reason">${escape(r.reason)}</td>
      <td>
        <div class="chips" data-video="${r.video_id}">
          ${allNames.map((n) => `
            <span class="chip ${r.playlists.includes(n) ? 'on' : ''}"
                  data-name="${escape(n)}">${escape(n)}</span>
          `).join('')}
        </div>
      </td>
    </tr>
  `).join('');

  return `
    <div class="bucket-group">
      <h4>${escape(name)} <span class="n">${rows.length} songs</span></h4>
      ${description ? `<div class="hint">${escape(description)}</div>` : ''}
      <table>
        <thead><tr><th>Song</th><th>Conf</th><th>Why</th><th>Playlists</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function wireChips() {
  $$('.chip').forEach((chip) => {
    chip.addEventListener('click', async () => {
      const container = chip.closest('.chips');
      const videoId = container.dataset.video;

      chip.classList.toggle('on');
      const playlists = Array.from(container.querySelectorAll('.chip.on'))
        .map((c) => c.dataset.name);

      try {
        await api(`/api/jobs/${state.jobId}/override`, {
          method: 'POST',
          body: JSON.stringify({ video_id: videoId, playlists }),
        });
        // Keep every copy of this song in sync across the other groups.
        $$(`.chips[data-video="${videoId}"]`).forEach((other) => {
          if (other === container) return;
          other.querySelectorAll('.chip').forEach((c) => {
            c.classList.toggle('on', playlists.includes(c.dataset.name));
          });
        });
      } catch (e) {
        chip.classList.toggle('on'); // roll back the visual change
        alert(`Could not save: ${e.message}`);
      }
    });
  });
}

$('#applyBtn').addEventListener('click', async () => {
  const s = state.review?.stats;
  if (!confirm(
    `Add ${s?.assigned ?? '?'} songs to your destination playlists?\n\n` +
    `Songs are only ever added. Your source playlist is not modified.`
  )) return;

  $('#applyBtn').disabled = true;
  setStatus($('#applyStatus'), 'Applying…');

  try {
    await api(`/api/jobs/${state.jobId}/apply`, { method: 'POST' });
    const timer = setInterval(async () => {
      const job = await api(`/api/jobs/${state.jobId}`);
      setStatus($('#applyStatus'), job.message,
        job.phase === 'done' ? 'ok' : job.phase === 'error' ? 'bad' : '');
      if (['done', 'error', 'paused'].includes(job.phase)) {
        clearInterval(timer);
        $('#applyBtn').disabled = false;
      }
    }, 1500);
  } catch (e) {
    setStatus($('#applyStatus'), e.message, 'bad');
    $('#applyBtn').disabled = false;
  }
});

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

function escape(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

(async function boot() {
  const s = await refreshStatus();

  // First run: no classifier configured at all, so keys come before anything else.
  if (s && !s.ready) {
    show('setup');
    await loadSettings();
    return;
  }

  if (s?.ytmusic?.configured) {
    try {
      await loadPlaylists();
      show('configure');
      return;
    } catch { /* fall through to connect */ }
  }
  show('connect');
})();
