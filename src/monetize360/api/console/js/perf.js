// The Performance page, shared by the admin console and the developer portal.
// Two clocks, kept apart: "round trip" is measured in this browser (network included);
// "server" is what the server itself reports in each response's Server-Timing header.
import { api, calls, h, icon, replace, badge, note, empty, run, ms, count, duration } from './core.js';

const reload = () => window.dispatchEvent(new Event('m360:reload'));
const sorted = (list) => [...list].sort((a, b) => a - b);
const at = (list, q) => (list.length ? sorted(list)[Math.max(0, Math.ceil(q * list.length) - 1)] : null);

function bar(value, max, tone = '') {
  const fill = h('span', { class: tone });
  fill.style.width = `${Math.max(2, Math.min(100, (value / (max || 1)) * 100))}%`;
  return h('div.bar', { 'aria-hidden': 'true' }, fill);
}

const tile = (glyph, label, value, text) => h('div.panel.tile', null,
  h('div.label', null, icon(glyph), label), h('div.value', null, value), h('div.note', null, text));

// A valid request for a domain, built from what GET /v1/domains says it takes.
function sampleRequest(d) {
  const signals = {};
  for (const s of d.signals) {
    if (!s.required) continue;
    signals[s.name] = s.type === 'enum' ? s.values[0] : s.type === 'bool' ? true
      : s.range ? (s.type === 'int' ? Math.round((Number(s.range[0]) + Number(s.range[1])) / 2) : String((Number(s.range[0]) + Number(s.range[1])) / 2)) : '1';
  }
  return d.needs_base_price ? { base_price: '1000', signals } : { signals };
}

export async function performancePage() {
  const [board, { domains }] = await Promise.all([api('GET', '/v1/scoreboard'), api('GET', '/v1/domains')]);
  const mine = calls.filter((c) => c.status > 0);
  const trips = mine.map((c) => c.ms);
  const inside = mine.filter((c) => c.serverMs !== null).map((c) => c.serverMs);

  // this session, by endpoint
  const groups = new Map();
  for (const c of mine) {
    const key = `${c.method} ${c.route}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  const rows = [...groups.entries()].map(([key, list]) => ({
    key, n: list.length, p50: at(list.map((c) => c.ms), 0.5), p95: at(list.map((c) => c.ms), 0.95), max: Math.max(...list.map((c) => c.ms)),
    server: at(list.filter((c) => c.serverMs !== null).map((c) => c.serverMs), 0.5),
  })).sort((a, b) => b.n - a.n);
  const widest = Math.max(1, ...rows.map((r) => r.p95));

  const measure = h('button.btn.primary', { type: 'button', disabled: domains.length === 0,
    title: domains.length ? 'Sends 20 price requests that are not recorded' : 'No live domain for this key', onclick: () => run(measure, async () => {
      const d = domains[0];
      const body = sampleRequest(d);
      for (let i = 0; i < 20; i += 1) await api('POST', `/v1/simulate/${d.domain}`, body);
      for (let i = 0; i < 10; i += 1) await api('GET', '/v1/me');
      reload();
    }) }, icon('zap'), 'Measure now');

  const http = board.live.http;
  const engine = board.live.engine_ms_at_most;
  const m = board.measured;
  const within = (v) => (v === null || v === undefined ? '—' : `≤ ${ms(v)}`);
  const widestServer = Math.max(1, ...(http || []).map((r) => r.mean_ms));

  return [
    h('div.page-head', null,
      h('div', null, h('h1', null, 'Performance'),
        h('p', null, 'How long every API call takes. Round trip is timed in this browser and includes the network. Server time is reported by the server itself in each response.')),
      h('div.actions', null, h('button.btn', { type: 'button', onclick: reload }, icon('refresh-cw'), 'Refresh'), measure)),

    h('div.tiles', null,
      tile('send', 'Calls from this tab', count(mine.length), 'since you signed in'),
      tile('clock', 'Typical round trip', trips.length ? ms(at(trips, 0.5)) : '—', 'half of the calls were faster'),
      tile('chart-column', 'Slow end', trips.length ? ms(at(trips, 0.95)) : '—', '95% of the calls were faster'),
      tile('server', 'Typical server time', inside.length ? ms(at(inside, 0.5)) : '—', inside.length ? 'time spent inside the server' : 'not reported by this server')),

    h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, 'This tab, by endpoint'), h('p', null, 'Every call this page made, grouped. The bar is the 95% round trip.'))),
      rows.length === 0 ? empty('clock', 'No calls yet', 'Press “Measure now”, or use the other pages and come back.')
        : h('div.table-wrap', null, h('table', null,
          h('thead', null, h('tr', null, h('th', null, 'Endpoint'), h('th.right', null, 'Calls'), h('th.right', null, 'Typical'), h('th.right', null, '95%'),
            h('th.right', null, 'Slowest'), h('th.right', null, 'Server'), h('th', null, ''))),
          h('tbody', null, rows.map((r) => h('tr', null,
            h('td.mono', null, r.key), h('td.right.num', null, count(r.n)), h('td.right.num', null, h('strong', null, ms(r.p50))),
            h('td.right.num', null, ms(r.p95)), h('td.right.num.muted', null, ms(r.max)), h('td.right.num.muted', null, ms(r.server)),
            h('td.bar-cell', null, bar(r.p95, widest, r.p95 > 250 ? 'warn' : '')))))))),

    h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, 'Inside the server, every caller'),
        h('p', null, `Counted by this server since it started ${duration(board.live.uptime_seconds)} ago: authentication, database, engine and audit record together.`))),
      !Array.isArray(http) ? h('div.panel-body', null, note('', 'info', 'This server was started before per-route timing was added. Restart it to see this table.'))
        : http.length === 0 ? empty('server', 'No API calls yet', 'Nothing has called this server since it started.')
          : h('div.table-wrap', null, h('table', null,
            h('thead', null, h('tr', null, h('th', null, 'Endpoint'), h('th.right', null, 'Calls'), h('th.right', null, 'Average'),
              h('th.right', null, 'Half within'), h('th.right', null, '95% within'), h('th', null, ''))),
            h('tbody', null, http.map((r) => h('tr', null,
              h('td.mono', null, `${r.method} ${r.route}`), h('td.right.num', null, count(r.calls)), h('td.right.num', null, h('strong', null, ms(r.mean_ms))),
              h('td.right.num', null, within(r.p50_ms_at_most)), h('td.right.num', null, within(r.p95_ms_at_most)),
              h('td.bar-cell', null, bar(r.mean_ms, widestServer, r.mean_ms > 250 ? 'warn' : '')))),
            engine && engine.p50 !== null && h('tr', null,
              h('td', null, 'Pricing engine alone ', h('span.muted', null, '(rules and explanation, no database)')), h('td.right.num', null, count(board.live.prices.ok + board.live.simulations.ok)),
              h('td.right.num.muted', null, '—'), h('td.right.num', null, within(engine.p50)), h('td.right.num', null, within(engine.p95)), h('td')))))),

    m && h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, 'Under load'), h('p', null, `Measured with a load generator on ${m.measured_on}. ${m.machine}.`)),
        badge('Measured, not estimated', 'info')),
      h('div.panel-body.form', null,
        h('div.facts', null,
          h('div.fact', null, h('b', null, `${m.latency_light_load.p50_ms} ms`), h('span', null, 'typical price, with its audit record')),
          h('div.fact', null, h('b', null, `${m.latency_light_load.p95_ms} ms`), h('span', null, '95% of prices')),
          h('div.fact', null, h('b', null, `${m.latency_light_load.p99_ms} ms`), h('span', null, '99% of prices')),
          h('div.fact', null, h('b', null, `${count(m.latency_light_load.req_per_s)} / s`), h('span', null, 'at that latency, 4 workers')),
          h('div.fact', null, h('b', null, `${count(m.load_test.real_prices_5_audit_chains.workers_8.median)} / s`), h('span', null, 'recorded prices at full load, 8 workers')),
          h('div.fact', null, h('b', null, `${count(m.load_test.simulate_no_writes.workers_8)} / s`), h('span', null, 'engine and API without the audit write'))),
        note('', 'info', m.caveat))),

    h('section.panel', null,
      h('div.panel-head', null, h('div', null, h('h2', null, 'Latest calls from this tab'), h('p', null, 'Newest first.'))),
      mine.length === 0 ? empty('send', 'No calls yet')
        : h('div.table-wrap', null, h('table', null,
          h('thead', null, h('tr', null, h('th', null, 'Time'), h('th', null, 'Call'), h('th', null, 'Status'), h('th.right', null, 'Round trip'), h('th.right', null, 'Server'))),
          h('tbody', null, mine.slice(0, 25).map((c) => h('tr', null,
            h('td.nowrap.muted', null, c.at.toLocaleTimeString()), h('td.mono', null, `${c.method} ${c.path}`),
            h('td', null, badge(String(c.status), c.status < 400 ? 'ok' : 'danger')),
            h('td.right.num', null, h('strong', null, ms(c.ms))), h('td.right.num.muted', null, ms(c.serverMs)))))))),
  ];
}
