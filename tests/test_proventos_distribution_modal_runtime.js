'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '../app/templates/index.html'), 'utf8');
const categoryStart = html.indexOf('function categoryKeyFromName(');
const categoryEnd = html.indexOf('\n\nfunction readAssetNoteData', categoryStart);
const helperStart = html.indexOf('function getProventoAssetType(');
const paletteStart = html.indexOf('function buildProvPalette(');
const paletteEnd = html.indexOf('window.closeAllProventosMenus=function()', paletteStart);
const modeStart = html.indexOf('function setProvDistributionMode(');
const modeEnd = html.indexOf('\n\nconst chartPalette=', modeStart);
assert(categoryStart >= 0 && categoryEnd > categoryStart, 'asset category helper must exist');
assert(helperStart >= 0 && paletteStart > helperStart, 'proventos asset helpers must exist');
assert(paletteStart >= 0 && paletteEnd > paletteStart, 'distribution builder functions must exist');
assert(modeStart >= 0 && modeEnd > modeStart, 'distribution mode renderer must exist');

const nodes = {
  provTypeFilter: { value: 'todos' },
  provAssetFilter: { value: 'todos' },
  provDistributionTitle: { textContent: '' },
  provDistributionBack: { style: {} },
  provDistAssetTab: { classList: { toggle() {} } },
  provDistTypeTab: { classList: { toggle() {} } },
  provDistributionTotal: { textContent: '', style: {} },
  provDistributionDetailList: { innerHTML: '' },
};
const renderedCharts = [];
const RealDate = Date;
class FixedDate extends RealDate {
  constructor(...args) { super(...(args.length ? args : ['2026-09-20T12:00:00'])); }
  static now() { return new RealDate('2026-09-20T12:00:00').getTime(); }
}
const context = {
  document: { getElementById: (id) => nodes[id] || null },
  window: {},
  labels: { acao: 'Ações', fii: 'FIIs' },
  state: {
    itens: [
      { ticker: 'PETR4', tipo: 'acao' },
      { ticker: 'HGLG11', tipo: 'fii' },
      { ticker: 'BBDC4', tipo: 'acao' },
    ],
    proventos_dashboard: {
      recebidos: [
        { ticker: 'PETR4', tipo_ativo: 'acao', data_pagamento: '2026-09-15', valor_total: 100, tipo_evento: 'DIVIDENDO' },
        { ticker: 'HGLG11', tipo_ativo: 'fii', data_pagamento: '2026-09-20', valor_total: 63.61, tipo_evento: 'RENDIMENTO' },
      ],
      a_receber: [
        { ticker: 'PETR4', tipo_ativo: 'acao', data_pagamento: '2026-09-25', valor_total: 20, tipo_evento: 'JCP' },
        { ticker: 'BBDC4', tipo_ativo: 'acao', data_pagamento: '2026-09-27', valor_total: 5, tipo_evento: 'JCP' },
        { ticker: 'BBDC4', tipo_ativo: 'acao', data_pagamento: '2026-09-10', valor_total: 99, tipo_evento: 'JCP' },
      ],
    },
  },
  provDistributionData: { ativo: [], tipo: [] },
  provDistributionMode: 'ativo',
  provDistributionStatus: 'Pago',
  buildProvDistribution: undefined,
  brl: (value) => `R$ ${Number(value).toFixed(2)}`,
  pct: (value) => `${Number(value).toFixed(2)}%`,
  escapeHTML: (value) => String(value),
  chart: (...args) => renderedCharts.push(args),
  Number,
  Date: FixedDate,
};

vm.createContext(context);
vm.runInContext(html.slice(categoryStart, categoryEnd), context);
vm.runInContext(html.slice(helperStart, paletteStart) + html.slice(paletteStart, paletteEnd), context);
vm.runInContext(html.slice(modeStart, modeEnd), context);

context.openProvDistributionForBar('2026-09', 'mensal');
assert.equal(nodes.provDistributionTitle.textContent, 'Distribuição de proventos em 09/2026');
assert.equal(nodes.provDistributionBack.style.display, 'flex');
assert.equal(context.provDistributionStatus, 'Consolidado');
assert.deepEqual(
  Array.from(context.provDistributionData.ativo, (row) => [row.name, Number(row.value.toFixed(2))]),
  [['PETR4', 120], ['HGLG11', 63.61], ['BBDC4', 5]],
  'asset distribution must include received and still-current forecasts for the whole month',
);
assert.deepEqual(
  Array.from(context.provDistributionData.tipo, (row) => [row.name, Number(row.value.toFixed(2))]),
  [['Ações', 125], ['FIIs', 63.61]],
);
assert.equal(nodes.provDistributionTotal.textContent, 'R$ 188.61');
assert.match(nodes.provDistributionDetailList.innerHTML, /Recebido \+ a receber/);
assert.equal(renderedCharts.at(-1)[1], 'doughnut');
assert.deepEqual(Array.from(renderedCharts.at(-1)[2].datasets[0].data), [120, 63.61, 5]);


// The active asset filter must still be respected when opening the monthly detail.
nodes.provTypeFilter.value = 'fii';
context.openProvDistributionForBar('2026-09', 'mensal');
assert.deepEqual(
  Array.from(context.provDistributionData.ativo, (row) => [row.name, Number(row.value.toFixed(2))]),
  [['HGLG11', 63.61]],
  'the existing asset-class filter must be applied to the monthly distribution',
);
assert.equal(nodes.provDistributionTotal.textContent, 'R$ 63.61');
nodes.provTypeFilter.value = 'todos';
nodes.provAssetFilter.value = 'BBDC4';
context.openProvDistributionForBar('2026-09', 'mensal');
assert.deepEqual(
  Array.from(context.provDistributionData.ativo, (row) => [row.name, Number(row.value.toFixed(2))]),
  [['BBDC4', 5]],
  'the selected ticker must also constrain the monthly detail',
);
assert.equal(nodes.provDistributionTotal.textContent, 'R$ 5.00');
nodes.provAssetFilter.value = 'todos';
context.openProvDistributionForBar('2026-09', 'mensal');

context.setProvDistributionMode('tipo');
assert.deepEqual(Array.from(renderedCharts.at(-1)[2].labels), ['Ações', 'FIIs']);
assert.deepEqual(Array.from(renderedCharts.at(-1)[2].datasets[0].data), [125, 63.61]);

const chartHandler = html.match(/onClick:\(event,elements\)=>\{if\(!elements\?\.length\)return;const hit=elements\[0\],index=hit\.index;if\(index>=0&&keys\[index\]\)openProvDistributionForBar\(keys\[index\],mode\);\}/);
assert.ok(chartHandler, 'clicking either segment must open the combined month distribution');

console.log('OK: monthly distribution modal renders the doughnut chart with received + forecast values by asset and by type.');
