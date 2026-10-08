export const UNAVAILABLE = 'Unavailable';

export function validDay(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

export function records(value) {
  return Array.isArray(value) ? value.filter(row => row && typeof row === 'object' && !Array.isArray(row)) : [];
}

export function dimension(value) {
  return value === undefined || value === null || value === '' ? null : String(value);
}

export function hasDimensionFilter(filters = {}) {
  return ['model', 'center'].some(key => filters[key] !== undefined && filters[key] !== '');
}

export function filterRows(rows, filters = {}, capabilities = { model: true, center: true }) {
  const modelSelected = filters.model !== undefined && filters.model !== '';
  const centerSelected = filters.center !== undefined && filters.center !== '';
  if ((modelSelected && !capabilities.model) || (centerSelected && !capabilities.center)) return [];
  return records(rows).filter(row => {
    if (filters.start && (!validDay(row.day) || row.day < filters.start)) return false;
    if (filters.end && (!validDay(row.day) || row.day > filters.end)) return false;
    if (modelSelected && dimension(row.model) !== dimension(filters.model)) return false;
    if (centerSelected && dimension(row.cost_center_id) !== dimension(filters.center)) return false;
    return true;
  });
}

export function distinctsUnavailable(report, filters = {}) {
  const period = report?.period;
  return !validDay(period?.start) || !validDay(period?.end) || hasDimensionFilter(filters)
    || Boolean(filters.start && filters.start > period.start)
    || Boolean(filters.end && filters.end < period.end);
}

export function suppressedFamilies(report) {
  const privacy = report?.privacy;
  if (Array.isArray(privacy?.suppressed_families)) {
    return [...new Set(privacy.suppressed_families.filter(family => typeof family === 'string'))];
  }
  return privacy?.breakdowns_suppressed === true ? ['daily', 'models', 'billing', 'tokens'] : [];
}

export function visibleData(report, filters = {}) {
  const families = suppressedFamilies(report);
  return {
    daily: families.includes('daily') ? [] : filterRows(report?.daily, filters, { model: false, center: false }),
    models: families.includes('models') ? [] : filterRows(report?.models, filters, { model: true, center: false }),
    billing: families.includes('billing') ? [] : filterRows(report?.billing, filters),
    tokens: families.includes('tokens') ? [] : filterRows(report?.tokens, filters),
    distinctsUnavailable: distinctsUnavailable(report, filters),
    overviewSuppressed: families.includes('overview'),
    suppressed: families.length > 0,
    suppressedFamilies: families,
  };
}

function decimalParts(value) {
  if (typeof value === 'number' && Number.isSafeInteger(value)) value = String(value);
  if (typeof value !== 'string' || !/^[+-]?\d+(?:\.\d+)?$/.test(value)) return null;
  const negative = value.startsWith('-');
  const [whole, fraction = ''] = value.replace(/^[+-]/, '').split('.');
  return { coefficient: BigInt(`${negative ? '-' : ''}${whole}${fraction}`), scale: fraction.length };
}

function decimalString(coefficient, scale) {
  const negative = coefficient < 0n;
  const digits = (negative ? -coefficient : coefficient).toString().padStart(scale + 1, '0');
  return `${negative ? '-' : ''}${scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits}`;
}

// A missing component makes the aggregate unknown, rather than an understated known total.
export function sumDecimals(values) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const parts = values.map(decimalParts);
  if (parts.some(part => part === null)) return null;
  const scale = parts.reduce((maximum, part) => Math.max(maximum, part.scale), 0);
  const total = parts.reduce((sum, part) => sum + part.coefficient * 10n ** BigInt(scale - part.scale), 0n);
  return decimalString(total, scale);
}

export function compareDecimals(left, right) {
  const a = decimalParts(left);
  const b = decimalParts(right);
  if (!a || !b) return a ? 1 : b ? -1 : 0;
  const scale = Math.max(a.scale, b.scale);
  const difference = a.coefficient * 10n ** BigInt(scale - a.scale)
    - b.coefficient * 10n ** BigInt(scale - b.scale);
  return difference < 0n ? -1 : difference > 0n ? 1 : 0;
}

export function barPercent(value, maximum) {
  const a = decimalParts(value);
  const b = decimalParts(maximum);
  if (!a || !b || a.coefficient < 0n || b.coefficient <= 0n) return 0;
  const scale = Math.max(a.scale, b.scale);
  const numerator = a.coefficient * 10n ** BigInt(scale - a.scale);
  const denominator = b.coefficient * 10n ** BigInt(scale - b.scale);
  return Number((numerator * 1000n) / denominator > 1000n ? 1000n : (numerator * 1000n) / denominator) / 10;
}

export function aggregateRows(rows, keys, metrics) {
  const groups = new Map();
  for (const row of records(rows)) {
    const key = JSON.stringify(keys.map(name => row[name] ?? null));
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  return [...groups.values()].map(group => {
    const result = Object.fromEntries(keys.map(key => [key, group[0][key] ?? null]));
    for (const metric of metrics) result[metric] = sumDecimals(group.map(row => row[metric]));
    const days = group.map(row => row.day).filter(validDay).sort();
    result.start = days[0] ?? null;
    result.end = days.at(-1) ?? null;
    return result;
  });
}

export const BILLING_METRICS = [
  'gross_quantity', 'discount_quantity', 'net_quantity', 'gross_amount', 'discount_amount', 'net_amount',
];
export const TOKEN_METRICS = ['input', 'output', 'cache_read', 'cache_write'];
export const BILLING_DIMENSIONS = [
  'model', 'cost_center_id', 'cost_center_name', 'attribution', 'unit', 'currency', 'product', 'sku', 'source',
];

export function billingSummary(rows) {
  return aggregateRows(rows, BILLING_DIMENSIONS, BILLING_METRICS).map(row => {
    if (!row.unit) {
      for (const metric of ['gross_quantity', 'discount_quantity', 'net_quantity']) row[metric] = null;
    }
    if (!row.currency) {
      for (const metric of ['gross_amount', 'discount_amount', 'net_amount']) row[metric] = null;
    }
    return row;
  });
}

export function tokenSummary(rows) {
  return aggregateRows(rows, ['model', 'cost_center_id', 'cost_center_name', 'attribution', 'source'], TOKEN_METRICS);
}

export function displayValue(value) {
  if (value === null || value === undefined || value === '' || (typeof value === 'number' && !Number.isFinite(value))) return UNAVAILABLE;
  if (typeof value === 'number') return new Intl.NumberFormat('en-US', { maximumFractionDigits: 10 }).format(value);
  if (typeof value === 'string' && /^[+-]?\d+(?:\.\d+)?$/.test(value)) {
    const [whole, fraction] = value.split('.');
    return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction === undefined ? '' : `.${fraction}`}`;
  }
  return String(value);
}

export function csvCell(value) {
  let text = value === null || value === undefined ? UNAVAILABLE : String(value);
  // Spreadsheet parsers may ignore whitespace/control characters before a formula.
  if (/^[\s\u0000-\u001f\u007f-\u009f]*[=+\-@]/u.test(text) || /^[\s\u0000-\u001f\u007f-\u009f]/u.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

export function toCsv(rows, columns) {
  return [columns.map(csvCell).join(','), ...rows.map(row => columns.map(column => csvCell(row[column])).join(','))].join('\r\n');
}

export function sourceFreshness(source, now = Date.now()) {
  const collected = Date.parse(source?.last_successful_collection);
  const hasSnapshot = Number.isFinite(collected);
  if (source?.status === 'error') {
    return { stale: true, message: 'A source collection failed. Any retained data is the last successful snapshot, not a fresh collection.' };
  }
  if (source?.status === 'unavailable' && hasSnapshot) {
    return { stale: true, message: 'Source unavailable; retained data is the last successful snapshot, not a fresh collection.' };
  }
  if (hasSnapshot && now - collected > 48 * 60 * 60 * 1000) {
    return { stale: true, message: 'The last successful source collection is more than 48 hours old.' };
  }
  return {
    stale: false,
    message: hasSnapshot ? 'Last successful collection is within 48 hours.' : 'Unavailable: no successful collection time was reported.',
  };
}

export function freshness(report, now = Date.now()) {
  const generated = Date.parse(report?.generated_at);
  const reasons = [];
  if (!Number.isFinite(generated)) reasons.push('Report generation time is unavailable.');
  else if (now - generated > 48 * 60 * 60 * 1000) reasons.push('This report is more than 48 hours old.');
  for (const source of records(report?.sources)) {
    const state = sourceFreshness(source, now);
    if (state.stale) reasons.push(`${dimension(source.label) ?? dimension(source.id) ?? 'Source'}: ${state.message}`);
  }
  return reasons;
}
