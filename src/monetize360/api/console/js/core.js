// Shared pieces: the API client, the element builder, dialogs, toasts and formatters.
// Nothing here (or anywhere in the console) uses innerHTML: every value from the server becomes a text node.

const STORE = 'm360.key';
const read = () => { try { return sessionStorage.getItem(STORE) || ''; } catch { return ''; } };

// The key lives in this tab only (sessionStorage): closing the tab forgets it.
export const session = {
  key: read(),
  me: null,
  set(key) { this.key = key; try { sessionStorage.setItem(STORE, key); } catch { /* private mode: memory only */ } },
  clear() { this.key = ''; this.me = null; try { sessionStorage.removeItem(STORE); } catch { /* nothing stored */ } },
};

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.message) || `The server answered ${status}`);
    this.status = status;
    this.code = (body && body.error) || 'error';
    this.details = (body && Array.isArray(body.details)) ? body.details : [];
  }
}

// api('POST', '/v1/admin/kill', {domain, mode}) -> parsed JSON, or throws ApiError.
// opts.key: use this key for one call instead of the signed-in one. opts.yaml: send text as application/yaml.
// opts.raw: return the Response (for file downloads). opts.anyStatus: don't throw on an error status.
export async function api(method, path, body, opts = {}) {
  const headers = {};
  const key = opts.key || session.key;
  if (key) headers.Authorization = `Bearer ${key}`;
  let payload;
  if (opts.yaml !== undefined) { headers['Content-Type'] = 'application/yaml'; payload = opts.yaml; }
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, cache: 'no-store' });
  } catch {
    throw new ApiError(0, { error: 'network', message: "Can't reach the server. Check that it is running." });
  }
  if (opts.raw && res.ok) return res;
  const data = await res.json().catch(() => null);
  if (!res.ok && !opts.anyStatus) {
    // the signed-in key stopped working (revoked or expired): back to the sign-in screen
    if (res.status === 401 && !opts.key && session.key) window.dispatchEvent(new Event('m360:signed-out'));
    throw new ApiError(res.status, data);
  }
  return data;
}

// ---------- elements ----------

// h('button.btn.primary', {onclick, disabled}, icon('check'), 'Save')
export function h(tag, attrs, ...kids) {
  const [name, ...classes] = tag.split('.');
  const el = document.createElement(name || 'div');
  if (classes.length) el.className = classes.join(' ');
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.classList.add(...String(v).split(' ').filter(Boolean));
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (!k.includes('-') && k in el) el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, kids);
  return el;
}

export function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function replace(el, ...kids) { el.replaceChildren(); return append(el, kids); }

const SVG = 'http://www.w3.org/2000/svg';
export function icon(name, cls = '') {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', `icon ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', `icons.svg#${name}`);
  svg.append(use);
  return svg;
}

export const badge = (text, tone = '', iconName = '') => h('span.badge', { class: tone }, iconName && icon(iconName), text);
export const note = (tone, iconName, ...kids) => h('div.note-box', { class: tone }, icon(iconName), h('div', null, ...kids));
export const loading = (text = 'Loading') => h('div.loading', null, icon('loader-circle', 'spin'), text);
export const empty = (iconName, title, text, action) =>
  h('div.empty', null, icon(iconName), h('strong', null, title), text && h('span', null, text), action);

// An error from the API as a readable box, with every detail the server listed.
export function errorBox(err, title) {
  const details = (err && err.details) || [];
  return note('danger', 'circle-alert',
    h('strong', null, title || 'That didn’t work'),
    h('span', null, err && err.message ? err.message : String(err)),
    details.length > 0 && h('ul.problems', null, details.slice(0, 30).map((d) => h('li', null,
      d.path && h('code', null, `${d.path} `), d.message || (d.value !== undefined ? JSON.stringify(d.value) : '')))));
}

// Disable a button and show a spinner while its action runs. Errors are shown as a toast unless handled.
export async function run(button, action) {
  const first = button.firstChild;
  const spinner = icon('loader-circle', 'spin');
  button.disabled = true;
  if (first instanceof SVGElement) first.replaceWith(spinner); else button.prepend(spinner);
  try {
    return await action();
  } catch (err) {
    toast(err.message || String(err), 'danger');
    return undefined;
  } finally {
    button.disabled = false;
    if (first instanceof SVGElement) spinner.replaceWith(first); else spinner.remove();
  }
}

export function toast(text, tone = '') {
  const el = h('div.toast', { class: tone }, icon(tone === 'danger' ? 'circle-alert' : 'check'), text);
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), tone === 'danger' ? 7000 : 3500);
}

// A modal built on <dialog>: focus is trapped and Esc closes it, natively.
// build(close) returns {body, foot}; the promise resolves with whatever close(value) was given.
export function dialog(title, build, { wide = false } = {}) {
  return new Promise((resolve) => {
    let result;
    const el = h('dialog', { class: wide ? 'wide' : '' });
    const close = (value) => { result = value; el.close(); };
    const { body, foot } = build(close);
    append(el, [
      h('div.dlg-head', null, h('h2', null, title),
        h('button.btn.quiet.sm', { type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
      h('div.dlg-body', null, body),
      foot && h('div.dlg-foot', null, foot),
    ]);
    el.addEventListener('close', () => { el.remove(); resolve(result); });
    document.body.append(el);
    el.showModal();
  });
}

export function confirm({ title, text, action = 'Confirm', danger = false, tone = 'info', iconName = 'info' }) {
  return dialog(title, (close) => ({
    body: note(tone, iconName, text),
    foot: [
      h('button.btn', { type: 'button', onclick: () => close(false) }, 'Cancel'),
      h('button.btn.primary', { type: 'button', class: danger ? 'danger' : '', onclick: () => close(true) }, action),
    ],
  })).then(Boolean);
}

// ---------- formatting ----------

const SYMBOL = { INR: '₹', USD: '$', EUR: '€', GBP: '£', JPY: '¥' };

// Money arrives as a decimal string and is shown as one: no float ever touches it.
export function money(value, currency) {
  if (value === null || value === undefined || value === '') return '—';
  const text = String(value);
  const negative = text.startsWith('-');
  const [whole, cents] = text.replace('-', '').split('.');
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const symbol = currency ? (SYMBOL[currency] || `${currency} `) : '';
  return `${negative ? '−' : ''}${symbol}${grouped}${cents !== undefined ? `.${cents}` : ''}`;
}

const DATE = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const FULL = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
export const when = (iso) => (iso ? DATE.format(new Date(iso)) : '—');
export const whenFull = (iso) => (iso ? FULL.format(new Date(iso)) : '—');
export const count = (n) => Number(n || 0).toLocaleString();

export function duration(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied'); } catch { toast('Select the text and copy it by hand', 'danger'); }
}

// ---------- the pricing switch, used on Domains and on a domain's page ----------

export const MODES = {
  normal: { label: 'Dynamic', icon: 'zap', tone: 'ok',
    explain: 'Prices follow the live rules.' },
  base_only: { label: 'Base price only', icon: 'circle-minus', tone: 'warn',
    explain: 'Rules are switched off. Customers pay the base price; floors, ceilings and rounding still apply.' },
  freeze: { label: 'Frozen', icon: 'snowflake', tone: 'info',
    explain: 'Every item keeps the last price it was given. An item that never had a price gets none until you switch back.' },
};

export const modeBadge = (mode) => badge(MODES[mode].label, MODES[mode].tone, MODES[mode].icon);

// A three-way switch. Asks before changing, calls the kill-switch API, then onChange(newMode).
export function modeSwitch(domain, current, live, onChange) {
  const group = h('div.seg', { role: 'radiogroup', 'aria-label': `Pricing for ${domain}` });
  const draw = (mode) => replace(group, Object.entries(MODES).map(([id, m]) => h('button', {
    type: 'button', role: 'radio', class: `m-${id}`, 'aria-checked': String(id === mode), disabled: !live,
    title: live ? m.explain : 'Publish a version first',
    onclick: async (e) => {
      if (id === mode) return;
      const button = e.currentTarget;          // gone from the event once we await
      const yes = await confirm({
        title: `Switch ${domain} to “${m.label}”?`, text: `${m.explain} This takes effect on every server at once.`,
        action: `Switch to ${m.label}`, tone: m.tone, iconName: m.icon,
      });
      if (!yes) return;
      await run(button, async () => {
        await api('POST', '/v1/admin/kill', { domain, mode: id });
        toast(`${domain}: ${m.label.toLowerCase()}`);
        draw(id);
        if (onChange) onChange(id);
      });
    },
  }, icon(m.icon), m.label)));
  draw(current);
  return group;
}
