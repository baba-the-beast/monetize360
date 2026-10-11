// Entry point: sign-in, the page frame (sidebar + status) and the hash router.
import { api, session, h, icon, replace, loading, errorBox, run } from './core.js';
import { overview, limits, keys, history } from './pages.js';
import { domains, domain, version } from './packs.js';

const root = document.getElementById('app');
const NAV = [
  ['overview', 'Overview', 'layout-dashboard'],
  ['domains', 'Domains', 'layers'],
  ['limits', 'Limits', 'shield-check'],
  ['keys', 'API keys', 'key-round'],
  ['history', 'Price history', 'history'],
];
const PAGES = {
  overview,
  domains: (parts) => (parts.length === 0 ? domains() : parts.length === 1 ? domain(parts[0]) : version(parts[0], parts[1])),
  limits, keys, history,
};

let main = null;
let statusTimer = null;
let visit = 0;

// ---------- sign in ----------

function signIn(message) {
  clearInterval(statusTimer);
  const problem = h('div');
  if (message) replace(problem, errorBox({ message }, 'Signed out'));
  const input = h('input', { type: 'password', id: 'key', autocomplete: 'off', spellcheck: false,
    placeholder: 'm360_live_…', required: true });
  const show = h('button.btn.quiet', { type: 'button', 'aria-label': 'Show or hide the key',
    onclick: () => { input.type = input.type === 'password' ? 'text' : 'password'; } }, icon('eye'));
  const submit = h('button.btn.primary', { type: 'submit' }, icon('arrow-right'), 'Open console');

  const form = h('form.panel', { onsubmit: (e) => {
    e.preventDefault();
    const key = input.value.trim();
    run(submit, async () => {
      try {
        const me = await api('GET', '/v1/me', undefined, { key });
        if (me.role !== 'admin') {
          replace(problem, errorBox({ message: `This key has the role “${me.role}”. The console needs an admin key.` },
            'Not an admin key'));
          return;
        }
        session.set(key);
        session.me = me;
        frame();
      } catch (err) {
        replace(problem, errorBox(err.status === 401
          ? { message: 'That key isn’t valid, or it has expired or been revoked.' } : err, 'Couldn’t sign in'));
      }
    });
  } },
  h('div', null, h('h1', null, 'Admin console'),
    h('p.muted', null, 'Sign in with an admin API key for your organisation.')),
  h('label.field', { htmlFor: 'key' }, h('span', null, 'API key'), h('div.key-input', null, input, show)),
  problem, submit,
  h('p.small.muted', null, 'The key is kept in this browser tab only and is sent to this server and nowhere else. ',
    'Closing the tab signs you out.'));

  replace(root, h('div.signin', null, h('div.signin-card', null,
    h('div.brand', null, h('img', { src: 'favicon.svg', alt: '' }), 'Monetize360'), form)));
  input.focus();
}

// ---------- frame ----------

function frame() {
  const me = session.me;
  const status = h('div.sys', null, h('span.dot'), h('span', null, 'Checking…'));
  main = h('main');
  replace(root, h('div.shell', null,
    h('aside.side', null,
      h('div.brand', null, h('img', { src: 'favicon.svg', alt: '' }), 'Monetize360'),
      h('nav.nav', { 'aria-label': 'Sections' }, NAV.map(([id, label, glyph]) =>
        h('a', { href: `#/${id}`, 'data-page': id, title: label }, icon(glyph), h('span', null, label)))),
      h('div.side-foot', null,
        status,
        h('div.who', null, h('strong', null, me.tenant_id), h('span.small.muted', null, `Key ${me.key_id} · ${me.role}`)),
        h('button.btn.sm', { type: 'button', onclick: () => { session.clear(); signIn(); } }, icon('log-out'), 'Sign out'))),
    main));

  const check = async () => {
    let tone = 'danger', text = 'Server unreachable';
    try {
      const r = await api('GET', '/readyz', undefined, { anyStatus: true });
      if (r && r.database === 'down') text = 'Database unreachable · prices still served';
      else if (r && !r.ready) text = 'Server not ready';
      else if (r && r.redis === 'down') { tone = 'warn'; text = 'Broadcast offline · changes take up to 10 s'; }
      else if (r) { tone = 'ok'; text = 'All systems normal'; }
    } catch { /* keep "unreachable" */ }
    replace(status, h('span.dot', { class: tone }), h('span', null, text));
  };
  check();
  clearInterval(statusTimer);
  statusTimer = setInterval(check, 15000);
  route();
}

// ---------- router ----------

async function route() {
  if (!main) return;
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  const page = PAGES[parts[0]] ? parts[0] : 'overview';
  for (const a of root.querySelectorAll('.nav a')) {
    if (a.dataset.page === page) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  const view = h('div.page', null, loading());
  const mine = ++visit;
  replace(main, view);
  try {
    const content = await PAGES[page](parts.slice(1));
    if (mine === visit) replace(view, content);        // ignore a page that finished after the user moved on
  } catch (err) {
    if (mine === visit) {
      replace(view, errorBox(err, 'This page couldn’t be loaded'),
        h('div.actions', null, h('button.btn', { type: 'button', onclick: route }, icon('refresh-cw'), 'Try again')));
    }
  }
  window.scrollTo(0, 0);
}

window.addEventListener('hashchange', route);
window.addEventListener('m360:reload', route);
window.addEventListener('m360:signed-out', () => {
  session.clear();
  main = null;
  signIn('Your key stopped working. It may have been revoked or have expired.');
});

// ---------- start ----------

if (!session.key) signIn();
else {
  replace(root, h('div.signin', null, loading()));
  api('GET', '/v1/me').then((me) => {
    if (me.role !== 'admin') { session.clear(); signIn(); return; }
    session.me = me;
    frame();
  }).catch((err) => { if (err.status !== 401) signIn(err.message); });
}
