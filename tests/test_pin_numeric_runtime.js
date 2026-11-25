const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'templates', 'index.html'), 'utf8');
const match = html.match(/function sanitizePinValue\(value,maxLength=12\)\{[^\n]*\}/);
assert.ok(match, 'sanitizePinValue must be defined in the dashboard');
const context = { String };
vm.runInNewContext(`${match[0]}; globalThis.result = sanitizePinValue;`, context);
const sanitize = context.result;
assert.equal(sanitize('123456'), '123456');
assert.equal(sanitize('12a.3-456'), '123456');
assert.equal(sanitize('１２３４５６'), ''); // Full-width digits are not ASCII PIN digits.
assert.equal(sanitize('123456789012345'), '123456789012');
assert.equal(sanitize('123456789012', 6), '123456');
assert.equal(sanitize(''), '');
console.log('6 numeric PIN sanitization runtime checks passed');
