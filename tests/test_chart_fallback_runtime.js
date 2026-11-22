const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const operations = [];
const context2d = {
  setTransform: (...args) => operations.push(['setTransform', ...args]),
  clearRect: (...args) => operations.push(['clearRect', ...args]),
  fillText: (...args) => operations.push(['fillText', ...args]),
  beginPath: () => operations.push(['beginPath']),
  moveTo: (...args) => operations.push(['moveTo', ...args]),
  lineTo: (...args) => operations.push(['lineTo', ...args]),
  stroke: () => operations.push(['stroke']),
  fill: () => operations.push(['fill']),
  arc: (...args) => operations.push(['arc', ...args]),
  closePath: () => operations.push(['closePath']),
  rect: (...args) => operations.push(['rect', ...args]),
  fillRect: (...args) => operations.push(['fillRect', ...args]),
  measureText: text => ({ width: String(text).length * 6 }),
};
function canvas(id) {
  const handlers = {};
  return {
    id, width: 0, height: 0, clientWidth: 420, clientHeight: 240,
    parentElement: {},
    addEventListener: (name, fn) => { handlers[name] = fn; },
    removeEventListener: name => { delete handlers[name]; },
    getBoundingClientRect: () => ({ width: 420, height: 240, left: 0, top: 0 }),
    getContext: () => context2d,
    _handlers: handlers,
  };
}

const window = { devicePixelRatio: 1 };
const sandbox = {
  window,
  getComputedStyle: () => ({ backgroundColor: 'rgb(32, 35, 44)' }),
  console,
  Math,
  Number,
  String,
  Array,
  Object,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('app/static/vendor/chart-fallback.js', 'utf8'), sandbox);
assert.strictEqual(typeof window.Chart, 'function');

const barCanvas = canvas('stacked-bar');
const bar = new window.Chart(barCanvas, {
  type: 'bar',
  data: { labels: ['Nov', 'Dez'], datasets: [
    { label: 'Recebido', data: [20, 30], backgroundColor: '#477bd0' },
    { label: 'A receber', data: [40, 50], backgroundColor: '#b9c9e8' },
  ] },
  options: { scales: { x: { stacked: true }, y: { stacked: true } } },
});
assert.ok(bar._hits.length === 4, 'stacked bar fallback should render every dataset item');
assert.ok(operations.some(item => item[0] === 'rect' || item[0] === 'fillRect'), 'bar geometry should be drawn');

const line = new window.Chart(canvas('line'), {
  type: 'line',
  data: { labels: ['Jan', 'Fev'], datasets: [{ label: 'Rentabilidade', data: [-2, 4], borderColor: '#78b7f5' }] },
});
assert.ok(line._hits.length === 2, 'line fallback should create hit targets');

const donut = new window.Chart(canvas('donut'), {
  type: 'doughnut',
  data: { labels: ['Ações', 'FIIs'], datasets: [{ data: [60, 40], backgroundColor: ['#20bd82', '#78b7f5'] }] },
  options: { cutout: '62%' },
});
assert.ok(donut._hits.length === 2, 'doughnut fallback should render slices');
bar.destroy();
assert.strictEqual(window.Chart.instances['stacked-bar'], undefined);
console.log('Chart fallback runtime: 4 verificações aprovadas.');
