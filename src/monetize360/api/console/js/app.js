// Admin console entry point: sign-in, the shared frame and the hash router.
import { api, session, h, icon, replace, loading, errorBox, shell, signInCard } from './core.js';
import { overview, limits, keys, history } from './pages.js';
import { domains, domain, version } from './packs.js';
import { performancePage } from './perf.js';

const root = document.getElementById('app');
const NAV = [
  ['overview', 'Overview', 'layout-dashboard'],
  ['domains', 'Domains', 'layers'],
  ['limits', 'Limits', 'shield-check'],
  ['keys', 'API keys', 'key-round'],
  ['history', 'Price history', 'history'],
  ['performance', 'Performance', 'chart-column'],
];
const LINKS = [['/portal', 'Developer portal', 'file-code'], ['/docs', 'API reference', 'file-text', true]];
const PAGES = {
  overview,
  domains: (parts) => (parts.length === 0 ? domains() : parts.length === 1 ? domain(parts[0]) : version(parts[0], parts[1])),
  limits, keys, history, performance: performancePage,
};

let frame = null;
let visit = 0;

function signIn(message) {
  frame = null;
  replace(root, signInCard({
    title: 'Admin console', text: 'Sign in with an admin API key for your organisation.', action: 'Open console', message,
    hint: 'The key is kept in this browser tab only and is sent to this server and nowhere else. Closing the tab signs you out.',
    onKey: async (key) => {
      const me = await api('GET', '/v1/me', undefined, { key });
      if (me.role !== 'admin') return `This key has the role “${me.role}”. The console needs an admin key. The developer portal at /portal accepts any key.`;
      session.set(key);
      session.me = me;
      open();
      return null;
    },
  }));
}

function open() {
  frame = shell({ tag: 'Admin', nav: NAV, links: LINKS, onSignOut: () => { session.clear(); signIn(); } });
  replace(root, frame.el);
  route();
}

async function route() {
  if (!frame) return;
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  const page = PAGES[parts[0]] ? parts[0] : 'overview';
  frame.mark(page);
  const view = h('div.page', null, loading());
  const mine = ++visit;
  replace(frame.main, view);
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
  signIn('Your key stopped working. It may have been revoked or have expired.');
});

if (!session.key) signIn();
else {
  replace(root, h('div.signin', null, loading()));
  api('GET', '/v1/me').then((me) => {
    if (me.role !== 'admin') { signIn(); return; }      // a portal key is in this tab: ask for an admin key, keep theirs
    session.me = me;
    open();
  }).catch((err) => { if (err.status !== 401) signIn(err.message); });
}
