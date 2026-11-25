const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const html = fs.readFileSync('app/templates/index.html', 'utf8');
const match = html.match(/function setThemeBrandAssets\(light\)\{[\s\S]*?\n\}/);
assert(match, 'setThemeBrandAssets must exist in the dashboard template');

function makeElement(attributeName, initialValue) {
  let value = initialValue;
  return {
    getAttribute(name) { return name === attributeName ? value : null; },
    setAttribute(name, next) { if (name === attributeName) value = next; },
    get value() { return value; }
  };
}

const elements = {
  brand: makeElement('src', '/static/lonely-wallet-logotype.png'),
  loading: makeElement('src', '/static/lonely-wallet-logo.png'),
  favicon: makeElement('href', '/static/lonely-wallet-favicon.png'),
  touch: makeElement('href', '/static/lonely-wallet-favicon.png')
};
const document = {
  querySelector(selector) {
    if (selector === '.brand-logo') return elements.brand;
    if (selector === '#loadingScreen .loading-logo') return elements.loading;
    return null;
  },
  getElementById(id) {
    if (id === 'appFavicon') return elements.favicon;
    if (id === 'appAppleTouchIcon') return elements.touch;
    return null;
  }
};
const context = { document };
vm.runInNewContext(match[0], context);

context.setThemeBrandAssets(true);
assert.strictEqual(elements.brand.value, '/static/lonely-wallet-logotype-light.png');
assert.strictEqual(elements.loading.value, '/static/lonely-wallet-logo-light.png');
assert.strictEqual(elements.favicon.value, '/static/lonely-wallet-favicon-light.png');
assert.strictEqual(elements.touch.value, '/static/lonely-wallet-favicon-light.png');

context.setThemeBrandAssets(false);
assert.strictEqual(elements.brand.value, '/static/lonely-wallet-logotype.png');
assert.strictEqual(elements.loading.value, '/static/lonely-wallet-logo.png');
assert.strictEqual(elements.favicon.value, '/static/lonely-wallet-favicon.png');
assert.strictEqual(elements.touch.value, '/static/lonely-wallet-favicon.png');

console.log('Theme brand assets runtime: light/dark logo, loading icon, favicon and touch icon switches passed.');
