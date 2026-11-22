const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('app/templates/index.html', 'utf8');
const match = html.match(/function getFixedIncomeDiscountSummary\(items\)\s*\{[\s\S]*?\n\}/);
assert(match, 'getFixedIncomeDiscountSummary helper must exist in the dashboard');
const sandbox = {};
vm.runInNewContext(`${match[0]}\nthis.getFixedIncomeDiscountSummary=getFixedIncomeDiscountSummary;`, sandbox);
const summarize = sandbox.getFixedIncomeDiscountSummary;

// Sums every fixed-income row, not only the current visible/paginated rows.
let result = summarize([
  { saldo_com_descontos: 1000.25 },
  { saldo_com_descontos: 250.50 },
  { saldo_com_descontos: 75.00 }
]);
assert.strictEqual(result.complete, true);
assert.ok(Math.abs(result.total - 1325.75) < 1e-9);

// An empty fixed-income category has a valid zero total.
result = summarize([]);
assert.strictEqual(result.complete, true);
assert.strictEqual(result.total, 0);

// If any line cannot be estimated, do not present a misleading partial total.
result = summarize([
  { saldo_com_descontos: 1000 },
  { saldo_com_descontos: null },
  { saldo_com_descontos: 250 }
]);
assert.strictEqual(result.complete, false);
assert.strictEqual(result.total, null);

// Numeric strings returned by JSON are accepted.
result = summarize([{ saldo_com_descontos: '100.10' }, { saldo_com_descontos: '50.20' }]);
assert.strictEqual(result.complete, true);
assert.ok(Math.abs(result.total - 150.30) < 1e-9);

console.log('4 fixed-income summary runtime tests passed');
