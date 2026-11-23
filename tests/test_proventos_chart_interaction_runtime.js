'use strict';
const fs = require('fs');
const vm = require('vm');
const nodeAssert = require('assert');
const source = fs.readFileSync(__dirname + '/../app/templates/index.html', 'utf8');
function assert(condition, message) { if (!condition) throw new Error(message); }
const chartStart = source.indexOf("chart('provMonthlyChart','bar'");
assert(chartStart >= 0, 'monthly proventos chart call must exist');
const chartEnd = source.indexOf(' const totalReceived=got.reduce', chartStart);
assert(chartEnd > chartStart, 'monthly chart block must be bounded');
const chartBlock = source.slice(chartStart, chartEnd);
assert(chartBlock.includes("interaction:{mode:'index',intersect:false,axis:'x'}"), 'hover should select the whole stacked month column');
assert(chartBlock.includes("hover:{mode:'index',intersect:false}"), 'hover mode should highlight all datasets for the selected index');
assert(chartBlock.includes("title:items=>items?.length?items[0].label:''"), 'tooltip should show month/year as title');
assert(chartBlock.includes("label:ctx=>[ctx.dataset.label,brl(ctx.parsed?.y??ctx.raw??0)]"), 'tooltip should show received and pending values separately');
assert(chartBlock.includes("return ['Total',brl(total)]"), 'tooltip should show stacked total');
assert(chartBlock.includes('openProvDistributionForBar(keys[index],mode)'), 'clicking either stacked segment should open the whole month distribution');
assert(!chartBlock.includes('hit.datasetIndex===1'), 'click behavior must not depend on the clicked stack segment');
const distributionStart = source.indexOf('function openProvDistributionForBar(');
const distributionEnd = source.indexOf('window.closeAllProventosMenus', distributionStart);
const distributionBlock = source.slice(distributionStart, distributionEnd);
assert(distributionBlock.includes('[...(d.recebidos||[]),...(d.a_receber||[])]'), 'month distribution should combine paid and pending events');
assert(distributionBlock.includes("provDistributionStatus='Todos'"), 'combined distribution should identify its status');
const distributionCalls = [];
const distributionBack = { style: {} };
const sandbox = {
  state: { itens: [], proventos_dashboard: {
    recebidos: [{ ticker: 'MXRF11', data_pagamento: '2026-11-05', status: 'Pago', valor_total: 58.90 }],
    a_receber: [{ ticker: 'MXRF11', data_pagamento: '2026-11-20', status: 'A receber', valor_total: 46.60 }],
  } },
  document: { getElementById: id => id === 'provDistributionBack' ? distributionBack : null },
  buildProvDistribution: (records, title) => distributionCalls.push({ records, title }),
  setProvDistributionMode: () => {},
  provDistributionStatus: 'Pago',
  provDistributionMode: 'ativo',
  labels: {},
  escapeHTML: value => String(value),
};
vm.createContext(sandbox);
vm.runInContext(distributionBlock, sandbox);
sandbox.openProvDistributionForBar('2026-11', 'mensal');
nodeAssert.strictEqual(distributionCalls.length, 1, 'click drill-down should open the distribution modal');
nodeAssert.strictEqual(distributionCalls[0].records.length, 2, 'monthly distribution should include received and pending events');
nodeAssert.ok(distributionCalls[0].title.includes('11/2026') && distributionCalls[0].title.includes('recebidos e a receber'), 'monthly modal should identify the full month distribution');
nodeAssert.strictEqual(sandbox.provDistributionStatus, 'Todos', 'monthly distribution should use the combined status');
const filterHelper = source.slice(source.indexOf('function renderProventosDashboard()'), source.indexOf('let myProvPeriod='));
assert(filterHelper.includes("if(x.status==='A receber')return date>=`"), 'rolling 12 month chart should include pending only from current month onwards');
console.log('Proventos chart interaction runtime checks: 14 passed');
