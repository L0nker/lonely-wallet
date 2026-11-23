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
function flushAnimations() {
  let steps = 0;
  while (animationQueue.length && steps < 20) {
    const callback = animationQueue.shift();
    callback(steps * 100);
    steps += 1;
  }
  return steps;
}
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

const tooltipElements = [];
const body = {
  appendChild(element) { element.parentNode = this; tooltipElements.push(element); },
  removeChild(element) {
    const index = tooltipElements.indexOf(element);
    if (index >= 0) tooltipElements.splice(index, 1);
    element.parentNode = null;
  },
};
const document = {
  body,
  createElement(tag) {
    return {
      tagName: tag.toUpperCase(), style: {}, attributes: {}, textContent: '', parentNode: null,
      setAttribute(name, value) { this.attributes[name] = value; },
    };
  },
};
const animationQueue = [];
let animationId = 0;
const window = {
  devicePixelRatio: 1,
  requestAnimationFrame(callback) { animationQueue.push(callback); return ++animationId; },
  cancelAnimationFrame() {},
};
const sandbox = {
  window,
  document,
  getComputedStyle: () => ({ backgroundColor: 'rgb(32, 35, 44)' }),
  console,
  Math,
  Number,
  String,
  Array,
  Object,
};
vm.createContext(sandbox);
const fallbackSource = fs.readFileSync('app/static/vendor/chart-fallback.js', 'utf8');
vm.runInContext(fallbackSource, sandbox);
assert.strictEqual(typeof window.Chart, 'function');

// Reproduce the browser's separate fallback script after the primary script 404s.
const html = fs.readFileSync('app/templates/index.html', 'utf8');
const primaryScriptIndex = html.indexOf("filename='vendor/chart.umd.min.js'");
const fallbackScriptIndex = html.indexOf("filename='vendor/chart-fallback.js'");
assert.ok(primaryScriptIndex >= 0 && fallbackScriptIndex > primaryScriptIndex,
  'fallback must be a separate script after the optional Chart.js script');
const scriptTags = html.slice(primaryScriptIndex - 80, fallbackScriptIndex + 100);
assert.ok(!/onerror\s*=/.test(scriptTags), 'fallback must not rely on reassigning the failed script src');

const barCanvas = canvas('stacked-bar');
const bar = new window.Chart(barCanvas, {
  type: 'bar',
  data: { labels: ['Nov', 'Dez'], datasets: [
    { label: 'Recebido', data: [20, 30], backgroundColor: '#477bd0' },
    { label: 'A receber', data: [40, 50], backgroundColor: '#b9c9e8' },
  ] },
  options: { scales: { x: { stacked: true }, y: { stacked: true } } },
});
flushAnimations();
assert.ok(bar._hits.length === 4, 'stacked bar fallback should render every dataset item');
assert.ok(operations.some(item => item[0] === 'rect' || item[0] === 'fillRect'), 'bar geometry should be drawn');

const line = new window.Chart(canvas('line'), {
  type: 'line',
  data: { labels: ['Jan', 'Fev'], datasets: [{ label: 'Rentabilidade', data: [-2, 4], borderColor: '#78b7f5' }] },
  options: { animation: { duration: 300 } },
});
const animationSteps = flushAnimations();
assert.ok(line._hits.length === 2, 'line fallback should create hit targets');
assert.ok(animationSteps >= 3, 'fallback should animate chart appearance when requestAnimationFrame is available');

const tooltipCanvas = canvas('tooltip-chart');
const tooltipChart = new window.Chart(tooltipCanvas, {
  type: 'bar',
  data: { labels: ['PETR4'], datasets: [{ label: 'Valor', data: [10], backgroundColor: '#477bd0' }] },
  options: {
    animation: false,
    plugins: { tooltip: { callbacks: { label: context => `Valor formatado: ${context.raw}` } } },
  },
});
flushAnimations();
const firstHit = tooltipChart._hits[0];
tooltipCanvas._handlers.mousemove({ clientX: firstHit.x + 2, clientY: firstHit.y + 2 });
assert.ok(tooltipElements.length === 1, 'hover should create a visible tooltip');
assert.ok(tooltipElements[0].textContent.includes('Valor formatado: 10'), 'tooltip should honor Chart.js label callbacks');
tooltipCanvas._handlers.mouseleave();
assert.strictEqual(tooltipElements.length, 0, 'tooltip should hide when pointer leaves the canvas');

const donut = new window.Chart(canvas('donut'), {
  type: 'doughnut',
  data: { labels: ['Ações', 'FIIs'], datasets: [{ data: [60, 40], backgroundColor: ['#20bd82', '#78b7f5'] }] },
  options: { cutout: '62%', animation: false },
});
assert.ok(donut._hits.length === 2, 'doughnut fallback should render slices');

// If the real Chart.js was loaded successfully, the fallback must leave it untouched.
const existingChart = function ExistingChart() {};
const successWindow = { Chart: existingChart };
vm.runInNewContext(fallbackSource, { window: successWindow, document, Math, Number, String, Array, Object });
assert.strictEqual(successWindow.Chart, existingChart, 'fallback must not replace a valid Chart.js global');

bar.destroy();
assert.strictEqual(window.Chart.instances['stacked-bar'], undefined);
console.log('Chart loader + fallback runtime: 9 verificações aprovadas.');
