// The developer portal: for whoever connects an app to the engine. Same frame and parts as the admin console.
// Any key may sign in. Prices asked for here are real: they go through POST /v1/price and are recorded.
import {
  api, session, h, icon, replace, badge, note, empty, errorBox, run, toast, copy, loading, money, shell, signInCard, dialog, ms, tookText,
} from './core.js';
import { priceView } from './packs.js';
import { performancePage } from './perf.js';

const root = document.getElementById('app');
const NAV = [['prices', 'Get a price', 'tag'], ['key', 'API key', 'key-round'], ['app', 'Sample app', 'server'],
  ['performance', 'Performance', 'chart-column'], ['reference', 'Reference', 'file-text']];
let appKey = null;         // the key the sample app runs on; null = the one you are signed in with
const issued = [];          // prices this tab was given, newest first
let frame = null;
let visit = 0;

function signIn(message) {
  frame = null;
  replace(root, signInCard({
    title: 'Developer portal', text: 'Sign in with an API key of your organisation.', action: 'Open portal', message,
    hint: 'No key yet? An admin of your organisation creates one, here or in the admin console. The key stays in this browser tab only.',
    onKey: async (key) => {
      session.me = await api('GET', '/v1/me', undefined, { key });
      session.set(key);
      open();
      return null;
    },
  }));
}

function open() {
  const links = [['/docs', 'API reference', 'file-code', true]];
  if (session.me.role === 'admin') links.unshift(['/admin/', 'Admin console', 'layout-dashboard']);
  frame = shell({ tag: 'Developers', nav: NAV, links, onSignOut: () => { session.clear(); issued.length = 0; appKey = null; signIn(); } });
  replace(root, frame.el);
  route();
}

// ---------- code samples ----------

const SAMPLES = {
  curl: (url, body) => `curl -X POST ${url} \\\n  -H "Authorization: Bearer $M360_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify(body)}'`,
  Python: (url, body) => `import os, requests\n\nr = requests.post(\n    "${url}",\n    headers={"Authorization": f"Bearer {os.environ['M360_API_KEY']}"},\n    json=${JSON.stringify(body, null, 4).replace(/\n/g, '\n    ').replace(/\btrue\b/g, 'True').replace(/\bfalse\b/g, 'False')},\n    timeout=5,\n)\nr.raise_for_status()\nprice = r.json()\nprint(price["price"], price["currency"], "valid until", price["valid_until"])`,
  JavaScript: (url, body) => `const r = await fetch("${url}", {\n  method: "POST",\n  headers: {\n    Authorization: \`Bearer \${process.env.M360_API_KEY}\`,\n    "Content-Type": "application/json",\n  },\n  body: JSON.stringify(${JSON.stringify(body, null, 2).replace(/\n/g, '\n  ')}),\n});\nif (!r.ok) throw new Error((await r.json()).message);\nconst price = await r.json();\nconsole.log(price.price, price.currency, "valid until", price.valid_until);`,
};

// ---------- Get a price ----------

async function prices() {
  const { domains } = await api('GET', '/v1/domains');
  const head = h('div.page-head', null, h('div', null, h('h1', null, 'Get a price'),
    h('p', null, 'Send what your app knows about the item. The engine applies your organisation’s live rules and answers with the price and how it got there.')));
  if (domains.length === 0) {
    return [head, h('section.panel', null, empty('layers', 'Nothing to price yet', 'No domain is live for this key. An admin publishes rules in the admin console first.'))];
  }

  const picker = h('select', null, domains.map((d) => h('option', { value: d.domain }, d.domain)));
  const fieldsBox = h('div.form-row');
  const preview = h('input', { type: 'checkbox' });
  const send = h('button.btn.primary', { type: 'submit' }, icon('send'), 'Get price');
  const codeBox = h('pre.code');
  const tabBar = h('div.tabs', { role: 'tablist' });
  const result = h('div.panel-body.form');
  const resultPanel = h('section.panel', { hidden: true }, h('div.panel-head', null, h('h2', null, 'Response')), result);
  const log = h('div');
  let language = 'curl';
  let inputs = {};
  let entity, basePrice, idem;

  const current = () => domains.find((d) => d.domain === picker.value);
  const request = () => {
    const signals = {};
    for (const [name, [s, input]] of Object.entries(inputs)) {
      const v = input.value.trim();
      if (v === '') continue;
      signals[name] = s.type === 'bool' ? v === 'true' : s.type === 'int' && /^-?\d+$/.test(v) ? Number(v) : v;
    }
    const body = { entity: entity.value.trim() || 'room-101' };
    if (basePrice && basePrice.value.trim()) body.base_price = basePrice.value.trim();
    body.signals = signals;
    if (!preview.checked && idem.value.trim()) body.idempotency_key = idem.value.trim();
    return { path: `/v1/${preview.checked ? 'simulate' : 'price'}/${current().domain}`, body };
  };
  const showCode = () => {
    const { path, body } = request();
    replace(tabBar, Object.keys(SAMPLES).map((l) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(l === language),
      onclick: () => { language = l; showCode(); } }, l)),
    h('button.btn.quiet.sm.tab-copy', { type: 'button', onclick: () => copy(codeBox.textContent) }, icon('copy'), 'Copy'));
    replace(codeBox, SAMPLES[language](`${location.origin}${path}`, body));
  };
  const showLog = () => replace(log, issued.length > 0 && h('section.panel', null,
    h('div.panel-head', null, h('div', null, h('h2', null, 'Prices you were given'), h('p', null, 'Each one is recorded on the server. Keep the price ID with the order.'))),
    h('div.table-wrap', null, h('table', null,
      h('thead', null, h('tr', null, h('th', null, 'Time'), h('th', null, 'Domain'), h('th', null, 'Item'), h('th.right', null, 'Price'), h('th', null, 'Price ID'), h('th', null, ''))),
      h('tbody', null, issued.map((p) => h('tr', null,
        h('td.nowrap.muted', null, p.at.toLocaleTimeString()), h('td', null, p.domain), h('td', null, p.entity),
        h('td.right.num', null, h('strong', null, money(p.price, p.currency))), h('td.mono', null, p.price_id),
        h('td.right', null, h('button.btn.quiet.sm', { type: 'button', 'aria-label': 'Copy the price ID', onclick: () => copy(p.price_id) }, icon('copy'))))))))));
  const build = () => {
    const d = current();
    inputs = {};
    entity = h('input', { type: 'text', value: 'room-101', maxLength: 200 });
    basePrice = d.needs_base_price ? h('input', { type: 'text', inputMode: 'decimal', value: '3000' }) : null;
    idem = h('input', { type: 'text', maxLength: 200, placeholder: 'booking-8812' });
    const fields = [h('label.field', null, h('span', null, 'Item'), entity, h('small', null, 'entity · what is being priced: a room, a seat, a product'))];
    if (basePrice) fields.push(h('label.field', null, h('span', null, `Base price (${d.currency})`), basePrice, h('small', null, 'base_price · your normal price for it')));
    for (const s of d.signals) {
      const fallback = s.default === null ? '' : String(s.default);
      const optional = s.required ? null : h('option', { value: '' }, `default (${fallback})`);
      let input;
      if (s.type === 'enum') input = h('select', null, [optional, ...s.values.map((v) => h('option', { value: v }, v))]);
      else if (s.type === 'bool') input = h('select', null, [optional, h('option', { value: 'true' }, 'true'), h('option', { value: 'false' }, 'false')]);
      else input = h('input', { type: 'text', inputMode: s.type === 'text' ? 'text' : 'decimal', placeholder: s.required ? 'required' : `default ${fallback}`, value: s.required && s.range ? s.range[0] : '' });
      inputs[s.name] = [s, input];
      fields.push(h('label.field', null, h('span', null, s.name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())), input,
        h('small', null, [`signals.${s.name}`, s.type, s.range && `${s.range[0]} to ${s.range[1]}`, s.required ? 'required' : 'optional'].filter(Boolean).join(' · '))));
    }
    fields.push(h('label.field', null, h('span', null, 'Idempotency key'), idem, h('small', null, 'optional · the same key gives the same price again, never a second record')));
    replace(fieldsBox, fields);
    resultPanel.hidden = true;
    showCode();
  };
  picker.addEventListener('change', build);
  preview.addEventListener('change', showCode);
  fieldsBox.addEventListener('input', showCode);
  build();
  showLog();

  const form = h('form.form', { onsubmit: (e) => {
    e.preventDefault();
    run(send, async () => {
      const { path, body } = request();
      resultPanel.hidden = false;
      try {
        const price = await api('POST', path, body);
        if (price.price_id && !issued.some((p) => p.price_id === price.price_id)) {
          issued.unshift({ ...price, entity: body.entity, at: new Date() });
          showLog();
        }
        replace(result, priceView(price),
          price.price_id
            ? note('ok', 'circle-check', h('span', null, 'Recorded as ', h('code.inline', null, price.price_id), `. You may charge this price until ${new Date(price.valid_until).toLocaleTimeString()}.`))
            : note('', 'info', 'A preview: nothing was recorded and there is no price ID.'),
          h('details', null, h('summary', null, 'Full JSON response'), h('pre.code', null, JSON.stringify(price, null, 2))));
      } catch (err) {
        replace(result, errorBox(err, `${err.status} ${err.code}`));
      }
    });
  } },
  h('div.form-row', null, h('label.field', null, h('span', null, 'Domain'), picker, h('small', null, 'What you are pricing. Only domains your key may price are listed.'))),
  fieldsBox,
  h('div.actions', null, send, h('label.check', null, preview, h('span.muted', null, 'Preview only, don’t record'))));

  return [head,
    h('section.panel', null, h('div.panel-head', null, h('h2', null, 'Request')), h('div.panel-body', null, form)),
    resultPanel,
    h('section.panel', null, h('div.panel-head', null, h('div', null, h('h2', null, 'The same request from your code'),
      h('p', null, 'It changes as you fill in the form. Keep the key in an environment variable on your server.'))), h('div.codebox', null, tabBar, codeBox)),
    log];
}

// ---------- API key ----------

async function key() {
  const me = session.me;
  const head = h('div.page-head', null, h('div', null, h('h1', null, 'API key'),
    h('p', null, 'Every request carries a key. The key decides whose rules apply and what the caller may do.')));
  const mine = h('section.panel', null, h('div.panel-head', null, h('h2', null, 'The key you are signed in with')),
    h('div.panel-body', null, h('dl.kv', null,
      h('dt', null, 'Organisation'), h('dd', null, me.tenant_id),
      h('dt', null, 'Key ID'), h('dd.mono', null, me.key_id),
      h('dt', null, 'Role'), h('dd', null, badge(me.role, me.role === 'pricing' ? 'ok' : 'info')),
      h('dt', null, 'May price'), h('dd', null, me.domains.includes('*') ? 'Every domain of your organisation' : me.domains.join(', ')))));
  const care = note('', 'lock', 'Keep keys on your server, in its secret store. Never put one in a web page or a mobile app: anyone could read it there.');
  if (me.role !== 'admin') {
    return [head, mine, note('', 'info', 'New keys are created by an admin of your organisation. A key is shown once, when it is created.'), care];
  }

  const { domains } = await api('GET', '/v1/domains');
  const made = h('div');
  const name = h('input', { type: 'text', maxLength: 80, placeholder: 'Checkout server' });
  const scope = h('select', null, h('option', { value: '*' }, 'All domains'), domains.map((d) => h('option', { value: d.domain }, `${d.domain} only`)));
  const create = h('button.btn.primary', { type: 'submit' }, icon('key-round'), 'Create key');
  const form = h('form.form', { onsubmit: (e) => {
    e.preventDefault();
    run(create, async () => {
      const body = { role: 'pricing', domains: [scope.value] };
      if (name.value.trim()) body.name = name.value.trim();
      const fresh = await api('POST', '/v1/admin/keys', body);
      replace(made,
        note('warn', 'triangle-alert', h('span', null, h('strong', null, 'Copy it now. '), 'This is the only time the full key is shown; only its fingerprint is stored.')),
        h('div.secret', null, h('code', null, fresh.api_key),
          h('button.btn.sm', { type: 'button', onclick: () => copy(fresh.api_key) }, icon('copy'), 'Copy'),
          h('button.btn.sm.primary', { type: 'button', title: 'Sign in to this portal with the new key',
            onclick: async () => { session.set(fresh.api_key); session.me = await api('GET', '/v1/me'); toast('Now using the new key'); open(); location.hash = '#/prices'; } },
          icon('arrow-right'), 'Use it here'),
          h('button.btn.sm', { type: 'button', title: 'Run the sample hotel app on this key',
            onclick: () => { appKey = fresh.api_key; location.hash = '#/app'; } }, icon('server'), 'Run the sample app with it')));
    });
  } },
  h('div.form-row', null,
    h('label.field', null, h('span', null, 'Name'), name, h('small', null, 'What will use it.')),
    h('label.field', null, h('span', null, 'May price'), scope, h('small', null, 'Limit it to one domain if the app needs only one.'))),
  h('div.actions', null, create), made);

  return [head, mine,
    h('section.panel', null, h('div.panel-head', null, h('div', null, h('h2', null, 'Create a key for your app'),
      h('p', null, 'A pricing key can ask for prices and nothing else. It cannot read or change rules, limits or other keys.'))),
    h('div.panel-body', null, form)), care];
}

// ---------- Reference ----------

const FIELDS = [
  ['price', 'The price to charge, as text with two decimals. Never a float.'],
  ['price_id', 'Proof of this price. Store it with the order; it finds the full record later.'],
  ['valid_until', 'You may reuse the price until then without asking again.'],
  ['waterfall', 'Every step from the base price to the final price, with the reason. Show it to staff or customers.'],
  ['counterfactuals', 'What the price would have been without each rule.'],
  ['bounds', 'The lowest and highest price that was allowed for this request.'],
  ['flags', 'Anything unusual, for example a default was used or the engine is switched off.'],
];
const ERRORS = [
  ['401 unauthorized', 'The key is missing, wrong, expired or revoked.'],
  ['404 pack_not_found', 'That domain is not live for you, or the key may not price it.'],
  ['409 idempotency_conflict', 'The same idempotency key was sent with a different request.'],
  ['422 signal_invalid', 'An input is out of range. The answer says which one.'],
  ['422 signal_missing', 'A required input was not sent.'],
  ['429 rate_limited', 'Too many requests for this key. Wait for the seconds in the Retry-After header.'],
  ['503 killed', 'Pricing for the domain is frozen and this item never had a price.'],
];
const ENDPOINTS = [
  ['POST /v1/price/{domain}', 'One price. Recorded before it is returned.'],
  ['POST /v1/price/{domain}/batch', 'Up to 500 prices in one call. One bad item does not fail the rest.'],
  ['POST /v1/simulate/{domain}', 'The same answer without recording anything.'],
  ['GET /v1/domains', 'The domains your key may price and the inputs each one takes.'],
  ['GET /v1/me', 'Whose key this is and what it may do.'],
];
const table = (title, text, rows) => h('section.panel', null,
  h('div.panel-head', null, h('div', null, h('h2', null, title), text && h('p', null, text))),
  h('div.table-wrap', null, h('table', null, h('tbody', null, rows.map(([a, b]) => h('tr', null, h('td.mono.nowrap', null, a), h('td', null, b)))))));

async function reference() {
  return [
    h('div.page-head', null, h('div', null, h('h1', null, 'Reference'), h('p', null, 'What your app calls, what comes back and what a refusal means.')),
      h('a.btn', { href: '/docs', target: '_blank', rel: 'noopener' }, icon('file-code'), 'Full API reference')),
    table('Calls', 'All of them take the header Authorization: Bearer <your key>.', ENDPOINTS),
    table('The answer', 'Fields of a price response.', FIELDS),
    table('When a request is refused', 'Every refusal has the same shape: error, message, details, request_id.', ERRORS),
  ];
}

// ---------- router ----------

// ---------- Sample app: a customer's storefront, running on an API key ----------
// It builds itself from GET /v1/domains for that key, so an airline's key shows flights and a hotel's key rooms.

const SHOP = {
  hotel: { name: 'Sunrise Hotel', what: 'rooms', verb: 'Book', items: [['Standard room', '2000'], ['Deluxe room', '3000'], ['Family room', '2600'], ['Suite', '3500']] },
  airline: { name: 'Skyline Air', what: 'fares', verb: 'Book' },
  ride: { name: 'CityCab', what: 'rides', verb: 'Request' },
  parking: { name: 'ParkEasy', what: 'spaces', verb: 'Reserve' },
};
const nice = (text) => String(text).replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

async function sampleApp() {
  const today = new Date().toLocaleDateString('en-CA');           // yyyy-mm-dd, local
  const usedKey = () => appKey || session.key;
  const keyInfo = h('div.actions');
  const keyInput = h('input', { type: 'password', autocomplete: 'off', spellcheck: false, placeholder: 'Paste another key to run the app on it' });
  const live = h('input', { type: 'checkbox', checked: true });
  const night = h('input', { type: 'date', value: today });
  const picker = h('select', { 'aria-label': 'Domain' });
  const title = h('h2');
  const controls = h('div.form-row');
  const grid = h('div.cards');
  const wire = h('div.wire');
  let domains = [];
  let domain = null;          // the domain on screen
  let cards = [];             // [{name, entity, base, fixed: {signal: value}}]
  let readers = {};           // signal name -> () => value to send, or undefined

  const sent = (method, path, started, status, text, answer) => replace(wire,
    h('span.mono', null, `${method} ${path}`), badge(String(status), status === 200 ? 'ok' : 'danger'),
    h('strong.num', null, tookText(answer) || ms(performance.now() - started)), h('span.muted', null, text));

  const body = (card) => {
    const signals = { ...card.fixed };
    for (const [name, read] of Object.entries(readers)) { const v = read(); if (v !== undefined && v !== '') signals[name] = v; }
    const item = { entity: `${card.entity}@${night.value || today}`, signals };
    if (card.base) item.base_price = card.base;
    if (night.value && night.value !== today) item.as_of = `${night.value}T12:00:00Z`;
    return item;
  };

  const book = (card) => async (e) => {
    const started = performance.now();
    const path = `/v1/price/${domain.domain}`;
    const request = { ...body(card), idempotency_key: `order-${Date.now()}` };
    await run(e.currentTarget, async () => {
      const price = await api('POST', path, request, { key: usedKey() });
      sent('POST', path, started, 200, 'order confirmed at this price', price);
      await dialog(`${card.name}: confirmed`, (close) => ({
        body: [priceView(price), note('ok', 'circle-check', h('span', null, 'The app stores this with the order: ', h('code.inline', null, price.price_id),
          '. Retrying with the same idempotency key ', h('code.inline', null, request.idempotency_key), ' can never create a second record.'))],
        foot: h('button.btn.primary', { type: 'button', onclick: () => close() }, 'Done'),
      }), { wide: true });
    });
  };

  const refresh = async () => {
    if (!domain) return;
    const started = performance.now();
    const path = `/v1/price/${domain.domain}/batch`;
    try {
      const answer = await api('POST', path, { items: cards.map(body) }, { key: usedKey() });
      const { results } = answer;
      sent('POST', path, started, 200, `${results.length} priced with rules v${(results.find((r) => r.pack_version) || {}).pack_version || '?'}`, answer);
      replace(grid, cards.map((card, i) => {
        const r = results[i];
        if (r.error) {
          const why = typeof r.error === 'string' ? r.message : r.error.message;
          return h('div.panel.card', null, h('strong', null, card.name), h('div.small.muted', null, why || 'The engine gave no price for this one.'), h('div'), h('div'));
        }
        const moved = r.waterfall.filter((w) => w.kind !== 'base' && !/^-?0+(\.0+)?$/.test(w.delta));
        const steps = moved.filter((w) => w.kind === 'rule');          // a shopper sees the reasons, not the rounding
        const limited = moved.some((w) => w.kind !== 'rule' && w.kind !== 'rounding');
        return h('div.panel.card', null,
          h('div', null, h('strong', null, card.name), h('div.mono.faint', null, card.entity)),
          h('div', null, h('div.card-price', null, money(r.price, r.currency)),
            card.base && h('div.small.muted', null, r.price !== `${card.base}.00` ? `normally ${money(card.base, r.currency)}` : 'the normal price')),
          h('div.chips', null, steps.map((w) => badge(`${w.reason} ${w.delta.startsWith('-') ? '−' : '+'}${money(w.delta.replace('-', ''))}`)),
            r.flags.map((f) => badge(f.split(':')[0].split('(')[0].trim(), 'warn', 'triangle-alert')),
            limited && badge('Kept within the allowed range', 'info'),
            steps.length === 0 && !limited && r.flags.length === 0 && badge('No rule applied')),
          h('button.btn.primary', { type: 'button', onclick: book(card) }, (SHOP[domain.domain] || {}).verb || 'Buy'));
      }));
    } catch (err) {
      sent('POST', path, started, err.status, err.code);
      if (err.status === 401) replace(keyInfo, badge('Refused', 'danger', 'x'), h('span.muted', null, 'The engine no longer accepts this key.'));
      replace(grid, errorBox(err.status === 401 ? { message: 'The engine refused this app’s key. It is wrong, expired or revoked, so the app gets no prices.' } : err, `${err.status} ${err.code}`));
    }
  };
  let pending = null;
  const soon = () => { clearTimeout(pending); pending = setTimeout(refresh, 250); };

  // One control per input the domain takes. A number with a range becomes a slider, a list a menu.
  const control = (s) => {
    const label = h('strong.num');
    let input, read;
    if (s.type === 'enum' || s.type === 'bool') {
      const values = s.type === 'bool' ? ['true', 'false'] : s.values;
      input = h('select', { onchange: refresh }, values.map((v) => h('option', { value: v, selected: String(s.default) === v }, nice(v))));
      read = () => (s.type === 'bool' ? input.value === 'true' : input.value);
    } else if (s.range) {
      const [lo, hi] = s.range.map(Number);
      const whole = s.type === 'int';
      const start = s.default !== null ? Number(s.default) : s.type === 'ratio' ? lo + (hi - lo) * 0.85 : lo + (hi - lo) * 0.05;
      input = h('input', { type: 'range', min: lo, max: hi, step: whole ? 1 : (hi - lo) / 100, value: whole ? Math.round(start) : start });
      const show = () => replace(label, s.type === 'ratio' ? `${Math.round(input.value * 100)}%` : String(Number(Number(input.value).toFixed(2))));
      input.addEventListener('input', () => { show(); soon(); });
      show();
      read = () => (whole ? Number(input.value) : Number(input.value).toFixed(2));
    } else {
      input = h('input', { type: 'text', inputMode: s.type === 'text' ? 'text' : 'decimal', value: s.default !== null ? String(s.default) : s.type === 'text' ? '' : '1', oninput: soon });
      read = () => (s.type === 'int' && /^-?\d+$/.test(input.value.trim()) ? Number(input.value.trim()) : input.value.trim());
    }
    readers[s.name] = read;
    return h('label.field', null, h('span', null, `${nice(s.name)} `, label), input, h('small', null, `sent as signals.${s.name}`));
  };

  // What is for sale: named rooms for the hotel; otherwise one card per base price, or per choice of the first menu input.
  const show = (name) => {
    domain = domains.find((d) => d.domain === name) || null;
    readers = {};
    if (!domain) return;
    const shop = SHOP[name] || { name: nice(name), what: 'items' };
    replace(title, `${shop.name} · ${shop.what}`);
    let split = null;
    if (domain.needs_base_price) {
      const items = shop.items || [['Basic', '1000'], ['Standard', '2000'], ['Plus', '3000'], ['Premium', '5000']];
      cards = items.map(([label, base]) => ({ name: label, entity: label.toLowerCase().replace(/\W+/g, '-'), base, fixed: {} }));
    } else {
      split = domain.signals.find((s) => s.type === 'enum');
      cards = split ? split.values.slice(0, 4).map((v) => ({ name: nice(v), entity: `${name}-${v}`, base: null, fixed: { [split.name]: v } }))
        : [{ name: nice(name), entity: `${name}-1`, base: null, fixed: {} }];
    }
    replace(controls, domain.signals.filter((s) => s !== split).map(control),
      h('label.field', null, h('span', null, 'Date'), night, h('small', null, 'weekends, hours and festivals are worked out by the engine')));
    replace(grid);
    refresh();
  };

  const connect = async () => {
    domain = null;
    try {
      const me = await api('GET', '/v1/me', undefined, { key: usedKey() });
      domains = (await api('GET', '/v1/domains', undefined, { key: usedKey() })).domains;
      replace(keyInfo, badge('Connected', 'ok', 'check'), h('span', null, `${me.tenant_id} · key `, h('span.mono', null, me.key_id)), badge(me.role, me.role === 'pricing' ? '' : 'info'));
    } catch (err) {
      domains = [];
      replace(keyInfo, badge('Refused', 'danger', 'x'), h('span.muted', null, 'The engine does not accept this key.'));
    }
    replace(picker, domains.map((d) => h('option', { value: d.domain }, d.domain)));
    picker.hidden = domains.length < 2;
    if (domains.length === 0) {
      replace(title, 'Nothing to sell yet');
      replace(controls);
      replace(grid, empty('layers', 'No live domain for this key', 'Publish rules in the admin console, or use a key that may price a live domain.'));
      return;
    }
    show(domains[0].domain);
  };

  picker.addEventListener('change', () => show(picker.value));
  night.addEventListener('change', refresh);
  const timer = setInterval(() => {
    if (!grid.isConnected) { clearInterval(timer); return; }          // left the page
    if (live.checked && !document.querySelector('dialog[open]')) refresh();
  }, 4000);
  const useKey = h('button.btn', { type: 'submit' }, 'Use this key');
  await connect();

  return [
    h('div.page-head', null, h('div', null, h('h1', null, 'Sample app'),
      h('p', null, 'A small storefront, the kind your customers build. It holds an API key, asks the engine what that key may price, and gets every price from it. Nothing about pricing is written in it.'))),
    h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, 'This app’s API key'), h('p', null, 'A real app keeps it on its server. Revoke it in the admin console and this screen stops getting prices.')), keyInfo),
      h('div.panel-body', null, h('form.actions', { onsubmit: (e) => { e.preventDefault(); const k = keyInput.value.trim().replace(/^Bearer\s+/i, '').replace(/^["'\s]+|["',\s]+$/g, ''); if (k) { appKey = k; keyInput.value = ''; run(useKey, connect); } } },
        h('div.grow', null, keyInput), useKey))),
    h('section.panel', null,
      h('div.panel-head', null, h('div', null, title, h('p', null, 'What the company’s own system knows goes in; the price comes back.')),
        h('div.actions', null, h('div', null, picker), h('label.check', null, live, h('span.muted', null, 'Ask again every 4 s')))),
      h('div.panel-body.form', null, controls, grid)),
    h('section.panel', null, h('div.panel-head', null, h('div', null, h('h2', null, 'What the app just sent'),
      h('p', null, 'Change a rule or switch the engine off in the admin console and watch the next answer.'))), h('div.panel-body', null, wire)),
  ];
}

const PAGES = { prices, key, app: sampleApp, performance: performancePage, reference };

async function route() {
  if (!frame) return;
  const name = location.hash.replace(/^#\/?/, '').split('/')[0];
  const page = PAGES[name] ? name : 'prices';
  frame.mark(page);
  const view = h('div.page', null, loading());
  const mine = ++visit;
  replace(frame.main, view);
  try {
    const content = await PAGES[page]();
    if (mine === visit) replace(view, content);
  } catch (err) {
    if (mine === visit) replace(view, errorBox(err, 'This page couldn’t be loaded'));
  }
  window.scrollTo(0, 0);
}

window.addEventListener('hashchange', route);
window.addEventListener('m360:reload', route);
window.addEventListener('m360:signed-out', () => { session.clear(); signIn('Your key stopped working. It may have been revoked or have expired.'); });

if (!session.key) signIn();
else {
  replace(root, h('div.signin', null, loading()));
  api('GET', '/v1/me').then((me) => { session.me = me; open(); }).catch((err) => { if (err.status !== 401) signIn(err.message); });
}
