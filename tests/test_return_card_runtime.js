const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'app', 'templates', 'index.html'), 'utf8');
const start = html.indexOf('function renderConsolidatedReturnSparklines(');
const end = html.indexOf('\n}\n\nfunction operationQuantity', start);
assert.ok(start >= 0 && end > start, 'renderConsolidatedReturnSparklines must exist');
const functionSource = html.slice(start, end + 2);

function run(totalReturn, fallback12m, series12m, hasAssets = true) {
  const twelveNode = { textContent: '', className: '' };
  const totalNode = { textContent: '', className: '' };
  let renderedSeries = null;
  const context = {
    state: {
      itens: hasAssets ? [{ ticker: 'PETR4' }] : [],
      rentabilidade_historica: { series_12m: series12m },
    },
    getAllTypesPortfolioHistory: () => ({ rows: [
      { mes: '2026-10', aplicado: 1000, patrimonio: 1200 },
    ] }),
    pct: value => `${Number(value).toFixed(2)}%`,
    renderReturnSparklines: (rolling, total) => { renderedSeries = { rolling, total }; },
    document: {
      querySelector(selector) {
        return selector === '#return12mChart'
          ? { parentElement: { querySelector: () => twelveNode } }
          : { parentElement: { querySelector: () => totalNode } };
      },
    },
    Number,
    Array,
    String,
  };
  vm.runInNewContext(`${functionSource}\nrenderConsolidatedReturnSparklines(${JSON.stringify(totalReturn)}, ${JSON.stringify(fallback12m)});`, context);
  return { twelveNode, totalNode, renderedSeries };
}

// O total deve ser exatamente o valor passado pelo card Patrimônio Total,
// mesmo que o último snapshot histórico mostre outro percentual.
const result = run(1.81, 2.4, [
  { mes: '2026-10', valor: 4.25 },
  { mes: '2026-11', valor: 5.75 },
]);
assert.equal(result.totalNode.textContent, '1.81%');
assert.equal(result.twelveNode.textContent, '5.75%');
assert.equal(result.renderedSeries.rolling.length, 2);

// Sem série móvel, usa o fallback recebido; null permanece indisponível,
// não é convertido incorretamente para 0%.
const fallback = run(0, 8.25, []);
assert.equal(fallback.twelveNode.textContent, '8.25%');
assert.equal(fallback.totalNode.textContent, '0.00%');
const unavailable = run(null, null, [], true);
assert.equal(unavailable.twelveNode.textContent, '—');
assert.equal(unavailable.totalNode.textContent, '—');

console.log('3 return-card runtime checks passed');
