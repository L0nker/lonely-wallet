'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '../app/templates/index.html'), 'utf8');
function extractFunction(name, nextMarker) {
  const start = html.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} must exist`);
  const end = html.indexOf(nextMarker, start);
  assert.ok(end > start, `${name} boundary must exist`);
  return html.slice(start, end);
}
const category = extractFunction('categoryKeyFromName', '\n\nfunction readAssetNoteData');
const helpers = extractFunction('getProventoAssetType', 'function buildProvPalette(');
const rendererStart = html.indexOf('function renderProventosDashboard()');
const rendererEnd = html.indexOf('\nfunction ', rendererStart + 12);
assert.ok(rendererStart >= 0 && rendererEnd > rendererStart, 'proventos dashboard renderer must exist');
const renderer = html.slice(rendererStart, rendererEnd);

const context = {
  state: {
    itens: [
      { ticker: 'PETR4', tipo: 'acao' },
      { ticker: 'MXRF11', tipo: 'fii' },
      { ticker: 'CDB123', tipo: 'renda_fixa' },
      { ticker: 'TESOURO', tipo: 'tesouro' },
    ],
  },
  categoryKeyFromName: undefined,
};
vm.createContext(context);
vm.runInContext(category, context);
vm.runInContext(helpers, context);

const rows = [
  { ticker: 'PETR4', tipo_ativo: 'acao', valor_total: 10 },
  { ticker: 'MXRF11', tipo_ativo: 'fii', valor_total: 20 },
  // Provider category conflicts with the local holding. The fixed-income
  // holding classification must win and exclude this event.
  { ticker: 'CDB123', tipo_ativo: 'outros', valor_total: 30 },
  { ticker: 'TESOURO', tipo_ativo: 'tesouro', valor_total: 40 },
  { ticker: 'LCI001', tipo_ativo: 'LCI', valor_total: 50 },
];

assert.deepEqual(
  Array.from(context.filterProventoRowsByAsset(rows, 'fii', 'todos'), x => x.ticker),
  ['MXRF11'],
  'selecting asset class must filter the chart source rows',
);
assert.deepEqual(
  Array.from(context.filterProventoRowsByAsset(rows, 'todos', 'PETR4'), x => x.ticker),
  ['PETR4'],
  'selecting one ticker must filter the chart source rows',
);
assert.deepEqual(
  Array.from(context.filterProventoRowsByAsset(rows, 'todos', 'todos'), x => x.ticker),
  ['PETR4', 'MXRF11'],
  'all chart rows must exclude fixed-income holdings and products',
);
assert.equal(context.filterProventoRowsByAsset(rows, 'renda_fixa', 'todos').length, 0,
  'fixed income must not be selectable as a proventos chart category');
assert.equal(context.isFixedIncomeProventoType('Renda Fixa'), true);
assert.equal(context.isFixedIncomeProventoType('Tesouro Direto'), true);
assert.equal(context.isFixedIncomeProventoType('CDB'), true);
assert.equal(context.isFixedIncomeProventoType('LCI'), true);
assert.equal(context.isFixedIncomeProventoType('Ações'), false);

assert.match(renderer, /const chartRows=filterProventoRowsByAsset\(rows,selectedType,selectedAsset\);/,
  'the evolution chart must derive its rows from the selected type and asset');
assert.match(renderer, /const allChartRows=chartRows\.filter\(x=>\{/,
  'the evolution chart must aggregate the filtered chart rows, not the unfiltered page rows');
assert.match(renderer, /const typeMap=new Map\(\);\s*rows\.forEach\(x=>\{/,
  'type options must be derived only from events that can produce income');
assert.match(renderer, /const availableYears=\[\.\.\.new Set\(chartRows\.filter\(/,
  'available chart years must respect the active type and ticker filters');

console.log('OK: proventos chart filters by type and ticker and excludes fixed-income categories.');
