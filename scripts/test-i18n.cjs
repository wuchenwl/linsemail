/* Test the actual TypeScript locale module, its source coverage and date helpers. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const ts = require(path.join(root, 'frontend/node_modules/typescript'));
const storage = new Map();
const listeners = {};
const doc = { documentElement: { lang: '' }, title: '' };
const sandbox = { exports: {}, localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
  document: doc, window: { addEventListener: (event, fn) => { listeners[event] = fn; } }, Intl, Date,
  require: name => { if (name === 'react') return require(path.join(root, 'frontend/node_modules/react')); throw new Error(name); } };
const compile = file => ts.transpileModule(fs.readFileSync(path.join(root, file), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
vm.runInNewContext(compile('frontend/src/i18n.ts'), sandbox);
const i18n = sandbox.exports;
assert.equal(i18n.getLocale(), 'zh-CN');
assert.equal(i18n.t('收件工作台'), '收件工作台');
i18n.setLocale('en');
assert.equal(i18n.t('收件工作台'), 'Inbox');
assert.equal(doc.documentElement.lang, 'en');
assert.equal(doc.title, 'Linsemail · Mailbox workspace');
assert.equal(storage.get('linsemail-language'), 'en');
assert.equal(i18n.t('用户邮箱标签'), '用户邮箱标签');
assert.equal(i18n.t('your-code@example.com'), 'your-code@example.com');
assert.equal(i18n.t('  复制  '), '  Copy  ');
assert.match(i18n.problemText('custom_prefix_unsupported', '自定义前缀不支持'), /does not support custom prefixes/);
assert.equal(i18n.problemText('unfamiliar_code', '未知错误'), 'Operation could not be completed (unfamiliar_code).');
listeners.storage({ key: 'linsemail-language', newValue: 'zh-CN' });
assert.equal(i18n.getLocale(), 'zh-CN');
i18n.setLocale('invalid');
assert.equal(i18n.getLocale(), 'zh-CN');
const app = fs.readFileSync(path.join(root, 'frontend/src/App.tsx'), 'utf8');
const ast = ts.createSourceFile('App.tsx', app, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const missing = new Set();
const intentional = new Set(['Language / 语言', '简体中文']);
function visit(node) {
  if ((ts.isStringLiteral(node) || ts.isJsxText(node)) && /[\u3400-\u9fff]/.test(node.text)) {
    const key = node.text.trim();
    if (!intentional.has(key) && !i18n.english[key]) missing.add(key);
  }
  ts.forEachChild(node, visit);
}
visit(ast);
visit(ts.createSourceFile('AutomationPanel.tsx', fs.readFileSync(path.join(root, 'frontend/src/AutomationPanel.tsx'), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX));
assert.deepEqual([...missing], [], 'Every interface string must have an English translation');
const apiSandbox = { exports: {}, require: name => { assert.equal(name, './i18n'); return i18n; }, Intl, Date };
vm.runInNewContext(compile('frontend/src/api.ts'), apiSandbox);
i18n.setLocale('en');
assert.equal(apiSandbox.exports.remaining(new Date(Date.now() - 1000).toISOString()), 'Expired');
assert.equal(apiSandbox.exports.remaining(new Date(Date.now() + 61 * 60000).toISOString()), '1 h 1 min');
assert.equal(apiSandbox.exports.formatTime(null), 'Not synced yet');
i18n.setLocale('zh-CN');
assert.equal(apiSandbox.exports.formatTime(null), '尚未取件');
console.log('i18n: locale switching, persistence, cross-tab updates, user data, errors, dates and ' + Object.keys(i18n.english).length + ' translations verified.');
