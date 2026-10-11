// Domains and their rules: the list, one domain's versions, and one version's path to live.
import {
  api, session, h, icon, replace, badge, note, empty, errorBox, run, toast, dialog, confirm, copy,
  money, when, count, MODES, modeSwitch, took, tookText,
} from './core.js';

const enc = encodeURIComponent;
const reload = () => window.dispatchEvent(new Event('m360:reload'));
const go = (hash) => { if (location.hash === hash) reload(); else location.hash = hash; };
const DOMAIN_LINE = /^domain:\s*["']?([a-z][a-z0-9_]{1,39})["']?\s*(#.*)?$/m;
const MAX_YAML = 256 * 1024;

const STATUS = {
  published: ['Live', 'ok', 'radio-tower'],
  approved: ['Approved', 'info', 'user-check'],
  draft: ['Draft', '', 'pencil-line'],
  archived: ['Was live', '', 'history'],
};
const statusBadge = (status) => badge(...(STATUS[status] || [status, '', '']));

function download(name, text, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- upload / edit rules ----------

// Opens the editor; saves a new DRAFT version and goes to it. Nothing changes for customers here.
export function uploadDialog(text = '') {
  return dialog(text ? 'New version' : 'Upload rules', (close) => {
    const area = h('textarea', { rows: 14, spellcheck: false, value: text,
      placeholder: 'domain: hotel\ncurrency: INR\nsignals:\n  …' });
    const target = h('div');
    const problem = h('div');
    const describe = () => {
      const found = DOMAIN_LINE.exec(area.value);
      replace(target, found
        ? note('info', 'info', h('span', null, 'Saves a new draft for ', h('strong', null, found[1]),
          '. Customers keep getting today’s prices until the draft is checked, approved and published.'))
        : note('', 'info', 'The file must contain a line such as “domain: hotel”. That decides which domain it belongs to.'));
      return found && found[1];
    };
    const load = async (file) => {
      if (!file) return;
      if (file.size > MAX_YAML) { replace(problem, errorBox({ message: 'The file is larger than 256 KB.' }, 'File too large')); return; }
      area.value = await file.text();
      replace(problem);
      describe();
    };
    const picker = h('input', { type: 'file', accept: '.yaml,.yml,text/yaml', onchange: (e) => load(e.target.files[0]) });
    const drop = h('label.drop', {
      ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); },
      ondragleave: () => drop.classList.remove('over'),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove('over'); load(e.dataTransfer.files[0]); },
    }, icon('upload', 'lg'), h('span', null, h('strong', null, 'Choose a YAML file'), ' or drop it here'), picker);
    area.addEventListener('input', describe);
    describe();

    const save = h('button.btn.primary', { type: 'button', onclick: () => run(save, async () => {
      const name = describe();
      if (!name) { replace(problem, errorBox({ message: 'Add a “domain:” line first.' }, 'No domain')); return; }
      try {
        const made = await api('PUT', `/v1/packs/${enc(name)}`, undefined, { yaml: area.value });
        close(made);
        toast(`Draft v${made.version} saved · ${tookText(made)}`);
        go(`#/domains/${enc(made.domain)}/${made.version}`);
      } catch (err) {
        replace(problem, errorBox(err, 'The rules were not saved'));
      }
    }) }, icon('check'), 'Save as draft');

    return {
      body: [drop, h('label.field', null, h('span', null, 'Rules (YAML)'), area), target, problem],
      foot: [h('button.btn', { type: 'button', onclick: () => close() }, 'Cancel'), save],
    };
  }, { wide: true });
}

// ---------- Domains ----------

export async function domains() {
  const [packs, policy] = await Promise.all([api('GET', '/v1/packs'), api('GET', '/v1/admin/policy')]);
  const modes = Object.fromEntries(policy.kill_switches.map((k) => [k.domain, k.mode]));
  const upload = h('button.btn.primary', { type: 'button', onclick: () => uploadDialog() }, icon('upload'), 'Upload rules');

  const list = packs.domains.length === 0
    ? empty('layers', 'No domains yet', 'A domain is one thing you price, such as hotel rooms or parking. Upload its rules to start.')
    : h('div.rows', null, packs.domains.map((d) => h('div.row', null,
      h('div.row-main', null,
        h('a', { href: `#/domains/${enc(d.domain)}` }, d.domain),
        h('div.row-meta', null,
          d.live_version ? badge(`Live v${d.live_version}`, 'ok', 'radio-tower') : badge('Not live'),
          d.latest_version > (d.live_version || 0)
            && h('a', { href: `#/domains/${enc(d.domain)}/${d.latest_version}` }, `v${d.latest_version} waiting`),
          h('span', null, `${d.versions} version${d.versions === 1 ? '' : 's'}`))),
      modeSwitch(d.domain, modes[d.domain] || 'normal', Boolean(d.live_version)),
      h('a.btn.sm', { href: `#/domains/${enc(d.domain)}` }, 'Open', icon('chevron-right')))));

  return [
    h('div.page-head', null,
      h('div', null, h('h1', null, 'Domains'),
        h('p', null, 'Each domain has its own rules. Choose here whether the engine sets its prices, and upload new rules when they change.')),
      upload),
    h('section.panel', null, list),
    note('', 'info', h('span', null, h('strong', null, 'Dynamic'), ' follows the live rules. ',
      h('strong', null, 'Base price only'), ' switches the rules off. ',
      h('strong', null, 'Frozen'), ' repeats the last price each item was given. A switch reaches every server at once.')),
  ];
}

// ---------- one domain ----------

export async function domain(name) {
  const [info, policy] = await Promise.all([api('GET', `/v1/packs/${enc(name)}`), api('GET', '/v1/admin/policy')]);
  const mode = (policy.kill_switches.find((k) => k.domain === name) || {}).mode || 'normal';
  const modeNote = h('div');
  const explain = (m) => replace(modeNote, m !== 'normal' && note(MODES[m].tone, MODES[m].icon,
    h('span', null, h('strong', null, `${MODES[m].label}. `), MODES[m].explain)));
  explain(mode);

  const edit = h('button.btn.primary', { type: 'button', onclick: () => run(edit, async () => {
    const newest = await api('GET', `/v1/packs/${enc(name)}/${info.versions[0].version}`);
    uploadDialog(newest.yaml);
  }) }, icon('pencil-line'), 'New version');

  const rollback = (v) => async (e) => {
    e.stopPropagation();
    const button = e.currentTarget;
    const yes = await confirm({ title: `Go back to version ${v}?`, action: `Make v${v} live`, tone: 'warn', iconName: 'rotate-ccw',
      text: `Version ${v} becomes live again on every server. The current version is kept and can be made live again later.` });
    if (yes) await run(button, async () => {
      const back = await api('POST', `/v1/packs/${enc(name)}/rollback`, { to_version: v });
      toast(`Version ${v} is live · ${tookText(back)}`);
      reload();
    });
  };
  const checkMark = (ok, label) => (ok === true ? badge(label, 'ok', 'check') : ok === false ? badge(label, 'danger', 'x') : badge(label));

  return [
    h('div.page-head', null,
      h('div', null,
        h('a.crumb', { href: '#/domains' }, icon('arrow-left'), 'Domains'),
        h('div.title-row', null, h('h1', null, name),
          info.live_version ? badge(`Live v${info.live_version}`, 'ok', 'radio-tower') : badge('Not live'))),
      h('div.actions', null, modeSwitch(name, mode, Boolean(info.live_version), explain), edit)),
    modeNote,
    h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, 'Versions'),
        h('p', null, 'A saved version never changes. To change the rules, save a new version and publish it.'))),
      h('div.table-wrap', null, h('table', null,
        h('thead', null, h('tr', null, ['Version', 'Status', 'Checks', 'Written by', 'Approved by', 'Saved', ''].map((t) => h('th', null, t)))),
        h('tbody', null, info.versions.map((v) => h('tr.link', { onclick: () => go(`#/domains/${enc(name)}/${v.version}`) },
          h('td', null, h('strong', null, `v${v.version}`)),
          h('td', null, statusBadge(v.status)),
          h('td', null, h('div.actions', null, checkMark(v.lint_ok, 'Rules'), checkMark(v.verify_ok, 'Safety'))),
          h('td.mono', null, v.owner || '—'),
          h('td.mono', null, v.approver || '—'),
          h('td.nowrap.muted', null, when(v.created_at)),
          h('td.right', null, v.status === 'archived'
            ? h('button.btn.sm', { type: 'button', onclick: rollback(v.version) }, icon('rotate-ccw'), 'Roll back')
            : icon('chevron-right')))))))),
  ];
}

// ---------- reading a pack ----------

const compact = (o) => JSON.stringify(o).replace(/"/g, '').replace(/,/g, ', ').replace(/:/g, ': ');
const signed = (v) => (String(v).startsWith('-') ? String(v).replace('-', '−') : `+${v}`);

function effect(then) {
  if (!then || typeof then !== 'object') return '';
  const v = then.value;
  const simple = v !== undefined && typeof v !== 'object';
  if (then.op === 'pct' && simple) return `${signed(v)}%`;
  if (then.op === 'multiplier' && simple) return `× ${v}`;
  if (then.op === 'abs' && simple) return signed(v);
  if (then.op === 'set' && simple) return `= ${v}`;
  return String(then.op || '').replace(/_/g, ' ');
}

function limit(name, g) {
  if (!g || typeof g !== 'object') return String(g);
  if (name === 'max_change' && g.pct !== undefined) return `${g.pct}% per ${g.per}`;
  if (g.op === 'abs') return String(g.value);
  if (g.op === 'pct_of_base') return `${g.value}% of the base price`;
  if (g.op === 'signal') return `the “${g.signal}” signal`;
  return compact(g);
}

const SOURCE = { request: 'Sent with the request', public: 'From the calendar', redis: 'Live feed' };
const GUARD = { floor: 'Never below', ceiling: 'Never above', max_change: 'Biggest change' };

function rulesView(pack) {
  const signals = Object.entries(pack.signals || {});
  const guards = Object.entries(pack.guards || {});
  const base = pack.base || {};
  return [
    h('div.panel-body', null, h('dl.kv', null,
      pack.description && [h('dt', null, 'About'), h('dd', null, pack.description)],
      h('dt', null, 'Currency'), h('dd', null, pack.currency || '—'),
      h('dt', null, 'Starting price'), h('dd', null, base.op === 'from_request' ? 'The base price sent with each request'
        : [String(base.op || '').replace(/_/g, ' '), ' ', Object.keys(base).length > 1 && h('code.inline', null, compact({ ...base, op: undefined }))]),
      guards.map(([k, g]) => [h('dt', null, GUARD[k] || k.replace(/_/g, ' ')), h('dd', null, limit(k, g))]),
      pack.rounding && [h('dt', null, 'Rounding'), h('dd', null, compact(pack.rounding).slice(1, -1))])),
    signals.length > 0 && h('div.table-wrap', null, h('table', null,
      h('thead', null, h('tr', null, ['Signal', 'Kind', 'Where it comes from', 'If missing', 'Allowed'].map((t) => h('th', null, t)))),
      h('tbody', null, signals.map(([name, s]) => h('tr', null,
        h('td.mono', null, name), h('td', null, s.type), h('td', null, SOURCE[s.source] || s.source),
        h('td', null, s.default === undefined || s.default === null ? h('span.muted', null, s.source === 'public' ? '—' : 'Request is refused') : String(s.default)),
        h('td', null, s.values ? s.values.join(', ') : s.range ? `${s.range[0]} to ${s.range[1]}` : h('span.muted', null, 'Any'))))))),
    (pack.groups || []).map((g) => h('div.rule-group', null,
      h('header', null, h('h3', null, g.name), badge(`combine: ${String(g.stacking).replace(/_/g, ' ')}`)),
      (g.rules || []).map((r) => {
        const detail = r.then && Object.keys(r.then).some((k) => k !== 'op' && k !== 'value');
        return h('div.rule', null,
          h('span', null, r.reason || r.id), h('span.effect', null, effect(r.then)),
          h('span.when', null, 'When ', h('code.inline', null, r.when === undefined ? 'always' : String(r.when)),
            detail && [' · ', h('code.inline', null, compact(r.then))]));
      }))),
  ];
}

function diffView(text) {
  if (!text) return h('div.panel-body', null, note('', 'info', 'Nothing to compare: this is the live version, or no version is live yet.'));
  return h('pre.code', null, text.split('\n').map((line) => {
    const cls = line.startsWith('@@') ? 'hunk' : line.startsWith('+') && !line.startsWith('+++') ? 'add'
      : line.startsWith('-') && !line.startsWith('---') ? 'del' : '';
    return cls ? h('span', { class: cls }, line || ' ') : `${line}\n`;
  }));
}

// ---------- prices ----------

export function waterfall(rows, currency) {
  return h('div.table-wrap', null, h('table.waterfall', null,
    h('thead', null, h('tr', null, h('th', null, 'Step'), h('th', null, 'Why'), h('th.right', null, 'Change'), h('th.right', null, 'Price after'))),
    h('tbody', null, (rows || []).map((r) => {
      const zero = /^-?0+(\.0+)?$/.test(r.delta);
      const down = r.delta.startsWith('-');
      return h('tr', null,
        h('td.mono', null, r.step), h('td', null, r.reason),
        h('td.right.num', { class: r.kind === 'base' || zero ? 'muted' : down ? 'delta-down' : 'delta-up' },
          r.kind === 'base' ? '—' : zero ? '0' : `${down ? '' : '+'}${money(r.delta)}`),
        h('td.right.num', null, money(r.after, currency)));
    }))));
}

export function priceView(p) {
  return [
    h('div', null, h('div.small.muted', null, 'Price'), h('div.price-big', null, money(p.price, p.currency)), took(p)),
    p.bounds && h('p.small.muted', null, `Allowed range for this request: ${money(p.bounds.lower, p.currency)} to ${money(p.bounds.upper, p.currency)}`),
    (p.flags || []).length > 0 && note('warn', 'triangle-alert', h('ul.problems', null, p.flags.map((f) => h('li', null, f)))),
    waterfall(p.waterfall, p.currency),
    (p.counterfactuals || []).length > 0 && h('div', null, h('h3', null, 'What if'),
      h('ul.problems.muted', null, p.counterfactuals.map((c) => h('li', null, `${c.if}: `,
        c.price ? h('strong', null, money(c.price, p.currency)) : (c.error && c.error.message) || '—')))),
  ];
}

function tryPrice(data) {
  const pack = data.pack || {};
  const result = h('div.form');
  const inputs = {};
  const fields = [];
  const basePrice = pack.base && pack.base.op === 'from_request' ? h('input', { type: 'text', inputMode: 'decimal', value: '1000' }) : null;
  if (basePrice) fields.push(h('label.field', null, h('span', null, `Base price (${pack.currency})`), basePrice));
  const asOf = h('input', { type: 'datetime-local' });
  fields.push(h('label.field', null, h('span', null, 'Date and time'), asOf, h('small', null, 'Empty means now. Weekend, hour and festival rules follow this.')));
  for (const [name, s] of Object.entries(pack.signals || {})) {
    if (s.source === 'public') continue;
    const fallback = s.default === undefined || s.default === null ? '' : String(s.default);
    let input;
    if (s.type === 'enum') input = h('select', null, [fallback ? null : h('option', { value: '' }, 'Choose…'), ...s.values.map((v) => h('option', { value: String(v), selected: String(v) === fallback }, String(v)))]);
    else if (s.type === 'bool') input = h('select', null, ['', 'true', 'false'].map((v) => h('option', { value: v, selected: v === fallback }, v || 'Not sent')));
    else input = h('input', { type: 'text', inputMode: s.type === 'text' ? 'text' : 'decimal', placeholder: fallback ? `default ${fallback}` : '' });
    inputs[name] = [s, input];
    fields.push(h('label.field', null, h('span.mono', null, name),
      input, h('small', null, [s.description, s.range && `${s.range[0]} to ${s.range[1]}`].filter(Boolean).join(' · ') || s.type)));
  }
  const button = h('button.btn.primary', { type: 'submit' }, icon('calculator'), 'Calculate');
  const form = h('form.form', { onsubmit: (e) => {
    e.preventDefault();
    run(button, async () => {
      const signals = {};
      for (const [name, [s, input]] of Object.entries(inputs)) {
        const v = input.value.trim();
        if (v === '') continue;
        // numbers travel as text so nothing is rounded on the way
        signals[name] = s.type === 'bool' ? v === 'true' : s.type === 'int' && /^-?\d+$/.test(v) ? Number(v) : v;
      }
      const body = { pack_version: data.version, signals };
      if (basePrice && basePrice.value.trim()) body.base_price = basePrice.value.trim();
      if (asOf.value) body.as_of = new Date(asOf.value).toISOString();
      try {
        replace(result, priceView(await api('POST', `/v1/simulate/${enc(data.domain)}`, body)));
      } catch (err) {
        replace(result, errorBox(err, 'No price'));
      }
    });
  } }, h('div.form-row', null, fields), h('div.actions', null, button,
    h('span.small.muted', null, 'A test only: nothing is recorded and no customer sees it.')));
  return h('div.panel-body.form', null, form, result);
}

// ---------- one version ----------

function problems(report) {
  const list = (items, tone, glyph, title) => items.length > 0 && note(tone, glyph, h('strong', null, title),
    h('ul.problems', null, items.map((p) => h('li', null, p.path && h('code', null, `${p.path} `), p.message))));
  return [
    list(report.errors || [], 'danger', 'circle-x', `${report.errors.length} problem${report.errors.length === 1 ? '' : 's'} to fix`),
    list(report.warnings || [], 'warn', 'triangle-alert', `${(report.warnings || []).length} thing${(report.warnings || []).length === 1 ? '' : 's'} to look at`),
    report.ok && (report.warnings || []).length === 0 && note('ok', 'circle-check', 'No problems found.'),
  ];
}

const fact = (value, label) => h('div.fact', null, h('b', null, value), h('span', null, label));

function safety(report) {
  const broken = Object.entries(report.violation_counts || {});
  return [
    h('div.facts', null, fact(count(report.cases), 'random requests priced'),
      fact(count(broken.reduce((n, [, c]) => n + c, 0)), 'broke a guarantee'), fact(`${report.seconds} s`, 'time taken')),
    report.ok ? note('ok', 'circle-check', 'Every price stayed inside its limits, added up exactly and could be reproduced.')
      : note('danger', 'circle-x', h('strong', null, 'These rules can produce a price that breaks a guarantee'),
        report.smallest && h('span', null, report.smallest.message),
        report.smallest && h('code.inline', null, JSON.stringify(report.smallest.input))),
  ];
}

function impactView(r) {
  if (!r.compared) return note('', 'info', 'There are no recorded prices for this domain yet, so there is nothing to compare with.');
  return [
    h('div.facts', null, fact(count(r.compared), 'recent prices re-run'), fact(`${r.up_pct}%`, 'would go up'),
      fact(`${r.down_pct}%`, 'would go down'), fact(`${r.unchanged_pct}%`, 'stay the same'),
      r.change_p50 !== undefined && r.change_p50 !== null && fact(`${r.change_p50}%`, 'typical change')),
    r.would_fail > 0 && note('warn', 'triangle-alert', `${r.would_fail} of those requests would get an error instead of a price.`),
  ];
}

export async function version(name, number) {
  const data = await api('GET', `/v1/packs/${enc(name)}/${enc(number)}`);
  const base = `/v1/packs/${enc(name)}/${data.version}`;
  const me = session.me;
  const open = data.status === 'draft' || data.status === 'approved';
  let impact = null;
  const steps = h('ol.steps');

  const step = (n, state, title, text, action, result) => h('li.step', { class: state },
    h('div.step-mark', null, state === 'done' ? icon('check') : state === 'failed' ? icon('x') : n),
    h('div.step-text', null, h('h3', null, title), h('p', null, text)),
    h('div.actions', null, action), h('div.step-result', null, result));
  const state = (report) => (report ? (report.ok ? 'done' : 'failed') : '');

  const approveWith = (key) => api('POST', `${base}/approve`, undefined, key ? { key } : {});
  const askSecondKey = () => dialog('Approve as a second person', (close) => {
    const input = h('input', { type: 'password', autocomplete: 'off', spellcheck: false, placeholder: 'm360_live_…' });
    const problem = h('div');
    const ok = h('button.btn.primary', { type: 'button', onclick: () => run(ok, async () => {
      try { await approveWith(input.value.trim()); close(true); } catch (err) {
        const why = err.status === 401 ? { message: 'That key isn’t valid.' }
          : err.status === 404 ? { message: `That key isn’t allowed to work on ${name}. It may be limited to other domains.` }
            : err.status === 403 ? { message: 'That key can’t approve. It needs the approver or admin role.' } : err;
        replace(problem, errorBox(why, 'Not approved'));
      }
    }) }, icon('user-check'), 'Approve');
    return {
      body: [
        note('info', 'lock', `Key ${data.owner} wrote this version, so a different person must approve it. `
          + 'Ask them to paste their approver key here. It is used for this one approval and is not kept.'),
        h('label.field', null, h('span', null, 'Approver’s API key'), input), problem],
      foot: [h('button.btn', { type: 'button', onclick: () => close() }, 'Cancel'), ok],
    };
  });

  function draw() {
    const lintOk = data.lint_report && data.lint_report.ok;
    const verifyOk = data.verify_report && data.verify_report.ok;
    const draft = data.status === 'draft';
    const mine = data.owner === me.key_id;

    const lintBtn = h('button.btn.sm', { type: 'button', disabled: !draft, onclick: () => run(lintBtn, async () => {
      data.lint_report = await api('POST', `${base}/lint`);
      draw();
    }) }, icon('list-checks'), data.lint_report ? 'Check again' : 'Check rules');
    const verifyBtn = h('button.btn.sm', { type: 'button', disabled: !draft, onclick: () => run(verifyBtn, async () => {
      data.verify_report = await api('POST', `${base}/verify`, { cases: 10000 });
      draw();
    }) }, icon('flask-conical'), data.verify_report ? 'Test again' : 'Run safety test');
    const impactBtn = h('button.btn.sm', { type: 'button', onclick: () => run(impactBtn, async () => {
      impact = await api('POST', `${base}/impact`, { last_n: 1000 });
      draw();
    }) }, icon('chart-column'), impact ? 'Run again' : 'Preview impact');
    const approveBtn = h('button.btn.sm', { type: 'button', class: lintOk && verifyOk && draft ? 'primary' : '',
      disabled: !(lintOk && verifyOk && draft), onclick: () => run(approveBtn, async () => {
        const done = mine ? await askSecondKey() : await approveWith().then(() => true);
        if (done) { toast(`Version ${data.version} approved`); reload(); }
      }) }, icon('user-check'), mine ? 'Approve with another key' : 'Approve');
    const publishBtn = h('button.btn.sm', { type: 'button', class: data.status === 'approved' ? 'primary' : '',
      disabled: data.status !== 'approved', onclick: async () => {
        const yes = await confirm({ title: `Make version ${data.version} live?`, action: 'Publish', tone: 'warn', iconName: 'radio-tower',
          text: `Every new ${name} price will use these rules, on every server, straight away. You can roll back in one click.` });
        if (yes) await run(publishBtn, async () => {
          try { const live = await api('POST', `${base}/publish`); toast(`Version ${data.version} is live · ${tookText(live)}`); reload(); }
          catch (err) { await dialog('Not published', () => ({ body: errorBox(err, 'Not published') })); }
        });
      } }, icon('send'), 'Publish');

    replace(steps,
      step(1, state(data.lint_report), 'Check the rules',
        'Looks for mistakes, unfair rules, banned signals and anything that breaks your limits.', lintBtn,
        data.lint_report && [problems(data.lint_report), took(data.lint_report)]),
      step(2, state(data.verify_report), 'Safety test',
        'Prices 10,000 random requests and proves none of them breaks a floor, a ceiling or the arithmetic.', verifyBtn,
        data.verify_report && [safety(data.verify_report), took(data.verify_report)]),
      step(3, impact ? 'done' : '', 'Preview the impact (optional)',
        'Re-runs your most recent real prices with these rules and shows what would have changed.', impactBtn,
        impact && [impactView(impact), took(impact)]),
      step(4, data.approver ? 'done' : '', 'Approval by a second person',
        data.approver ? `Approved by key ${data.approver} on ${when(data.approved_at)}.`
          : lintOk && verifyOk ? 'Both checks passed. Whoever wrote the rules cannot approve them, so a second person signs off.'
            : 'Whoever wrote the rules cannot approve them. Steps 1 and 2 must pass first.', !data.approver && approveBtn),
      step(5, '', 'Publish', 'Makes this version live for customers.', publishBtn));
  }
  if (open) draw();

  // tabs
  const TABS = {
    Rules: () => rulesView(data.pack || {}),
    YAML: () => [
      h('div.panel-body.actions', null,
        h('button.btn.sm', { type: 'button', onclick: () => download(`${name}-v${data.version}.yaml`, data.yaml, 'application/yaml') }, icon('download'), 'Download'),
        h('button.btn.sm', { type: 'button', onclick: () => copy(data.yaml) }, icon('copy'), 'Copy'),
        h('button.btn.sm', { type: 'button', onclick: () => uploadDialog(data.yaml) }, icon('pencil-line'), 'Edit as a new version')),
      h('pre.code', null, data.yaml)],
    'Changes from live': () => diffView(data.diff_from_live),
    'Try a price': () => tryPrice(data),
  };
  const tabBody = h('div');
  const tabBar = h('div.tabs', { role: 'tablist' });
  const show = (label) => {
    replace(tabBar, Object.keys(TABS).map((t) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(t === label), onclick: () => show(t) }, t)));
    replace(tabBody, TABS[label]());
  };
  show('Rules');

  return [
    h('div.page-head', null, h('div', null,
      h('a.crumb', { href: `#/domains/${enc(name)}` }, icon('arrow-left'), name),
      h('div.title-row', null, h('h1', null, `${name} · version ${data.version}`), statusBadge(data.status)),
      h('p', null, `Written by key ${data.owner} · saved ${when(data.created_at)}`,
        data.published_at ? ` · first live ${when(data.published_at)}` : ''))),
    data.status === 'published' && note('ok', 'radio-tower', h('span', null, h('strong', null, 'This version is live. '),
      `Every ${name} price is set by these rules. To change them, open the YAML tab and choose “Edit as a new version”.`)),
    data.status === 'archived' && note('', 'history', h('span', null, h('strong', null, 'This version was live before. '),
      `Version ${data.live_version} is live now. You can roll back to this one from the ${name} page.`)),
    open && h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, 'Path to live'),
        h('p', null, 'Nothing here affects customers until the last step.'))), steps),
    h('section.panel', null, tabBar, tabBody),
  ];
}
