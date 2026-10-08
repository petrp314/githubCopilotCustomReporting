import test from 'node:test';
import assert from 'node:assert/strict';
import {
  validDay, filterRows, distinctsUnavailable, visibleData, sumDecimals, compareDecimals,
  displayValue, billingSummary, tokenSummary, csvCell, toCsv, freshness, sourceFreshness, barPercent,
} from '../web/data-utils.js';

const period = { start: '2026-10-01', end: '2026-10-07', timezone: 'UTC' };
const rows = [
  { day: '2026-10-01', model: 'dynamic-model-v2', cost_center_id: 'engineering', input: '0' },
  { day: '2026-10-03', model: 'automatic', cost_center_id: 'engineering', input: null },
  { day: '2026-10-07', model: 'dynamic-model-v2', cost_center_id: 'enterprise-only', input: '3' },
];

test('filters inclusive UTC dates and the exact supported model and center dimensions', () => {
  assert.deepEqual(filterRows(rows, { start: '2026-10-01', end: '2026-10-03', center: 'engineering' }), rows.slice(0, 2));
  assert.deepEqual(filterRows(rows, { model: 'dynamic-model-v2', center: 'enterprise-only' }), [rows[2]]);
  assert.deepEqual(filterRows(rows, { model: 'missing' }), []);
  assert.deepEqual(filterRows(rows, { start: '2026-10-08' }), []);
  assert.equal(validDay('2026-02-30'), false);
  assert.equal(validDay('2024-02-29'), true);
  assert.equal(validDay('2026-10-01T00:00:00Z'), false);
});

test('unreported dimensions are selectable without conflating them with all dimensions', () => {
  const data = [{ day: period.start }, ...rows];
  assert.deepEqual(filterRows(data, { model: null }), [data[0]]);
  assert.deepEqual(filterRows(data, { center: null }), [data[0]]);
  assert.equal(filterRows(data, {}).length, 4);
});

test('unsupported dimensions never imply a join to enterprise daily or model telemetry', () => {
  const report = { period, daily: rows, models: rows, billing: rows, tokens: rows };
  const view = visibleData(report, { model: 'dynamic-model-v2' });
  assert.deepEqual(view.daily, []);
  assert.equal(view.models.length, 2);
  const centered = visibleData(report, { center: 'engineering' });
  assert.deepEqual(centered.daily, []);
  assert.deepEqual(centered.models, []);
  assert.equal(centered.billing.length, 2);
  assert.equal(centered.tokens.length, 2);
});

test('whole-family privacy suppression cannot be bypassed by filtering', () => {
  const report = { period, privacy: { breakdowns_suppressed: true }, daily: rows, models: rows, billing: rows, tokens: rows };
  const data = visibleData(report, { model: 'automatic' });
  for (const name of ['daily', 'models', 'billing', 'tokens']) assert.deepEqual(data[name], []);
  assert.equal(data.suppressed, true);
});

test('per-family privacy suppression preserves independently approved billing', () => {
  const report = {
    period, privacy: { breakdowns_suppressed: true, suppressed_families: ['daily', 'models', 'tokens', 'overview'] },
    daily: rows, models: rows, billing: rows, tokens: rows,
  };
  const data = visibleData(report, { center: 'engineering' });
  for (const name of ['daily', 'models', 'tokens']) assert.deepEqual(data[name], []);
  assert.deepEqual(data.billing, rows.slice(0, 2));
  assert.equal(data.overviewSuppressed, true);
  assert.deepEqual(data.suppressedFamilies, ['daily', 'models', 'tokens', 'overview']);
  report.privacy.suppressed_families = [];
  const approved = visibleData(report);
  assert.equal(approved.suppressed, false);
  for (const name of ['daily', 'models', 'billing', 'tokens']) assert.equal(approved[name].length, rows.length);
});

test('family list is authoritative even if legacy suppression flag is absent or false', () => {
  const report = { period, privacy: { suppressed_families: ['tokens'] }, billing: rows, tokens: rows };
  assert.equal(visibleData(report).billing.length, rows.length);
  assert.deepEqual(visibleData(report).tokens, []);
  report.privacy.breakdowns_suppressed = false;
  assert.deepEqual(visibleData(report).tokens, []);
});

test('unique counts and adoption are unavailable for narrower windows or dimensional filters', () => {
  const report = { period };
  assert.equal(distinctsUnavailable(report), false);
  assert.equal(distinctsUnavailable(report, { start: period.start, end: period.end }), false);
  assert.equal(distinctsUnavailable(report, { start: '2026-10-02' }), true);
  assert.equal(distinctsUnavailable(report, { end: '2026-10-06' }), true);
  assert.equal(distinctsUnavailable(report, { model: 'automatic' }), true);
  assert.equal(distinctsUnavailable(report, { center: 'engineering' }), true);
  assert.equal(distinctsUnavailable(report, { center: null }), true);
  assert.equal(distinctsUnavailable({}), true);
});

test('unknown is distinct from real zero in rendering and exact totals', () => {
  for (const value of [null, undefined, '', NaN, Infinity]) assert.equal(displayValue(value), 'Unavailable');
  assert.equal(displayValue(0), '0');
  assert.equal(displayValue('0.00'), '0.00');
  assert.equal(sumDecimals(['0', '0.00']), '0.00');
  assert.equal(sumDecimals(['1.00', null]), null);
  assert.equal(sumDecimals([]), null);
  assert.equal(sumDecimals(['1', 'unavailable']), null);
  assert.equal(sumDecimals(['1e3']), null);
});

test('fixed-decimal BigInt arithmetic preserves cents and large values exactly', () => {
  assert.equal(sumDecimals(['0.1', '0.2']), '0.3');
  assert.equal(sumDecimals(['9007199254740993.01', '0.09']), '9007199254740993.10');
  assert.equal(sumDecimals(['12.500', '-2.5', '0.0001']), '10.0001');
  assert.equal(sumDecimals(['-0.1', '0.10']), '0.00');
  assert.equal(compareDecimals('9007199254740993.01', '9007199254740993.00'), 1);
  assert.equal(compareDecimals('1.00', '1'), 0);
  assert.equal(displayValue('9007199254740993.10'), '9,007,199,254,740,993.10');
  assert.equal(barPercent('25.0', '100'), 25);
  assert.equal(barPercent(null, '100'), 0);
  assert.equal(barPercent('0', '0'), 0);
});

test('billing aggregates never mix unit, currency, source, product, SKU or attribution', () => {
  const base = {
    day: period.start, model: 'automatic', cost_center_id: 'enterprise-only', unit: 'AI credits',
    currency: 'USD', product: 'Copilot', sku: 'usage', source: 'billing', attribution: 'source-billed',
    gross_quantity: '2', discount_quantity: '1', net_quantity: '1',
    gross_amount: '0.20', discount_amount: '0.10', net_amount: '0.10',
  };
  const data = [
    base, { ...base, day: period.end, net_amount: '0.20' },
    { ...base, currency: 'EUR' }, { ...base, unit: 'Premium requests' },
    { ...base, source: 'other' }, { ...base, product: 'other' },
    { ...base, sku: 'other' }, { ...base, attribution: 'current-only' },
  ];
  const result = billingSummary(data);
  assert.equal(result.length, 7);
  assert.equal(result[0].net_amount, '0.30');
  assert.equal(result[0].net_quantity, '2');
  assert.equal(result[0].start, period.start);
  assert.equal(result[0].end, period.end);
});

test('token categories stay separate and missing categories are never inferred as zero', () => {
  const data = [
    { ...rows[0], output: '10', cache_read: null, cache_write: '2', source: 'ai-report' },
    { ...rows[0], input: '5', output: '20', cache_read: '1', cache_write: '3', source: 'ai-report' },
  ];
  const [result] = tokenSummary(data);
  assert.equal(result.input, '5');
  assert.equal(result.output, '30');
  assert.equal(result.cache_read, null);
  assert.equal(result.cache_write, '5');
  assert.equal(Object.hasOwn(result, 'total'), false);
  assert.equal(Object.hasOwn(result, 'cli_prompt_tokens'), false);
});

test('billing summary does not assume that missing units or currencies are comparable', () => {
  const [result] = billingSummary([
    { day: period.start, net_quantity: '1', net_amount: '0.10' },
    { day: period.end, net_quantity: '2', net_amount: '0.20' },
  ]);
  assert.equal(result.net_quantity, null);
  assert.equal(result.net_amount, null);
});

test('CSV neutralizes formulas and leading whitespace/control characters, and escapes CSV quotes', () => {
  for (const value of ['=1+1', '+SUM(A1)', '-1+2', '@SUM(A1)', ' =cmd', '\t=cmd', '\r\n@cmd', '\u0000=cmd', '\u0085=cmd', '  text']) {
    assert.equal(csvCell(value).startsWith('"\''),
      true, `must neutralize ${JSON.stringify(value)}`);
  }
  assert.equal(csvCell('model,"quoted"'), '"model,""quoted"""');
  assert.equal(csvCell('automatic'), '"automatic"');
  assert.equal(csvCell('0'), '"0"');
  assert.equal(csvCell(null), '"Unavailable"');
});

test('CSV exports only requested visible rows and explicit columns, including source and date context', () => {
  const selected = filterRows(rows, { model: 'automatic' });
  const csv = toCsv(selected.map(row => ({ ...row, source: 'ai-report', unit: 'input tokens', currency: null, hidden: 'never export this' })),
    ['day', 'model', 'source', 'unit', 'currency', 'input']);
  assert.equal(csv.includes('dynamic-model-v2'), false);
  assert.equal(csv.includes('never export this'), false);
  assert.equal(csv.includes('"2026-10-03","automatic","ai-report","input tokens","Unavailable","Unavailable"'), true);
});

test('freshness warns after 48 hours and on collection errors with retained snapshots', () => {
  const now = Date.parse('2026-10-08T12:00:00Z');
  assert.deepEqual(freshness({ generated_at: '2026-10-06T12:00:00Z' }, now), []);
  assert.equal(freshness({ generated_at: '2026-10-06T11:59:59Z' }, now).length, 1);
  assert.equal(freshness({ generated_at: '2026-10-08T12:00:00Z', sources: [{ status: 'error' }] }, now).length, 1);
  assert.equal(freshness({}, now).length, 1);
});

test('source age remains stale even when the report was newly generated', () => {
  const now = Date.parse('2026-10-08T12:00:00Z');
  const sources = [{ id: 'usage', status: 'ok', last_successful_collection: '2026-10-06T11:59:59Z' }];
  const messages = freshness({ generated_at: '2026-10-08T12:00:00Z', sources }, now);
  assert.equal(messages.length, 1);
  assert.match(messages[0], /usage:.*source collection is more than 48 hours old/);
  assert.equal(sourceFreshness(sources[0], now).stale, true);
  sources[0].last_successful_collection = '2026-10-06T12:00:00Z';
  assert.deepEqual(freshness({ generated_at: '2026-10-08T12:00:00Z', sources }, now), []);
});

test('unavailable source with a retained snapshot is stale, but never-collected source is not', () => {
  const now = Date.parse('2026-10-08T12:00:00Z');
  const source = { id: 'billing', status: 'unavailable', message: 'Permission denied (403)', last_successful_collection: '2026-10-08T11:00:00Z' };
  const messages = freshness({ generated_at: '2026-10-08T12:00:00Z', sources: [source] }, now);
  assert.equal(messages.length, 1);
  assert.match(messages[0], /billing: Source unavailable; retained data/);
  assert.equal(sourceFreshness(source, now).stale, true);
  source.last_successful_collection = null;
  assert.deepEqual(freshness({ generated_at: '2026-10-08T12:00:00Z', sources: [source] }, now), []);
  assert.equal(sourceFreshness(source, now).stale, false);
  assert.match(sourceFreshness(source, now).message, /Unavailable/);
});

test('missing optional arrays are tolerated without fabricating data', () => {
  const data = visibleData({ schema_version: 1 }, {});
  for (const name of ['daily', 'models', 'billing', 'tokens']) assert.deepEqual(data[name], []);
  assert.deepEqual(filterRows([null, 'bad', []]), []);
});

test('application renders safely, suppresses unsupported filter grains, and exports only visible tables', async () => {
  class Element {
    constructor(tag) {
      this.tagName = tag;
      this.children = [];
      this.attributes = {};
      this.listeners = new Map();
      this.value = '';
      this.hidden = false;
      this.text = '';
    }
    set textContent(value) { this.text = String(value); this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
    set innerHTML(_) { assert.fail('Untrusted values must never be rendered with innerHTML'); }
    set defaultValue(value) { this.value = value; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.text = ''; this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, listener) { this.listeners.set(name, listener); }
    checkValidity() { return true; }
    remove() {}
    click() {}
  }
  const elements = new Map();
  const originalDocument = globalThis.document;
  const originalFetch = globalThis.fetch;
  const originalCreateUrl = URL.createObjectURL;
  const originalRevokeUrl = URL.revokeObjectURL;
  let exportedBlob;
  const maliciousModel = '<img src=x onerror=alert(1)>';
  const metric = { value: 10, unit: 'users', source: 'usage', window: 'Full period', coverage: 'Approved aggregate' };
  const fixture = {
    schema_version: 1, demo: true, period, generated_at: new Date().toISOString(),
    sources: [{ id: 'usage', label: 'Telemetry', status: 'unavailable', last_successful_collection: new Date().toISOString(), missing_days: [] }],
    privacy: { breakdowns_suppressed: true, suppressed_families: ['tokens'], minimum_cohort: 5, notice: 'Approved aggregates only' },
    overview: { licensed_users: metric, observed_active_users: metric, adoption_rate: metric, interactions: { ...metric, value: 0 } },
    daily: [{ day: period.start, active_users: 10, interactions: 0 }],
    models: [{ day: period.start, model: maliciousModel, active_users: 10, interactions: 0 }],
    billing: [{
      day: period.start, model: 'automatic', cost_center_id: 'enterprise-only', cost_center_name: 'Enterprise Only',
      unit: 'AI credits', currency: 'USD', source: 'billing', net_quantity: '0.1', net_amount: '0.20',
    }],
    tokens: [{ day: period.start, model: 'automatic', cost_center_id: 'enterprise-only', input: '0', output: null, source: 'tokens' }],
  };
  globalThis.document = {
    body: new Element('body'),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element('div'));
      return elements.get(id);
    },
    createElement: tag => new Element(tag),
  };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, './data/report.json');
    assert.equal(options.redirect, 'error');
    return { ok: true, json: async () => fixture };
  };
  URL.createObjectURL = blob => { exportedBlob = blob; return 'blob:dashboard-test'; };
  URL.revokeObjectURL = () => {};
  try {
    await import('../web/app.js');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements.get('dashboard').hidden, false);
    assert.equal(elements.get('demo-label').hidden, false);
    assert.equal(elements.get('stale-banner').hidden, false);
    assert.match(elements.get('stale-banner').textContent, /Source unavailable; retained data/);
    assert.match(elements.get('model-table').textContent, /<img src=x onerror=alert\(1\)>/);
    assert.match(elements.get('overview').textContent, /Observed interactions0/);
    assert.match(elements.get('token-summary').textContent, /Suppressed/);
    assert.equal(elements.get('center-filter').disabled, false);
    assert.equal(elements.get('model-filter').disabled, false);
    elements.get('center-filter').value = JSON.stringify('enterprise-only');
    elements.get('filters').listeners.get('change')();
    assert.match(elements.get('overview').textContent, /Licensed usersN\/A/);
    assert.match(elements.get('daily-table').textContent, /neither dimension/);
    assert.match(elements.get('model-table').textContent, /no cost-center attribution/);
    assert.equal(elements.get('export-button').disabled, false);
    elements.get('export-button').listeners.get('click')();
    const csv = await exportedBlob.text();
    assert.match(csv, /billing_period_summary/);
    assert.doesNotMatch(csv, /tokens_daily|tokens_period_summary/);
    assert.doesNotMatch(csv, /daily_telemetry|daily_model_activity|onerror/);
    assert.match(csv, /"0\.20"/);
    assert.match(csv, /"2026-10-01"/);
    fixture.privacy.suppressed_families.push('overview');
    elements.get('filters').listeners.get('change')();
    assert.match(elements.get('overview').textContent, /Licensed usersSuppressed/);
    elements.get('model-filter').value = JSON.stringify('model-with-no-data');
    elements.get('filters').listeners.get('change')();
    assert.equal(elements.get('export-button').disabled, true);
    assert.match(elements.get('billing-summary').textContent, /no published rows/);
  } finally {
    globalThis.document = originalDocument;
    globalThis.fetch = originalFetch;
    URL.createObjectURL = originalCreateUrl;
    URL.revokeObjectURL = originalRevokeUrl;
  }
});
