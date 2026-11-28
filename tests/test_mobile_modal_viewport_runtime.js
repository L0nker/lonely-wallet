const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('app/templates/index.html', 'utf8');
const start = html.indexOf('function syncMobileModalViewport(){');
const end = html.indexOf('const brl=v=>', start);
assert(start >= 0 && end > start, 'mobile modal viewport helpers must be present');
const source = html.slice(start, end);

const rootStyles = new Map();
const rootClasses = new Set();
const bodyClasses = new Set();
function makeClassList(set) {
  return { toggle(name, force) { if (force) set.add(name); else set.delete(name); } };
}
const handlers = {};
const viewportHandlers = {};
const viewport = {
  height: 548,
  offsetTop: 24,
  addEventListener(name, callback) { viewportHandlers[name] = callback; }
};
const context = {
  Math,
  Number,
  Boolean,
  window: {
    innerHeight: 800,
    visualViewport: viewport,
    addEventListener(name, callback) { handlers[name] = callback; }
  },
  document: {
    documentElement: {
      style: { setProperty(name, value) { rootStyles.set(name, value); } },
      classList: makeClassList(rootClasses)
    },
    body: { classList: makeClassList(bodyClasses) }
  }
};

vm.runInNewContext(source, context);
assert.strictEqual(rootStyles.get('--lw-mobile-visual-height'), '548px');
assert.strictEqual(rootStyles.get('--lw-mobile-visual-top'), '24px');
assert.strictEqual(typeof handlers.resize, 'function');
assert.strictEqual(typeof viewportHandlers.resize, 'function');
assert.strictEqual(typeof viewportHandlers.scroll, 'function');

// Simulate the virtual keyboard reducing the visible viewport.
viewport.height = 332;
viewport.offsetTop = 40;
viewportHandlers.resize();
assert.strictEqual(rootStyles.get('--lw-mobile-visual-height'), '332px');
assert.strictEqual(rootStyles.get('--lw-mobile-visual-top'), '40px');

context.setMainModalViewportLock(true);
assert(rootClasses.has('lw-main-modal-open'));
assert(bodyClasses.has('lw-main-modal-open'));
context.setMainModalViewportLock(false);
assert(!rootClasses.has('lw-main-modal-open'));
assert(!bodyClasses.has('lw-main-modal-open'));

console.log('Mobile operation modal viewport runtime checks passed.');
