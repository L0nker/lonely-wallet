const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'templates', 'index.html'), 'utf8');
const start = html.indexOf('function calculateTotalProfit(');
const end = html.indexOf('\n}\n', start);
assert.ok(start >= 0 && end > start, 'calculateTotalProfit must exist');
const source = html.slice(start, end + 2);
const context = { Number };
vm.runInNewContext(`${source}\nthis.calculateTotalProfit = calculateTotalProfit;`, context);

assert.equal(context.calculateTotalProfit(403.35, 502.21), 905.56);
assert.equal(context.calculateTotalProfit(-120.50, 300.75), 180.25);
assert.equal(context.calculateTotalProfit(0, 0), 0);
assert.equal(context.calculateTotalProfit('invalid', 25), 25);
assert.match(html, /const totalProfit=calculateTotalProfit\(summaryCapitalGain,r\.proventos\|\|0\);/);
console.log('4 total-profit runtime checks passed');
