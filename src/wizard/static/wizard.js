// Rosa Setup Wizard — vanilla-JS SPA driver.
//
// State comes from GET /api/status (which reads the persistent
// .wizard_state.json). Each step posts to /api/step/<id> which validates,
// persists to config.yaml + secrets.env, and returns { ok: true }.
//
// The list below decides visual order + which template renders each step.
// STUB entries render the generic "coming soon, skip for now" card so the
// wizard is fully walkable in MVP without every integration wired up yet.

const TOKEN = document.querySelector(
  'meta[name="rosa-wizard-token"]',
).getAttribute('content');

const STEPS = [
  { id: 'welcome',      title: 'Welcome',         tpl: 'tpl-welcome',   required: true  },
  { id: 'identity',     title: 'Identity',        tpl: 'tpl-identity',  required: true  },
  { id: 'claude',       title: 'Anthropic',       tpl: 'tpl-claude',    required: true  },
  { id: 'imessage',     title: 'iMessage',        tpl: 'tpl-imessage',  required: false },
  { id: 'google',       title: 'Google',          tpl: 'tpl-google',    required: false },
  { id: 'imap',         title: 'IMAP',            tpl: 'tpl-token-step', required: false,
    label: 'IMAP config (host, user, password)',
    token_help: 'Line format: <code>label host user password [port]</code>. One per line. Password stored to secrets.env.',
    token_hint: 'e.g. "personal imap.fastmail.com you@example.com app-password 993"',
    endpoint: '/api/step/imap',
    body: 'Add non-Gmail mailboxes (Outlook, Fastmail, custom IMAP).' },
  { id: 'slack',        title: 'Slack',           tpl: 'tpl-slack',    required: false },
  { id: 'todoist',      title: 'Todoist',         tpl: 'tpl-token-step', required: false,
    label: 'Todoist API token',
    token_help: 'Grab your token at <a href="https://todoist.com/prefs/integrations" target="_blank">todoist.com/prefs/integrations</a> → Developer → API token.',
    token_hint: '40-character hex string. Stored in secrets.env (0600).',
    endpoint: '/api/step/todoist',
    body: 'Sync tasks so Rosa can nudge you about overdue items.' },
  { id: 'plaud',        title: 'Plaud',           tpl: 'tpl-plaud',     required: false },
  { id: 'vips',         title: 'VIP contacts',    tpl: 'tpl-list-step', required: false,
    label: 'VIP names or emails (one per line)',
    endpoint: '/api/step/vips',
    placeholder: 'Jane Smith\njane@bigcustomer.com\n# comments allowed',
    body: 'People whose messages Rosa must never let slip.' },
  { id: 'uptime',       title: 'Uptime',          tpl: 'tpl-list-step', required: false,
    label: 'URLs to monitor (one per line)',
    endpoint: '/api/step/uptime',
    placeholder: 'https://your-site.com\nhttps://api.your-site.com/health',
    body: 'Rosa checks these every 5 minutes and pings you when they go down.' },
  { id: 'news',         title: 'News feeds',      tpl: 'tpl-list-step', required: false,
    label: 'RSS feed URLs (one per line)',
    endpoint: '/api/step/news',
    placeholder: 'https://news.ycombinator.com/rss\nhttps://blog.company.com/feed',
    body: 'Feeds Rosa scans for items worth surfacing in your morning briefing.' },
  { id: 'notifications',title: 'Notifications',   tpl: 'tpl-notifications', required: false },
  { id: 'confidential', title: 'Confidential',    tpl: 'tpl-list-step', required: false,
    label: 'Email domains (bare, one per line)',
    endpoint: '/api/step/confidential',
    placeholder: 'legal-firm.com\ntherapist.nl\naccountant.com',
    body: 'Mail from/to these domains stays on your Mac (routed to your local model, never to Claude).' },
  { id: 'features',     title: 'Features',        tpl: 'tpl-features',  required: false },
  { id: 'main_channel', title: 'Main channel',    tpl: 'tpl-main-channel', required: false },
  { id: 'confirm',      title: 'Confirm',         tpl: 'tpl-confirm',   required: true  },
];

// ---------------------------------------------------------- helpers ---

async function api(path, body) {
  const opts = {
    method: body ? 'POST' : 'GET',
    headers: { 'X-Wizard-Token': TOKEN },
  };
  if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    // M5: session-token vervalt bij wizard-restart. Toon een sticky
    // error met reload-hint zodat de user niet zonder feedback vast zit.
    if (r.status === 401 || r.status === 403) {
      _showSessionExpired();
    }
    const err = new Error(data.detail || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return data;
}

function _showSessionExpired() {
  if (document.getElementById('session-expired-banner')) return;
  const banner = document.createElement('div');
  banner.id = 'session-expired-banner';
  banner.className = 'rosa-callout error';
  banner.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:9999;margin:0;border-radius:0;text-align:center';
  banner.innerHTML =
    'The wizard restarted — this tab is stale. ' +
    '<a href="/" style="color:inherit;text-decoration:underline;">Reload</a> to continue.';
  document.body.appendChild(banner);
}

function templateNode(id) {
  const tpl = document.getElementById(id);
  if (!tpl) throw new Error(`template ${id} not found`);
  return tpl.content.cloneNode(true);
}

function renderProgress(currentIdx, completed, skipped) {
  const bar = document.getElementById('progress');
  bar.innerHTML = '';
  STEPS.forEach((step, idx) => {
    const el = document.createElement('div');
    el.className = 'rosa-wizard-progress-step';
    if (completed.includes(step.id) || skipped.includes(step.id)) {
      el.classList.add('done');
    } else if (idx === currentIdx) {
      el.classList.add('active');
    }
    el.title = step.title;
    bar.appendChild(el);
  });
}

function showError(message) {
  const stage = document.getElementById('stage');
  const box = document.createElement('div');
  box.className = 'rosa-callout error';
  box.textContent = message;
  stage.querySelector('.rosa-card')?.appendChild(box);
  setTimeout(() => box.remove(), 4000);
}

// ------------------------------------------------------ step wiring ---

function wireWelcome(root, onDone) {
  const consent = root.querySelector('#consent');
  const btn = root.querySelector('[data-action="submit"]');
  consent.addEventListener('change', () => {
    btn.disabled = !consent.checked;
  });
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await api('/api/step/welcome', { consent: consent.checked });
      onDone();
    } catch (e) {
      showError(e.message);
      btn.disabled = false;
    }
  });
}

function wireIdentity(root, onDone) {
  const btn = root.querySelector('[data-action="submit"]');
  btn.addEventListener('click', async () => {
    const name = root.querySelector('#f-name').value.trim();
    if (!name) {
      showError('Name is required.');
      root.querySelector('#f-name').classList.add('error');
      return;
    }
    btn.disabled = true;
    try {
      await api('/api/step/identity', {
        name,
        email: root.querySelector('#f-email').value.trim(),
        timezone: root.querySelector('#f-timezone').value,
        preferred_language: root.querySelector('#f-language').value,
        home_city: root.querySelector('#f-city').value.trim(),
        home_country: root.querySelector('#f-country').value.trim().toUpperCase(),
        company: root.querySelector('#f-company').value.trim(),
      });
      onDone();
    } catch (e) {
      showError(e.message);
      btn.disabled = false;
    }
  });
}

function wireClaude(root, onDone) {
  const btn = root.querySelector('[data-action="submit"]');
  btn.addEventListener('click', async () => {
    const key = root.querySelector('#f-key').value.trim();
    if (!key.startsWith('sk-ant-')) {
      showError('API key should start with "sk-ant-".');
      root.querySelector('#f-key').classList.add('error');
      return;
    }
    btn.disabled = true;
    try {
      await api('/api/step/claude', {
        anthropic_api_key: key,
        claude_model: root.querySelector('#f-model').value,
        local_model_main: root.querySelector('#f-local').value,
      });
      onDone();
    } catch (e) {
      showError(e.message);
      btn.disabled = false;
    }
  });
}

function wireImessage(root, onDone, onSkip) {
  const submit = root.querySelector('[data-action="submit"]');
  const skip = root.querySelector('[data-action="skip"]');
  submit.addEventListener('click', async () => {
    const primary = root.querySelector('#f-imsg').value.trim();
    if (!primary) {
      showError('Primary handle is required — or click "Skip for now".');
      return;
    }
    submit.disabled = true;
    try {
      await api('/api/step/imessage', {
        primary_handle: primary,
        extra_handles: root.querySelector('#f-imsg-extra').value.trim(),
      });
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  skip.addEventListener('click', () => onSkip('imessage'));
}

function wireGoogle(root, onDone, onSkip) {
  const submit = root.querySelector('[data-action="submit"]');
  const skip = root.querySelector('[data-action="skip"]');
  const redirectSlot = root.querySelector('#redirect-url');
  const expectedRedirect = `${window.location.origin}/oauth/google/callback`;
  redirectSlot.textContent = expectedRedirect;

  submit.addEventListener('click', async () => {
    const creds = root.querySelector('#f-google-creds').value.trim();
    if (!creds) {
      showError('Paste your credentials.json contents first.');
      return;
    }
    submit.disabled = true;
    try {
      const r = await api('/api/step/google/init', { credentials: creds });
      // Send user off to Google's consent screen.
      window.location.href = r.auth_url;
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  skip.addEventListener('click', () => onSkip('google'));
}

function wireTokenStep(root, step, onDone, onSkip) {
  root.querySelector('[data-slot="title"]').textContent = step.title;
  root.querySelector('[data-slot="body"]').textContent = step.body || '';
  root.querySelector('[data-slot="help"]').innerHTML = step.token_help || '';
  root.querySelector('[data-slot="label"]').textContent = step.label || 'Token';
  root.querySelector('[data-slot="token-hint"]').innerHTML =
    step.token_hint || '';

  const input = root.querySelector('[data-slot="token-input"]');
  // Multi-line for IMAP where user pastes several accounts.
  if (step.id === 'imap') {
    const ta = document.createElement('textarea');
    ta.className = 'rosa-textarea';
    ta.rows = 4;
    ta.placeholder = 'personal imap.fastmail.com you@example.com app-pw 993';
    ta.setAttribute('data-slot', 'token-input');
    input.replaceWith(ta);
  }

  const submit = root.querySelector('[data-action="submit"]');
  const skip = root.querySelector('[data-action="skip"]');

  submit.addEventListener('click', async () => {
    const el = root.querySelector('[data-slot="token-input"]');
    const value = el.value.trim();
    if (!value) {
      showError('Enter something or click "Skip for now".');
      return;
    }
    submit.disabled = true;
    try {
      await api(step.endpoint, { token: value });
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  skip.addEventListener('click', () => onSkip(step.id));
}

function wirePlaud(root, onDone, onSkip) {
  const submit = root.querySelector('[data-action="submit"]');
  const skip = root.querySelector('[data-action="skip"]');
  submit.addEventListener('click', async () => {
    const audio = root.querySelector('#f-plaud-audio').value.trim();
    if (!audio) {
      showError('Watched folder is required.');
      return;
    }
    submit.disabled = true;
    try {
      await api('/api/step/plaud', {
        audio_folder: audio,
        backup_folder: root.querySelector('#f-plaud-backup').value.trim(),
      });
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  skip.addEventListener('click', () => onSkip('plaud'));
}

function wireListStep(root, step, onDone, onSkip) {
  root.querySelector('[data-slot="title"]').textContent = step.title;
  root.querySelector('[data-slot="body"]').textContent = step.body || '';
  const help = root.querySelector('[data-slot="help"]');
  if (step.body) {
    help.remove();
  } else {
    help.textContent = '';
  }
  root.querySelector('[data-slot="label"]').textContent = step.label;
  const ta = root.querySelector('[data-slot="items"]');
  ta.placeholder = step.placeholder || '';

  const submit = root.querySelector('[data-action="submit"]');
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      await api(step.endpoint, { items: ta.value });
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  root.querySelector('[data-action="skip"]').addEventListener(
    'click', () => onSkip(step.id),
  );
}

function wireNotifications(root, onDone, onSkip) {
  const submit = root.querySelector('[data-action="submit"]');
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      await api('/api/step/notifications', {
        morning_time: root.querySelector('#f-morn').value,
        midday_time:  root.querySelector('#f-midday').value,
        dayclose_time: root.querySelector('#f-dayclose').value,
        quiet_start: root.querySelector('#f-qstart').value,
        quiet_end:   root.querySelector('#f-qend').value,
      });
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  root.querySelector('[data-action="skip"]').addEventListener(
    'click', () => onSkip('notifications'),
  );
}

// Moet 1-op-1 synchroon lopen met _ALLOWED_FEATURES in server.py
// (zie code-review L2). 21 feature-flags.
const _FEATURES = [
  { id: 'reminders',        label: 'Reminders',           on: true  },
  { id: 'comm_intel',       label: 'Communication intelligence', on: true  },
  { id: 'todoist_sync',     label: 'Todoist sync',        on: false },
  { id: 'slack_ingest',     label: 'Slack ingest',        on: false },
  { id: 'plaud_watcher',    label: 'Plaud recorder watcher', on: false },
  { id: 'voice_in',         label: 'Voice input (Whisper)', on: false },
  { id: 'uptime_monitor',   label: 'Uptime monitor',      on: false },
  { id: 'travel_alerts',    label: 'Travel alerts',       on: false },
  { id: 'sales',            label: 'Sales pipeline',      on: false },
  { id: 'market_intel',     label: 'Market-intel digest', on: false },
  { id: 'tenders',          label: 'Tender monitor (NL)', on: false },
  { id: 'insolvencies',     label: 'Insolvencies watch (NL)', on: false },
  { id: 'memory_cards',     label: 'Memory cards',        on: true  },
  { id: 'decisions_log',    label: 'Decisions log',       on: false },
  { id: 'patterns',         label: 'Pattern detection',   on: false },
  { id: 'weekly_retro',     label: 'Weekly retrospective (Sat)', on: true },
  { id: 'weekend_prep',     label: 'Weekend prep (Sun)',  on: true  },
  { id: 'ceo_letter',       label: 'CEO letter (Fri)',    on: false },
  { id: 'english_practice', label: 'English practice',    on: false },
  { id: 'okr_coaching',     label: 'OKR coaching',        on: false },
  { id: 'receipt_collector',label: 'Receipt collector',   on: false },
];

function wireSlack(root, onDone, onSkip) {
  const submit = root.querySelector('[data-action="submit"]');
  const skip = root.querySelector('[data-action="skip"]');
  submit.addEventListener('click', async () => {
    const body = {
      bot_token: root.querySelector('#f-slack-bot').value.trim(),
      app_token: root.querySelector('#f-slack-app').value.trim(),
      owner_user_id: root.querySelector('#f-slack-uid').value.trim(),
    };
    const userTok = root.querySelector('#f-slack-user').value.trim();
    if (userTok) body.token = userTok;
    if (!body.bot_token && !body.token) {
      showError('At least one token is required — bot token for bidirectional, user token for read-only ingest.');
      return;
    }
    submit.disabled = true;
    try {
      await api('/api/step/slack', body);
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  skip.addEventListener('click', () => onSkip('slack'));
}

function wireMainChannel(root, onDone, onSkip) {
  const submit = root.querySelector('[data-action="submit"]');
  submit.addEventListener('click', async () => {
    const choice = root.querySelector('input[name="main-channel"]:checked')?.value || 'imessage';
    submit.disabled = true;
    try {
      await api('/api/step/main_channel', { channel: choice });
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  root.querySelector('[data-action="skip"]').addEventListener(
    'click', () => onSkip('main_channel'),
  );
}

function wireFeatures(root, onDone, onSkip) {
  const list = root.querySelector('#features-list');
  list.innerHTML = _FEATURES.map(f => `
    <label class="rosa-checkbox" style="margin-bottom: 0.5rem;">
      <input type="checkbox" data-feat="${f.id}" ${f.on ? 'checked' : ''}>
      <span><strong>${f.label}</strong></span>
    </label>
  `).join('');

  const submit = root.querySelector('[data-action="submit"]');
  submit.addEventListener('click', async () => {
    const features = {};
    list.querySelectorAll('input[data-feat]').forEach(cb => {
      features[cb.getAttribute('data-feat')] = cb.checked;
    });
    submit.disabled = true;
    try {
      await api('/api/step/features', { features });
      onDone();
    } catch (e) {
      showError(e.message);
      submit.disabled = false;
    }
  });
  root.querySelector('[data-action="skip"]').addEventListener(
    'click', () => onSkip('features'),
  );
}

async function _runHealthChecks(root) {
  const list = root.querySelector('#health-list');
  list.innerHTML = '<em style="color: var(--rosa-muted);">Running checks…</em>';
  let result;
  try {
    result = await api('/api/health-check');
  } catch (e) {
    list.innerHTML =
      `<div class="rosa-callout error">Cannot run checks: ${e.message}</div>`;
    return;
  }
  list.innerHTML = '';
  const labels = {
    anthropic: 'Anthropic API',
    ollama: 'Ollama (local LLM)',
    google: 'Google OAuth',
    full_disk_access: 'iMessage read access',
    imessage_send: 'iMessage send capability',
  };
  for (const [id, r] of Object.entries(result.results)) {
    const row = document.createElement('div');
    row.className = 'rosa-callout ' + (r.ok ? 'info' : 'warn');
    row.style.cssText = 'padding: 0.5rem 0.75rem; margin: 0.25rem 0; font-size: 0.875rem;';
    const label = labels[id] || id;
    const icon = r.ok ? '✓' : '⚠';
    row.innerHTML =
      `<strong>${icon} ${label}:</strong> ${r.message}` +
      (r.details ? `<br><small style="color: var(--rosa-muted);">${r.details}</small>` : '');
    list.appendChild(row);
  }
  const summary = document.createElement('div');
  summary.style.cssText = 'font-size: 0.875rem; margin-top: 0.5rem;';
  summary.innerHTML = result.summary.all_ok
    ? `<strong style="color: var(--rosa-green);">All ${result.summary.total} checks passed.</strong>`
    : `<strong style="color: #d97706;">${result.summary.ok_count}/${result.summary.total} passed.</strong> You can still finish setup — some checks may fail because a service isn't running yet, and you can fix those later.`;
  list.appendChild(summary);
}

function wireStub(root, stepId, stubText, onSkip) {
  root.querySelector('[data-slot="title"]').textContent =
    STEPS.find(s => s.id === stepId)?.title ?? stepId;
  root.querySelector('[data-slot="body"]').textContent = stubText;
  root.querySelector('[data-action="skip"]').addEventListener(
    'click', () => onSkip(stepId),
  );
}

function wireConfirm(root, status, onDone) {
  const summary = root.querySelector('#confirm-summary');
  const done = status.completed.length;
  const skipped = status.skipped.length;
  const name = status.user_name || 'you';
  summary.textContent = '';
  const strong = document.createElement('strong');
  strong.textContent = 'Ready to start.';
  summary.appendChild(strong);
  summary.appendChild(document.createElement('br'));
  summary.append(
    ` ${done} step(s) configured, ${skipped} deferred. `,
    'Rosa will address you as ',
  );
  const em = document.createElement('em');
  em.textContent = name;
  summary.appendChild(em);
  summary.append('.');

  // Health-checks: ping alle services voordat user confirmt
  _runHealthChecks(root);
  root.querySelector('[data-action="rerun-health"]').addEventListener(
    'click', () => _runHealthChecks(root),
  );
  const btn = root.querySelector('[data-action="submit"]');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await api('/api/step/confirm', {});
      onDone();
    } catch (e) {
      showError(e.message);
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------- main flow ---

function nextStepIdx(status) {
  for (let i = 0; i < STEPS.length; i++) {
    const s = STEPS[i];
    if (status.completed.includes(s.id)) continue;
    if (status.skipped.includes(s.id)) continue;
    return i;
  }
  return STEPS.length - 1; // confirm
}

// M19d: edit-mode via ?mode=edit querystring. In edit-mode start je op
// een step-index gepiked uit ?step=<id>, en Save-buttons zeggen "Save"
// i.p.v. "Continue". Pre-fill is via /api/existing/<step>.
function _urlMode() {
  const p = new URLSearchParams(window.location.search);
  return {
    mode: p.get('mode') === 'edit' ? 'edit' : 'setup',
    step: p.get('step') || null,
  };
}

async function refresh() {
  const status = await api('/api/status');
  const { mode, step: forcedStep } = _urlMode();
  if (mode === 'edit') {
    // In edit-mode: laat step-picker zien of ga naar geforceerde step.
    if (forcedStep) {
      const idx = STEPS.findIndex(s => s.id === forcedStep);
      if (idx >= 0) {
        renderProgress(idx, status.completed, status.skipped);
        render(STEPS[idx].id, STEPS[idx], status);
        return;
      }
    }
    _renderStepPicker(status);
    return;
  }
  if (status.finished) {
    render('done', null, status);
    return;
  }
  const idx = nextStepIdx(status);
  const step = STEPS[idx];
  renderProgress(idx, status.completed, status.skipped);
  render(step.id, step, status);
}

function _renderStepPicker(status) {
  const stage = document.getElementById('stage');
  stage.innerHTML =
    '<article class="rosa-card">' +
    '<h2>Edit settings</h2>' +
    '<p class="lead">Pick a step to edit. Changes are saved to config.yaml immediately; if Rosa is running, send SIGHUP (rosa reload) to apply.</p>' +
    '<div id="step-list" style="display: grid; gap: 0.5rem;"></div>' +
    '</article>';
  const list = stage.querySelector('#step-list');
  STEPS.forEach(s => {
    if (s.id === 'welcome' || s.id === 'confirm') return;
    const a = document.createElement('a');
    a.href = `?mode=edit&step=${s.id}`;
    a.textContent = s.title;
    a.className = 'rosa-btn secondary';
    a.style.textAlign = 'left';
    list.appendChild(a);
  });
}

async function markSkip(stepId) {
  try {
    await api('/api/step/skip', { step: stepId });
  } catch (e) {
    showError(e.message);
    return;
  }
  await refresh();
}

function render(stepId, step, status) {
  const stage = document.getElementById('stage');
  stage.innerHTML = '';

  if (stepId === 'done') {
    stage.appendChild(templateNode('tpl-done'));
    return;
  }

  const node = templateNode(step.tpl);
  stage.appendChild(node);
  const root = stage; // template contents mount into #stage

  const onDone = () => refresh();
  const onSkip = (id) => markSkip(id);

  switch (step.id) {
    case 'welcome':   wireWelcome(root, onDone); break;
    case 'identity':  wireIdentity(root, onDone); break;
    case 'claude':    wireClaude(root, onDone); break;
    case 'imessage':  wireImessage(root, onDone, onSkip); break;
    case 'google':    wireGoogle(root, onDone, onSkip); break;
    case 'imap':
    case 'todoist':   wireTokenStep(root, step, onDone, onSkip); break;
    case 'slack':     wireSlack(root, onDone, onSkip); break;
    case 'plaud':     wirePlaud(root, onDone, onSkip); break;
    case 'vips':
    case 'uptime':
    case 'news':
    case 'confidential': wireListStep(root, step, onDone, onSkip); break;
    case 'notifications': wireNotifications(root, onDone, onSkip); break;
    case 'features':  wireFeatures(root, onDone, onSkip); break;
    case 'main_channel': wireMainChannel(root, onDone, onSkip); break;
    case 'confirm':   wireConfirm(root, status, onDone); break;
    default:
      wireStub(root, step.id, step.stub || '', onSkip);
  }
}

refresh().catch(e => {
  document.getElementById('stage').innerHTML =
    `<div class="rosa-callout error">Failed to load wizard: ${e.message}</div>`;
});
