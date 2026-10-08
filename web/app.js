import {
  UNAVAILABLE, records, dimension, hasDimensionFilter, visibleData, validDay,
  displayValue, billingSummary, tokenSummary, compareDecimals, barPercent,
  BILLING_METRICS, TOKEN_METRICS, toCsv, freshness, sourceFreshness, suppressedFamilies,
} from './data-utils.js';

const byId = id => document.getElementById(id);
const visibleExports = new Map();
let report;
const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });
const exportColumns = [
  'dataset', 'period_start', 'period_end', 'day', 'model', 'cost_center_id', 'cost_center_name',
  'center_state', 'attribution', 'source', 'unit', 'currency', 'product', 'sku',
  'active_users', 'licensed_users', 'interactions',
  'cli_prompt_tokens', 'cli_output_tokens', 'app_prompt_tokens', 'app_output_tokens',
  ...BILLING_METRICS, ...TOKEN_METRICS,
];

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function scalar(value) {
  return ['string', 'number', 'boolean'].includes(typeof value) ? String(value) : UNAVAILABLE;
}

function timestamp(value) {
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? new Date(parsed).toISOString().replace('T', ' ').replace(/(?:\.000)?Z$/, ' UTC') : UNAVAILABLE;
}

function sourceName(id) {
  const source = records(report.sources).find(item => item.id === id);
  return source ? `${scalar(source.label ?? source.id)} (${scalar(source.id)})` : scalar(id);
}

function centerName(row) {
  if (row.cost_center_name) return scalar(row.cost_center_name);
  const center = records(report.cost_centers).find(item => dimension(item.id) === dimension(row.cost_center_id));
  return scalar(center?.name ?? row.cost_center_id ?? 'Unknown / unresolved');
}

function centerState(row) {
  return scalar(records(report.cost_centers).find(item => dimension(item.id) === dimension(row.cost_center_id))?.state);
}

function windowLabel(value) {
  if (value && typeof value === 'object') return `${scalar(value.start)} – ${scalar(value.end)} UTC`;
  return scalar(value);
}

function currentFilters() {
  const filters = {
    start: byId('start-date').value || report.period?.start,
    end: byId('end-date').value || report.period?.end,
  };
  if (byId('model-filter').value) filters.model = JSON.parse(byId('model-filter').value);
  if (byId('center-filter').value) filters.center = JSON.parse(byId('center-filter').value);
  return filters;
}

function columnsFor(keys) {
  const labels = {
    day: 'Day (UTC)', model: 'Model', cost_center_id: 'Center ID', cost_center_name: 'Cost center',
    center_state: 'Center state', attribution: 'Attribution', source: 'Source', unit: 'Quantity unit',
    currency: 'Currency', product: 'Product', sku: 'SKU', active_users: 'Observed active users',
    licensed_users: 'Licensed users (snapshot)', interactions: 'Interactions',
    cli_prompt_tokens: 'CLI prompt tokens', cli_output_tokens: 'CLI output tokens',
    app_prompt_tokens: 'App prompt tokens', app_output_tokens: 'App output tokens',
    gross_quantity: 'Gross quantity', discount_quantity: 'Included / discounted quantity', net_quantity: 'Net quantity',
    gross_amount: 'Gross amount', discount_amount: 'Included / discounted amount', net_amount: 'Net usage amount',
    input: 'Input tokens', output: 'Output tokens', cache_read: 'Cache-read tokens', cache_write: 'Cache-write tokens',
  };
  const numeric = new Set([
    ...BILLING_METRICS, ...TOKEN_METRICS, 'active_users', 'licensed_users', 'interactions',
    'cli_prompt_tokens', 'cli_output_tokens', 'app_prompt_tokens', 'app_output_tokens',
  ]);
  return keys.map(key => ({
    key, label: labels[key] ?? key, numeric: numeric.has(key),
    value: row => key === 'cost_center_name' ? centerName(row)
      : key === 'center_state' ? centerState(row) : row[key] ?? null,
  }));
}

function table(targetId, caption, inputRows, columns, options = {}) {
  const target = byId(targetId);
  target.replaceChildren();
  visibleExports.delete(targetId);
  if (!inputRows.length) {
    target.append(element('p', options.empty ?? 'Unavailable: no published rows match these filters. Missing data is not zero.', 'empty'));
    return;
  }
  let rows = [...inputRows];
  const wrapper = element('div', undefined, 'table-scroll');
  wrapper.tabIndex = 0;
  wrapper.setAttribute('role', 'region');
  wrapper.setAttribute('aria-label', `${caption}. Scroll horizontally to view all columns.`);
  const grid = element('table');
  grid.append(element('caption', caption));
  const head = element('thead');
  const headings = element('tr');
  const body = element('tbody');
  const headers = [];
  let sortKey;
  let ascending = true;

  function paintRows() {
    body.replaceChildren();
    const exported = [];
    for (const row of rows) {
      const tr = element('tr');
      const exportRow = {
        dataset: options.dataset,
        period_start: row.start ?? row.day ?? options.filters?.start ?? report.period?.start,
        period_end: row.end ?? row.day ?? options.filters?.end ?? report.period?.end,
      };
      for (const column of columns) {
        const value = column.value(row);
        const td = element('td', displayValue(value), column.numeric ? 'numeric' : undefined);
        if (column.key === options.barKey) {
          const progress = element('progress', undefined, 'rank-bar');
          progress.max = 100;
          progress.value = barPercent(value, options.maximum(row));
          progress.setAttribute('aria-hidden', 'true');
          td.append(progress);
        }
        tr.append(td);
        exportRow[column.key] = value ?? UNAVAILABLE;
      }
      body.append(tr);
      exported.push(exportRow);
    }
    if (options.dataset) visibleExports.set(targetId, exported);
  }

  for (const column of columns) {
    const th = element('th');
    th.scope = 'col';
    th.setAttribute('aria-sort', 'none');
    const button = element('button', `${column.label} ↕`, 'sort-button');
    button.type = 'button';
    button.setAttribute('aria-label', `Sort by ${column.label}`);
    button.addEventListener('click', () => {
      ascending = sortKey === column.key ? !ascending : true;
      sortKey = column.key;
      for (const header of headers) header.setAttribute('aria-sort', 'none');
      th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
      rows.sort((left, right) => {
        const a = column.value(left);
        const b = column.value(right);
        const order = column.numeric ? compareDecimals(a, b) : collator.compare(scalar(a), scalar(b));
        return ascending ? order : -order;
      });
      paintRows();
    });
    th.append(button);
    headers.push(th);
    headings.append(th);
  }
  head.append(headings);
  grid.append(head, body);
  wrapper.append(grid);
  target.append(wrapper);
  paintRows();
}

function overview(unavailable, suppressed) {
  const target = byId('overview');
  target.replaceChildren();
  byId('overview-note').textContent = suppressed
    ? 'Suppressed: enterprise overview values are withheld under the publication privacy policy.'
    : unavailable
    ? 'N/A for this selection: overview metrics are only published for the full enterprise period. Daily and model unique users are never summed to reconstruct them.'
    : 'Published full-period enterprise metrics. Definitions, windows, and coverage belong to each source; these are not sums of daily users.';
  for (const [key, label] of [
    ['licensed_users', 'Licensed users'],
    ['observed_active_users', 'Observed active users'],
    ['adoption_rate', 'Observed adoption'],
    ['interactions', 'Observed interactions'],
  ]) {
    const metric = report.overview?.[key] ?? {};
    const card = element('article', undefined, 'card');
    card.append(element('h3', label), element('p', suppressed ? 'Suppressed' : unavailable ? 'N/A' : displayValue(metric.value), 'card-value'));
    const definitions = element('dl');
    const source = records(report.sources).find(item => item.id === metric.source);
    for (const [name, value] of [
      ['Unit', scalar(metric.unit)],
      ['Window', windowLabel(metric.window)],
      ['Source', sourceName(metric.source)],
      ['Coverage', scalar(metric.coverage)],
      ['Last collected', timestamp(source?.last_successful_collection)],
    ]) {
      definitions.append(element('dt', `${name}: `), element('dd', value));
    }
    card.append(definitions);
    target.append(card);
  }
}

function telemetrySource() {
  return scalar(report.overview?.interactions?.source ?? report.overview?.observed_active_users?.source);
}

function groupKey(row) {
  return JSON.stringify(['source', 'unit', 'currency', 'product', 'sku', 'attribution'].map(key => row[key] ?? null));
}

function render() {
  visibleExports.clear();
  const filters = currentFilters();
  const invalid = !byId('start-date').checkValidity() || !byId('end-date').checkValidity()
    || Boolean(filters.start && filters.end && filters.start > filters.end);
  const data = visibleData(report, filters);
  if (invalid) {
    for (const key of ['daily', 'models', 'billing', 'tokens']) data[key] = [];
    data.distinctsUnavailable = true;
  }
  const emptyFor = family => invalid
    ? 'Choose a valid date range within the published period; the start must not be after the end.'
    : data.suppressedFamilies.includes(family)
      ? 'Suppressed: this data family is withheld under the publication privacy policy. Missing rows must not be interpreted as zero.'
      : 'Unavailable: no published rows match these filters. Missing data is not zero.';
  overview(data.distinctsUnavailable, data.overviewSuppressed);
  const daily = data.daily.map(row => ({
    ...row,
    source: `Telemetry: ${telemetrySource()}; licensed snapshots: ${scalar(report.overview?.licensed_users?.source)}`,
    unit: 'Users / interactions / surface-specific tokens (separate columns)',
  }));
  const models = data.models.map(row => ({ ...row, source: telemetrySource(), unit: 'Users / interactions (separate columns)' }));
  const dailyUnsupported = hasDimensionFilter(filters);
  const modelUnsupported = filters.center !== undefined;
  table('daily-table', 'Daily enterprise telemetry — source-defined counts and separate token categories', daily,
    columnsFor(['day', 'active_users', 'licensed_users', 'interactions', 'cli_prompt_tokens', 'cli_output_tokens', 'app_prompt_tokens', 'app_output_tokens', 'source', 'unit']),
    { dataset: 'daily_telemetry', filters, empty: !invalid && !data.suppressedFamilies.includes('daily') && dailyUnsupported ? 'Unavailable for model or center filters: daily enterprise telemetry has neither dimension.' : emptyFor('daily') });
  table('model-table', 'Daily model activity — users are non-additive', models,
    columnsFor(['day', 'model', 'active_users', 'interactions', 'source', 'unit']),
    { dataset: 'daily_model_activity', filters, empty: !invalid && !data.suppressedFamilies.includes('models') && modelUnsupported ? 'Unavailable for center filters: model telemetry has no cost-center attribution.' : emptyFor('models') });

  const billing = billingSummary(data.billing);
  const maximums = new Map();
  for (const row of billing) {
    const key = groupKey(row);
    if (!maximums.has(key) || compareDecimals(row.net_quantity, maximums.get(key)) > 0) maximums.set(key, row.net_quantity);
  }
  billing.sort((a, b) => collator.compare(groupKey(a), groupKey(b)) || compareDecimals(b.net_quantity, a.net_quantity));
  const dimensions = ['model', 'cost_center_name', 'cost_center_id', 'center_state', 'attribution'];
  const billingColumns = [...dimensions, 'unit', 'currency', ...BILLING_METRICS, 'product', 'sku', 'source'];
  table('billing-summary', 'Selected-period billing by model and cost center — bars compare matching units only', billing,
    columnsFor(billingColumns), { dataset: 'billing_period_summary', filters, empty: emptyFor('billing'), barKey: 'net_quantity', maximum: row => maximums.get(groupKey(row)) });
  table('billing-trend', 'Daily billing aggregates — exact source quantities and amounts', data.billing,
    columnsFor(['day', ...billingColumns]), { dataset: 'billing_daily', filters, empty: emptyFor('billing') });

  const tokenColumns = [...dimensions, ...TOKEN_METRICS, 'source', 'unit'];
  const tokens = data.tokens.map(row => ({ ...row, unit: 'Tokens (four separate categories)' }));
  const summary = tokenSummary(tokens).map(row => ({ ...row, unit: 'Tokens (four separate categories)' }));
  table('token-summary', 'Selected-period token categories by model and cost center — no combined total', summary,
    columnsFor(tokenColumns), { dataset: 'tokens_period_summary', filters, empty: emptyFor('tokens') });
  table('token-trend', 'Daily billing-report token categories', tokens,
    columnsFor(['day', ...tokenColumns]), { dataset: 'tokens_daily', filters, empty: emptyFor('tokens') });
  byId('token-coverage').textContent = tokens.length
    ? `Visible report range: ${scalar(filters.start)} – ${scalar(filters.end)} UTC. Coverage: published rows and reported categories only; missing days/categories remain unavailable. Sources: ${[...new Set(tokens.map(row => sourceName(row.source)))].join('; ')}. Independent collection times and source status appear below.`
    : 'Input: Unavailable · Output: Unavailable · Cache-read: Unavailable · Cache-write: Unavailable. Report availability, collection status, and missing days appear below.';
  const count = data.daily.length + data.models.length + data.billing.length + data.tokens.length;
  byId('filter-status').textContent = invalid ? emptyFor()
    : `${count} published aggregate rows match the supported filters. Dates: ${scalar(filters.start)} – ${scalar(filters.end)} UTC.${data.suppressed ? ` Suppressed families: ${data.suppressedFamilies.join(', ')}.` : ''} CSV tables are separately labeled: do not add summary rows to daily rows.`;
  byId('export-button').disabled = invalid || !visibleExports.size;
}

function setupFilters() {
  const period = report.period ?? {};
  for (const [id, key] of [['start-date', 'start'], ['end-date', 'end']]) {
    const input = byId(id);
    if (validDay(period[key])) input.defaultValue = period[key];
    if (validDay(period.start)) input.min = period.start;
    if (validDay(period.end)) input.max = period.end;
  }
  const approved = visibleData(report);
  const published = [...approved.billing, ...approved.tokens];
  const modelRows = [...published, ...approved.models];
  const modelValues = [...new Set(modelRows.map(row => dimension(row.model)))].sort((a, b) => collator.compare(scalar(a), scalar(b)));
  for (const model of modelValues) {
    const option = element('option', model ?? 'Unknown / unreported model');
    option.value = JSON.stringify(model);
    byId('model-filter').append(option);
  }
  const centers = new Map(published.map(row => [dimension(row.cost_center_id), centerName(row)]));
  for (const [id, name] of [...centers.entries()].sort((a, b) => collator.compare(a[1], b[1]))) {
    const option = element('option', `${name}${id === null ? '' : ` (${id})`}`);
    option.value = JSON.stringify(id);
    byId('center-filter').append(option);
  }
  byId('model-filter').disabled = !modelValues.length;
  byId('center-filter').disabled = !centers.size;
  byId('filters').addEventListener('change', render);
  byId('filters').addEventListener('submit', event => { event.preventDefault(); render(); });
  byId('filters').addEventListener('reset', () => queueMicrotask(render));
  byId('export-button').addEventListener('click', () => {
    const rows = [...visibleExports.values()].flat().map(row => Object.fromEntries(exportColumns.map(key => [key, Object.hasOwn(row, key) ? row[key] : ''])));
    if (!rows.length) return;
    const url = URL.createObjectURL(new Blob([toCsv(rows, exportColumns)], { type: 'text/csv;charset=utf-8' }));
    const link = element('a');
    link.href = url;
    link.download = 'copilot-visible-aggregates.csv';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    byId('filter-status').textContent = `Exported ${rows.length} visible table rows. Summary and daily datasets are labeled separately; do not add them together.`;
  });
}

function showQuality() {
  byId('collection-status').textContent = `Report generated: ${timestamp(report.generated_at)}. Last successful collection: ${timestamp(report.last_successful_collection)}. Source schedules and latest available days differ.`;
  table('sources-table', 'Source availability and collection coverage', records(report.sources), [
    { key: 'label', label: 'Source', value: row => row.label ?? row.id },
    { key: 'status', label: 'Status', value: row => ({ ok: 'Available', unavailable: 'Unavailable', error: 'Error — retained data may be stale' })[row.status] ?? UNAVAILABLE },
    { key: 'last_successful_collection', label: 'Last successful collection (UTC)', value: row => timestamp(row.last_successful_collection) },
    { key: 'freshness', label: 'Collection freshness', value: row => {
      const state = sourceFreshness(row);
      return `${state.stale ? 'Stale: ' : ''}${state.message}`;
    } },
    { key: 'latest_available_day', label: 'Latest available day (UTC)', value: row => row.latest_available_day },
    { key: 'missing_days', label: 'Missing days', value: row => Array.isArray(row.missing_days) ? row.missing_days.map(scalar).join(', ') || 'None reported' : UNAVAILABLE },
    { key: 'message', label: 'Status / coverage details', value: row => row.message },
  ], { empty: 'Unavailable: no source status information was published.' });
  const notes = Array.isArray(report.quality) ? report.quality.filter(note => typeof note === 'string') : [];
  byId('quality-list').replaceChildren(...(notes.length ? notes : ['No additional quality or reconciliation results were published. Absence of a warning is not evidence of completeness.']).map(note => element('li', note)));
  const privacy = report.privacy ?? {};
  const families = suppressedFamilies(report);
  byId('privacy-notice').textContent = `${scalar(privacy.notice)} Minimum published cohort: ${displayValue(privacy.minimum_cohort)}. ${families.length ? `Suppressed families: ${families.join(', ')}. Other published families remain available.` : `Dimensional breakdowns: ${privacy.breakdowns_suppressed === false || Array.isArray(privacy.suppressed_families) ? 'approved for publication' : 'approval status unavailable'}.`}`;
}

async function load() {
  try {
    const response = await fetch('./data/report.json', { credentials: 'same-origin', cache: 'no-store', redirect: 'error' });
    if (!response.ok) throw new Error('Report request failed.');
    report = await response.json();
    if (!report || Array.isArray(report) || report.schema_version !== 1) throw new Error('Unsupported report schema.');
    byId('report-period').textContent = `Published period: ${scalar(report.period?.start)} – ${scalar(report.period?.end)} · UTC`;
    byId('demo-label').hidden = report.demo !== true;
    const warnings = freshness(report);
    byId('stale-banner').hidden = !warnings.length;
    byId('stale-banner').textContent = warnings.join(' ');
    setupFilters();
    showQuality();
    render();
    byId('dashboard').hidden = false;
    byId('load-status').textContent = report.demo === true ? 'Demo report loaded. All values are synthetic.' : 'Published aggregate report loaded.';
  } catch {
    byId('load-status').textContent = 'Report unavailable. The published aggregate file could not be loaded or has an unsupported schema. Ask the reporting administrator to verify collection and publication, then reload this page.';
    byId('load-status').className = 'notice warning';
    byId('load-status').setAttribute('role', 'alert');
    byId('dashboard').hidden = true;
  }
}

load();
