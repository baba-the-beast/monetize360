// Overview, Limits, API keys and Price history.
import {
  api, session, h, icon, replace, badge, note, empty, errorBox, run, toast, dialog, confirm, copy,
  money, when, whenFull, count, duration, modeBadge, took, tookText,
} from './core.js';
import { waterfall } from './packs.js';

const enc = encodeURIComponent;
const reload = () => window.dispatchEvent(new Event('m360:reload'));

// ---------- Overview ----------

const tile = (glyph, label, value, text) => h('div.panel.tile', null,
  h('div.label', null, icon(glyph), label), h('div.value', null, value), h('div.note', null, text));

const STATE = {
  ok: ['Connected', 'ok', 'check'], down: ['Unreachable', 'danger', 'x'], not_configured: ['Not used', '', ''],
};

function limitRows(p) {
  return [
    h('dt', null, 'Lowest price'), h('dd', null, p.min_price ? money(p.min_price) : h('span.muted', null, 'No limit')),
    h('dt', null, 'Highest price'), h('dd', null, p.max_price ? money(p.max_price) : h('span.muted', null, 'No limit')),
    h('dt', null, 'Highest surge'), h('dd', null, p.max_surge_x ? `${p.max_surge_x} × the base price` : h('span.muted', null, 'No limit')),
    h('dt', null, 'Banned signals'), h('dd', null, p.banned_signals.length ? p.banned_signals.join(', ') : h('span.muted', null, 'None')),
  ];
}

export async function overview() {
  const [packs, policy, board, ready] = await Promise.all([
    api('GET', '/v1/packs'), api('GET', '/v1/admin/policy'), api('GET', '/v1/scoreboard'),
    api('GET', '/readyz', undefined, { anyStatus: true }),
  ]);
  const modes = Object.fromEntries(policy.kill_switches.map((k) => [k.domain, k.mode]));
  const live = packs.domains.filter((d) => d.live_version);
  const paused = live.filter((d) => (modes[d.domain] || 'normal') !== 'normal');
  const since = `this server, last ${duration(board.live.uptime_seconds)}`;

  return [
    h('div.page-head', null, h('div', null, h('h1', null, 'Overview'),
      h('p', null, `Pricing for ${session.me.tenant_id} at a glance.`))),
    h('div.tiles', null,
      tile('layers', 'Domains live', `${live.length} of ${packs.domains.length}`, 'with published rules'),
      tile('zap', 'Engine', paused.length ? `${paused.length} paused` : 'On',
        paused.length ? paused.map((d) => d.domain).join(', ') : 'dynamic pricing on every live domain'),
      tile('tag', 'Prices issued', count(board.live.prices.ok), since),
      tile('circle-x', 'Requests refused', count(board.live.prices.rejected), since)),
    h('div.grid-2', null,
      h('section.panel', null,
        h('div.panel-head', null, h('h2', null, 'Domains'), h('a.btn.sm', { href: '#/domains' }, 'Manage', icon('chevron-right'))),
        packs.domains.length === 0 ? empty('layers', 'No domains yet', 'Upload a rules file on the Domains page.')
          : h('div.rows', null, packs.domains.map((d) => h('div.row', null,
            h('div.row-main', null, h('a', { href: `#/domains/${enc(d.domain)}` }, d.domain)),
            d.live_version ? badge(`Live v${d.live_version}`, 'ok', 'radio-tower') : badge('Not live'),
            d.live_version ? modeBadge(modes[d.domain] || 'normal') : h('span'))))),
      h('div.stack', null,
        h('section.panel', null,
          h('div.panel-head', null, h('h2', null, 'Limits'), h('a.btn.sm', { href: '#/limits' }, 'Edit', icon('chevron-right'))),
          h('div.panel-body', null, h('dl.kv', null, limitRows(policy)))),
        h('section.panel', null,
          h('div.panel-head', null, h('h2', null, 'System')),
          h('div.panel-body', null, h('dl.kv', null,
            h('dt', null, 'Database'), h('dd', null, badge(...(STATE[ready.database] || [ready.database, '', '']))),
            h('dt', null, 'Broadcast channel'), h('dd', null, badge(...(STATE[ready.redis] || [ready.redis, '', '']))),
            h('dt', null, 'Engine self-test'), h('dd', null, ready.self_test ? badge('Passed', 'ok', 'check') : badge('Failed', 'danger', 'x')),
            h('dt', null, 'Engine time'), h('dd', null, board.live.engine_ms_at_most.p95 === null ? h('span.muted', null, 'No prices yet')
              : `${board.live.engine_ms_at_most.p95} ms or less for 95% of prices`)))))),
  ];
}

// ---------- Limits ----------

export async function limits() {
  const p = await api('GET', '/v1/admin/policy');
  const field = (value, placeholder) => h('input', { type: 'text', inputMode: 'decimal', value: value || '', placeholder });
  const min = field(p.min_price, 'No limit');
  const max = field(p.max_price, 'No limit');
  const surge = field(p.max_surge_x, 'No limit');
  const banned = h('input', { type: 'text', value: p.banned_signals.join(', '), placeholder: 'customer_gender, customer_age', spellcheck: false });
  const result = h('div');
  const changed = h('p.small.muted', null, p.updated_at ? `Last changed ${whenFull(p.updated_at)} by key ${p.updated_by}.` : 'Never changed.');
  const save = h('button.btn.primary', { type: 'submit' }, icon('check'), 'Save limits');

  const form = h('form.form', { onsubmit: (e) => {
    e.preventDefault();
    run(save, async () => {
      const body = { banned_signals: banned.value.split(/[\s,]+/).filter(Boolean) };
      if (min.value.trim()) body.min_price = min.value.trim();
      if (max.value.trim()) body.max_price = max.value.trim();
      if (surge.value.trim()) body.max_surge_x = surge.value.trim();
      try {
        const saved = await api('PUT', '/v1/admin/policy', body);
        replace(result, note('ok', 'circle-check', 'Saved. Every price in every domain now obeys these limits.'));
        replace(changed, `Last changed ${whenFull(saved.updated_at)} by key ${saved.updated_by}.`);
        toast(`Limits saved · ${tookText(saved)}`);
      } catch (err) {
        replace(result, errorBox(err, err.code === 'policy_conflict' ? 'These limits clash with rules that are live' : 'Not saved'));
      }
    });
  } },
  h('div.form-row', null,
    h('label.field', null, h('span', null, 'Lowest price'), min, h('small', null, 'No price in any domain goes below this.')),
    h('label.field', null, h('span', null, 'Highest price'), max, h('small', null, 'No price in any domain goes above this.')),
    h('label.field', null, h('span', null, 'Highest surge'), surge, h('small', null, 'Times the base price. 2 means never more than double.'))),
  h('label.field', null, h('span', null, 'Banned signals'), banned,
    h('small', null, 'Names of inputs that no rule may use, separated by commas. Rules that use one cannot be approved.')),
  result, h('div.actions', null, save, changed));

  return [
    h('div.page-head', null, h('div', null, h('h1', null, 'Limits'),
      h('p', null, 'Hard limits for your whole organisation. They sit on top of every domain’s own rules, and the strictest limit always wins.'))),
    h('section.panel', null, h('div.panel-body', null, form)),
    note('', 'lock', h('span', null, h('strong', null, 'Two-person approval is always on. '),
      'Whoever writes a set of rules cannot approve it. This cannot be switched off.')),
  ];
}

// ---------- API keys ----------

const ROLES = {
  pricing: 'Ask for prices. Give this to your app or website.',
  editor: 'Write and test rules. Cannot make them live.',
  approver: 'Approve and publish rules written by someone else.',
  admin: 'Everything, including this console.',
};
const KEY_TONE = { active: 'ok', expired: 'warn', revoked: '' };

function createKey(domainNames) {
  return dialog('Create an API key', (close) => {
    const name = h('input', { type: 'text', maxLength: 80, placeholder: 'Checkout server' });
    const role = h('select', null, Object.keys(ROLES).map((r) => h('option', { value: r }, r)));
    const roleHelp = h('small', null, ROLES.pricing);
    role.addEventListener('change', () => replace(roleHelp, ROLES[role.value]));
    const all = h('input', { type: 'checkbox', checked: true });
    const boxes = domainNames.map((d) => [d, h('input', { type: 'checkbox', disabled: true })]);
    all.addEventListener('change', () => boxes.forEach(([, b]) => { b.disabled = all.checked; if (all.checked) b.checked = false; }));
    const rate = h('input', { type: 'number', min: 1, max: 10000, value: 200 });
    const expires = h('input', { type: 'date' });
    const problem = h('div');
    const create = h('button.btn.primary', { type: 'button', onclick: () => run(create, async () => {
      const chosen = all.checked ? ['*'] : boxes.filter(([, b]) => b.checked).map(([d]) => d);
      if (chosen.length === 0) { replace(problem, errorBox({ message: 'Choose at least one domain.' }, 'No domain')); return; }
      const body = { role: role.value, domains: chosen, rate_limit_rps: Number(rate.value) || 200 };
      if (name.value.trim()) body.name = name.value.trim();
      if (expires.value) body.expires_at = `${expires.value}T23:59:59Z`;
      try { close(await api('POST', '/v1/admin/keys', body)); } catch (err) { replace(problem, errorBox(err, 'Key not created')); }
    }) }, icon('key-round'), 'Create key');
    return {
      body: [
        h('label.field', null, h('span', null, 'Name'), name, h('small', null, 'What or who will use it.')),
        h('label.field', null, h('span', null, 'Role'), role, roleHelp),
        h('div.field', null, h('span.small', null, h('strong', null, 'Domains')),
          h('label.check', null, all, 'All domains, including future ones'),
          boxes.map(([d, b]) => h('label.check', null, b, d))),
        h('div.form-row', null,
          h('label.field', null, h('span', null, 'Requests per second'), rate),
          h('label.field', null, h('span', null, 'Expires (optional)'), expires)),
        problem],
      foot: [h('button.btn', { type: 'button', onclick: () => close() }, 'Cancel'), create],
    };
  });
}

const showKey = (made) => dialog('Copy the new key now', (close) => ({
  body: [
    note('warn', 'triangle-alert', h('span', null, h('strong', null, 'This is the only time the full key is shown. '),
      'Only a fingerprint is stored, so it cannot be looked up later. If it is lost, revoke it and create another.')),
    h('div.secret', null, h('code', null, made.api_key),
      h('button.btn.sm', { type: 'button', onclick: () => copy(made.api_key) }, icon('copy'), 'Copy'))],
  foot: h('button.btn.primary', { type: 'button', onclick: () => close() }, 'I have saved it'),
}));

export async function keys() {
  const [list, packs] = await Promise.all([api('GET', '/v1/admin/keys'), api('GET', '/v1/packs')]);
  const add = h('button.btn.primary', { type: 'button', onclick: async () => {
    const made = await createKey(packs.domains.map((d) => d.domain));
    if (made) { await showKey(made); reload(); }
  } }, icon('plus'), 'Create key');

  const revoke = (k) => async (e) => {
    const button = e.currentTarget;
    const own = k.key_id === session.me.key_id;
    const yes = await confirm({ title: `Revoke ${k.name || k.key_id}?`, action: 'Revoke key', danger: true, tone: 'danger', iconName: 'ban',
      text: own ? 'This is the key you are signed in with. Revoking it signs you out, and it can never be used again.'
        : 'Anything using this key is refused at once, on every server. This cannot be undone.' });
    if (yes) await run(button, async () => {
      const gone = await api('POST', `/v1/admin/keys/${enc(k.key_id)}/revoke`);
      toast(`Key revoked · ${tookText(gone)}`);
      reload();
    });
  };

  return [
    h('div.page-head', null,
      h('div', null, h('h1', null, 'API keys'),
        h('p', null, 'A key is how your app, or a person, talks to the engine. Each key belongs to your organisation only and can be limited to a role and to certain domains.')),
      add),
    h('section.panel', null, h('div.table-wrap', null, h('table', null,
      h('thead', null, h('tr', null, ['Name', 'Key ID', 'Role', 'Domains', 'Requests / s', 'Status', 'Created', ''].map((t, i) => h('th', { class: i === 4 ? 'right' : '' }, t)))),
      h('tbody', null, list.keys.map((k) => h('tr', null,
        h('td', null, k.name || h('span.muted', null, 'Unnamed'), k.key_id === session.me.key_id && [' ', badge('This session', 'info')]),
        h('td.mono', null, k.key_id),
        h('td', null, k.role),
        h('td', null, k.domains.includes('*') ? 'All' : k.domains.join(', ')),
        h('td.right.num', null, count(k.rate_limit_rps)),
        h('td', null, badge(k.status[0].toUpperCase() + k.status.slice(1), KEY_TONE[k.status])),
        h('td.nowrap.muted', null, when(k.created_at)),
        h('td.right', null, k.status === 'active'
          && h('button.btn.sm.danger', { type: 'button', onclick: revoke(k) }, icon('ban'), 'Revoke')))))))),
  ];
}

// ---------- Price history ----------

function record(priceId, currency) {
  return dialog('Price record', (close) => {
    const body = h('div.form', null, h('div.loading', null, icon('loader-circle', 'spin'), 'Loading'));
    const replayResult = h('div');
    const replay = h('button.btn', { type: 'button', onclick: () => run(replay, async () => {
      const r = await api('POST', '/v1/replay', { price_id: priceId });
      // a frozen price was copied from an earlier one, so there is nothing to recalculate
      replace(replayResult, r.identical === null ? note('info', 'snowflake', h('span', null,
        h('strong', null, 'Nothing to recalculate. '), 'This price was given while the domain was frozen: '
          + 'the item’s previous price was repeated', r.frozen_from ? ` (record ${r.frozen_from}).` : '.'))
        : r.identical ? note('ok', 'circle-check', `Recalculated from the record with rules v${r.replayed_pack_version}: the same price and the same steps.`, took(r))
        : note('danger', 'circle-x', h('strong', null, 'The recalculated price is different'),
          h('ul.problems', null, r.diff.map((d) => h('li', null, h('code', null, `${d.field} `), `${JSON.stringify(d.original)} → ${JSON.stringify(d.replayed)}`)))));
    }) }, icon('rotate-ccw'), 'Recalculate');

    api('GET', `/v1/audit/${enc(priceId)}`).then((r) => {
      const used = Object.entries(r.signals_used || {});
      replace(body,
        h('div', null, h('div.small.muted', null, r.entity || 'Price'), h('div.price-big', null, money(r.output.price, r.output.currency || currency))),
        (r.output.flags || []).length > 0 && note('warn', 'triangle-alert', h('ul.problems', null, r.output.flags.map((f) => h('li', null, f)))),
        waterfall(r.output.waterfall, r.output.currency || currency),
        h('dl.kv', null,
          h('dt', null, 'Priced for'), h('dd', null, whenFull(r.as_of)),
          h('dt', null, 'Recorded'), h('dd', null, whenFull(r.recorded_at)),
          h('dt', null, 'Rules'), h('dd', null, `v${r.pack_version}`, r.pack_owner && ` · written by ${r.pack_owner}`, r.pack_approver && ` · approved by ${r.pack_approver}`),
          h('dt', null, 'Asked by'), h('dd.mono', null, r.key_id ? `key ${r.key_id}` : '—'),
          used.length > 0 && [h('dt', null, 'Inputs used'), h('dd', null, used.map(([n, s]) => `${n} = ${s.value} (${s.source})`).join(' · '))],
          h('dt', null, 'Price ID'), h('dd.mono', null, r.price_id),
          h('dt', null, `Record #${r.seq}`), h('dd.mono', null, r.hash)),
        replayResult);
    }).catch((err) => replace(body, errorBox(err, 'Record not found')));

    return { body, foot: [replay, h('button.btn.primary', { type: 'button', onclick: () => close() }, 'Done')] };
  }, { wide: true });
}

export async function history(parts) {
  const packs = await api('GET', '/v1/packs');
  const names = packs.domains.map((d) => d.domain);
  const head = h('div', null, h('h1', null, 'Price history'),
    h('p', null, 'Every price the engine gives is recorded before it is returned, and each record is sealed to the one before it, so a changed or missing record is detected.'));
  if (names.length === 0) return [h('div.page-head', null, head), h('section.panel', null, empty('history', 'No prices yet', 'Publish a domain first.'))];

  const name = names.includes(parts[0]) ? parts[0] : names[0];
  const data = await api('GET', `/v1/audit?domain=${enc(name)}&limit=50`);
  const picker = h('select', { 'aria-label': 'Domain', onchange: (e) => { location.hash = `#/history/${enc(e.target.value)}`; } },
    names.map((n) => h('option', { value: n, selected: n === name }, n)));
  const verdict = h('div');

  const verify = h('button.btn', { type: 'button', onclick: () => run(verify, async () => {
    const v = await api('POST', '/v1/audit/verify', { domain: name });
    replace(verdict, v.ok
      ? note('ok', 'shield-check', h('span', null, h('strong', null, `All ${count(v.rows_checked)} records are intact. `), 'None was changed, removed or added after the fact.'), took(v))
      : note('danger', 'triangle-alert', h('strong', null, `The records were tampered with at record #${v.first_broken_seq}`), h('span', null, v.reason)));
  }) }, icon('shield-check'), 'Check records');

  const exportCsv = h('button.btn', { type: 'button', onclick: () => run(exportCsv, async () => {
    const res = await api('GET', `/v1/audit/export?domain=${enc(name)}&format=csv`, undefined, { raw: true });
    const url = URL.createObjectURL(await res.blob());
    const a = h('a', { href: url, download: `audit-${name}.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }) }, icon('download'), 'Export CSV');

  return [
    h('div.page-head', null, head, h('div.actions', null, h('div', null, picker), verify, exportCsv)),
    verdict,
    h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, `Latest ${name} prices`), h('p', null, data.rows.length === 50 ? 'The newest 50. Export for the rest.' : `${data.rows.length} recorded.`))),
      data.rows.length === 0 ? empty('receipt-text', 'No prices recorded yet', `Nothing has asked for a ${name} price so far.`)
        : h('div.table-wrap', null, h('table', null,
          h('thead', null, h('tr', null, h('th', null, 'Priced for'), h('th', null, 'Item'), h('th.right', null, 'Base price'),
            h('th.right', null, 'Price'), h('th', null, 'Rules'), h('th.right', null, 'Record'))),
          h('tbody', null, data.rows.map((r) => h('tr.link', { tabIndex: 0, onclick: () => record(r.price_id, r.currency),
            onkeydown: (e) => { if (e.key === 'Enter') record(r.price_id, r.currency); } },
            h('td.nowrap', null, when(r.as_of)),
            h('td', null, r.entity || '—'),
            h('td.right.num.muted', null, r.base_price ? money(r.base_price, r.currency) : '—'),
            h('td.right.num', null, h('strong', null, money(r.price, r.currency))),
            h('td', null, `v${r.pack_version}`),
            h('td.right.mono.muted', null, `#${r.seq}`))))))),
  ];
}
