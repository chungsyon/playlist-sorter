'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  playlists: [],
  groups: [],       // user-named axes; a playlist may sit on one of them
  jobId: null,
  poll: null,
  review: null,
  lowOnly: false,
  oauth: null,      // { device_code, interval, timer, deadline }
  hasOauthClient: false,
  connected: false,   // YouTube Music session is live
  ready: false,       // at least one classifier is configured
  reviewReady: false, // the job has produced classifications
  applied: false,     // Apply has run, so there is a summary to show
  summary: null,
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
    case 'summary':
      if (!state.connected) return 'Connect YouTube Music first.';
      if (!state.jobId) return 'Start a sort from Configure first.';
      if (!state.applied) return 'Nothing applied yet — this unlocks after you press Apply.';
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
          <span class="select-wrap bucket-group-wrap">
            <select class="bucket-group-select"></select>
          </span>
        </div>
      </label>
    `).join('');

  $$('.bucket-check').forEach((cb) => {
    cb.addEventListener('change', () => {
      cb.closest('.bucket').classList.toggle('checked', cb.checked);
    });
  });

  // Typing a description or picking a group shouldn't toggle the surrounding
  // label's checkbox.
  $$('.bucket-desc').forEach((input) => {
    input.addEventListener('click', (e) => e.preventDefault());
  });

  $$('.bucket-group-select').forEach((sel) => {
    sel.addEventListener('click', (e) => e.preventDefault());
    sel.addEventListener('change', () => {
      // Putting a playlist in a group is a clear statement that you want it
      // used, so tick it rather than making that a second, forgettable step.
      if (!sel.value) return;
      const check = sel.closest('.bucket').querySelector('.bucket-check');
      check.checked = true;
      sel.closest('.bucket').classList.add('checked');
    });
  });

  syncGroupSelects();
}

$('#sourceSelect').addEventListener('change', renderBuckets);

// ---------------------------------------------------------------------------
// groups — the optional second axis
// ---------------------------------------------------------------------------

function renderGroups() {
  $('#groupChips').innerHTML = state.groups.length
    ? state.groups.map((g) => `
        <span class="group-chip">${escape(g)}<button class="x" data-group="${escape(g)}"
          aria-label="Remove ${escape(g)}">&times;</button></span>`).join('')
    : '<span class="group-empty">No groups yet — every playlist stays optional.</span>';

  $$('#groupChips .x').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.groups = state.groups.filter((g) => g !== btn.dataset.group);
      renderGroups();
    });
  });

  syncGroupSelects();
}

/** Repopulate the per-playlist group pickers, keeping any choice still valid. */
function syncGroupSelects() {
  const options = ['<option value="">No group</option>']
    .concat(state.groups.map((g) => `<option value="${escape(g)}">${escape(g)}</option>`))
    .join('');

  $$('.bucket-group-select').forEach((sel) => {
    const current = sel.value;
    sel.innerHTML = options;
    sel.value = state.groups.includes(current) ? current : '';
  });

  // With no groups defined the pickers are noise, so they stay out of the way.
  $$('.bucket-group-wrap').forEach((wrap) => {
    wrap.style.display = state.groups.length ? '' : 'none';
  });
}

function addGroup() {
  const input = $('#groupInput');
  const name = input.value.trim();
  if (!name) return;
  if (state.groups.some((g) => g.toLowerCase() === name.toLowerCase())) {
    return setStatus($('#configStatus'), `There is already a group called "${name}".`, 'bad');
  }
  state.groups.push(name);
  input.value = '';
  setStatus($('#configStatus'), '');
  renderGroups();
}

renderGroups();

$('#addGroupBtn').addEventListener('click', addGroup);
$('#groupInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); addGroup(); }
});

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
      group: el.querySelector('.bucket-group-select').value,
    }));

  if (!buckets.length) {
    return setStatus($('#configStatus'), 'Pick at least one destination playlist.', 'bad');
  }

  // A one-playlist group sends every song to that playlist — a filter, not a
  // choice. The server rejects it too; catching it here explains it sooner.
  for (const g of state.groups) {
    const members = buckets.filter((b) => b.group === g);
    if (members.length === 1) {
      return setStatus($('#configStatus'),
        `Group "${g}" has only one playlist ticked, so every song would be sent ` +
        `there. Add another playlist to the group, or remove the group.`, 'bad');
    }
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
    // A fresh job re-locks the downstream screens until it produces results.
    state.reviewReady = false;
    state.applied = false;
    state.summary = null;
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
    ...(data.groups?.length ? [stat(s.incomplete, 'missing a group')] : []),
    stat(s.low_confidence, 'low confidence'),
    ...Object.entries(s.providers || {}).map(([k, v]) => stat(v, `${k} calls`)),
  ].join('');

  const allNames = data.buckets.map((b) => b.name);
  const sections = [];
  let lastGroup = null;

  for (const bucket of data.buckets) {
    // A heading each time the axis changes, so the two questions stay visibly
    // separate instead of reading as one long list of playlists.
    if (data.groups?.length && bucket.group !== lastGroup) {
      lastGroup = bucket.group;
      sections.push(`<h3 class="axis">${bucket.group
        ? escape(bucket.group)
        : 'Not in a group'}</h3>`);
    }
    const rows = state.lowOnly
      ? bucket.songs.filter((r) => r.low_confidence)
      : bucket.songs;
    sections.push(group(bucket.name, bucket.description, rows, allNames));
  }

  // Only grouped sorts get this divider; without groups the leftovers section
  // reads fine on its own, as it always has.
  if (data.groups?.length && (data.incomplete?.length || data.unmatched.length)) {
    sections.push(`<h3 class="axis">Needs attention</h3>`);
  }

  if (data.incomplete?.length) {
    sections.push(group(
      'Missing a group',
      'These landed somewhere, but came up empty on a group they were promised ' +
      'to. Add the missing playlist by hand.',
      state.lowOnly ? data.incomplete.filter((r) => r.low_confidence) : data.incomplete,
      allNames,
    ));
  }

  if (data.unmatched.length) {
    sections.push(group(
      'Matched nothing',
      'These did not clearly fit any playlist. Assign them by hand or leave them out.',
      state.lowOnly ? data.unmatched.filter((r) => r.low_confidence) : data.unmatched,
      allNames,
    ));
  }

  $('#reviewBody').innerHTML = sections.join('');
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
      <td class="reason">${escape(r.reason)}
        <span class="gap" data-video="${r.video_id}">${gapLabel(r.missing_groups)}</span>
      </td>
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

function gapLabel(groups) {
  return groups?.length
    ? `nothing from ${groups.map(escape).join(' or ')}`
    : '';
}

/** Which groups this set of playlists leaves empty — recomputed after an edit
 *  so the badge can never contradict the chips sitting next to it. */
function missingGroupsFor(playlists) {
  const data = state.review;
  if (!data?.groups?.length) return [];
  return data.groups.filter((g) =>
    !data.buckets.some((b) => b.group === g && playlists.includes(b.name)));
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
        const gaps = gapLabel(missingGroupsFor(playlists));
        $$(`.gap[data-video="${videoId}"]`).forEach((el) => { el.innerHTML = gaps; });
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

        // Even a partial or failed apply wrote something worth accounting for.
        state.applied = true;
        updateSteps();
        await loadSummary();
        if (job.phase === 'done') show('summary');
      }
    }, 1500);
  } catch (e) {
    setStatus($('#applyStatus'), e.message, 'bad');
    $('#applyBtn').disabled = false;
  }
});

// ---------------------------------------------------------------------------
// 5. summary
// ---------------------------------------------------------------------------

$('#refreshSummary').addEventListener('click', loadSummary);

async function loadSummary() {
  if (!state.jobId) return;
  $('#summaryBody').innerHTML = '<div class="empty">Loading…</div>';
  try {
    state.summary = await api(`/api/jobs/${state.jobId}/summary`);
  } catch (e) {
    return setStatus($('#summaryStatus'), e.message, 'bad');
  }
  renderSummary();
}

function renderSummary() {
  const data = state.summary;
  if (!data) return;
  const s = data.stats;

  $('#summaryStats').innerHTML = [
    stat(s.added, 'songs added'),
    stat(s.playlists, 'playlists'),
    stat(s.skipped, 'already there'),
    stat(s.failed, 'failed'),
    stat(s.unassigned, 'matched nothing'),
    ...(data.groups?.length ? [stat(s.incomplete, 'missing a group')] : []),
  ].join('');

  let lastGroup = null;
  const groups = data.buckets.map((b) => {
    let head = '';
    if (data.groups?.length && b.group !== lastGroup) {
      lastGroup = b.group;
      head = `<h3 class="axis">${b.group ? escape(b.group) : 'Not in a group'}</h3>`;
    }

    if (!b.added) {
      return `${head}<div class="bucket-group">
        <h4>${escape(b.name)} <span class="n">nothing added</span></h4>
        <div class="empty">${b.skipped
          ? `All ${b.skipped} matching songs were already in this playlist.`
          : 'No songs were routed here.'}</div>
      </div>`;
    }

    const rows = b.songs.map((r) => `
      <tr>
        <td>
          <div class="song-title">${escape(r.title)}</div>
          <div class="song-artist">${escape(r.artists)}</div>
        </td>
        <td class="conf">${r.confidence != null ? r.confidence.toFixed(2) : '—'}</td>
        <td class="reason">${escape(r.reason)}${
          r.overridden ? '<span class="by-hand">changed by hand</span>' : ''}</td>
      </tr>`).join('');

    return `${head}
      <div class="bucket-group">
        <h4>${escape(b.name)}
          <span class="n">${b.added} added</span>
          ${b.skipped ? `<span class="n">${b.skipped} already there</span>` : ''}
        </h4>
        ${b.description ? `<div class="hint">${escape(b.description)}</div>` : ''}
        <table>
          <thead><tr><th>Song</th><th>Conf</th><th>Why</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  });

  $('#summaryBody').innerHTML = groups.join('') ||
    '<div class="empty">Nothing was written.</div>';

  $('#summaryBackup').textContent = data.backup_path
    ? `Backup of every destination, taken before writing: ${data.backup_path}`
    : '';
}

/** Plain-text receipt. Generated in the browser so it works offline. */
function summaryMarkdown(data) {
  const s = data.stats;
  const out = [
    `# Playlist Sorter — ${data.source_name || 'sort'}`,
    '',
    `- Songs added: ${s.added}`,
    `- Playlists written to: ${s.playlists}`,
    `- Already present, skipped: ${s.skipped}`,
    `- Failed writes: ${s.failed}`,
    `- Matched no playlist: ${s.unassigned}`,
    ...(data.groups?.length ? [`- Missing a group: ${s.incomplete}`] : []),
    `- Songs classified: ${s.classified}`,
  ];
  if (data.backup_path) out.push(`- Backup: ${data.backup_path}`);
  out.push('');

  let lastGroup = null;
  for (const b of data.buckets) {
    if (data.groups?.length && b.group !== lastGroup) {
      lastGroup = b.group;
      out.push(`# ${b.group || 'Not in a group'}`, '');
    }
    out.push(`## ${b.name} — ${b.added} added${b.skipped ? `, ${b.skipped} already there` : ''}`);
    if (b.description) out.push(`_${b.description}_`);
    out.push('');
    if (!b.songs.length) {
      out.push('_Nothing added._', '');
      continue;
    }
    for (const r of b.songs) {
      const conf = r.confidence != null ? r.confidence.toFixed(2) : '—';
      const hand = r.overridden ? ' [changed by hand]' : '';
      out.push(`- **${r.title}** — ${r.artists} (${conf})${hand}${r.reason ? ` · ${r.reason}` : ''}`);
    }
    out.push('');
  }
  return out.join('\n');
}

$('#downloadSummary').addEventListener('click', () => {
  if (!state.summary) return setStatus($('#summaryStatus'), 'Nothing to download yet.', 'bad');

  const blob = new Blob([summaryMarkdown(state.summary)], {
    type: 'text/markdown;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `playlist-sorter-${state.jobId}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setStatus($('#summaryStatus'), 'Downloaded.', 'ok');
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
