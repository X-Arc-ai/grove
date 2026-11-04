// Synthetic rendering regression. No DOM, browser, build, dependencies, or network.
// Select an unchanged source tree with GROVE_LOGIN_WEB for the original-base red run.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const web = process.env.GROVE_LOGIN_WEB || path.join(__dirname, '..');
const source = fs.readFileSync(path.join(web, 'product.js'), 'utf8');
// Exercise the real renderer and its pure helpers without starting the application.
function sourceFunction(name) {
  const start = source.indexOf('  function ' + name + '(');
  assert.notEqual(start, -1, 'Missing product function: ' + name);
  const next = source.indexOf('\n  function ', start + 1);
  assert.notEqual(next, -1, 'Missing next function after: ' + name);
  return source.slice(start, next);
}
const renderSource = ['accountMode', 'wordmark', 'esc', 'icon', 'signIn'].map(sourceFunction).join('\n');

function render({ mode = 'login', stage = 'signin', fixture = false, email = '', code = '' } = {}) {
  const ui = { authStage: stage, email, code };
  const before = JSON.stringify(ui);
  const pages = { login: '/login', signup: '/signup', privacy: '/privacy' };
  const context = vm.createContext({
    ui,
    icons: {},
    window: { location: { pathname: pages[mode] }, GroveEntryVisual: '<canvas class="gp-entry-canvas"></canvas>' },
    live: fixture ? null : {
      PAGES: pages,
      authPagePath: page => page + '?next=%2Freturn',
    },
  });
  const html = vm.runInContext(renderSource + '\nsignIn();', context, { timeout: 1000 });
  assert.equal(JSON.stringify(ui), before, 'Rendering must not change authentication state');
  return html;
}

function openingTag(html, tag, attribute) {
  const found = [...html.matchAll(new RegExp('<' + tag + '\\b[^>]*>', 'g'))]
    .map(match => match[0]).find(value => value.includes(attribute));
  assert(found, 'Missing ' + tag + ' with ' + attribute);
  return found;
}
function minimalLogin(html) {
  assert.doesNotMatch(html, /<h[1-6]\b|gp-auth-brand|gp-auth-intro|gp-wordmark/,
    'Initial login must not render a visible title, brand, or introduction');
  assert.doesNotMatch(html, /Give your AI a world to understand|Return to your Grove/);
  assert.match(html, /<div class="gp-auth"><div class="gp-auth-form"><div class="gp-signin-options">/,
    'The card starts directly with sign-in controls');
}

const checks = [];
const check = (name, run) => checks.push([name, run]);

check('Initial live login contains controls without a heading or slogan', () => {
  minimalLogin(render());
});

check('Initial login retains the dialog name and existing keyboard focus target', () => {
  const html = render();
  const dialog = openingTag(html, 'section', 'role="dialog"');
  assert.match(dialog, /aria-modal="true"/);
  assert.match(dialog, /aria-label="Log in"/);
  assert.match(dialog, /id="gp-title"/);
  assert.match(dialog, /tabindex="-1"/);
  assert.doesNotMatch(dialog, /aria-labelledby=/);
  assert.equal((html.match(/id="gp-title"/g) || []).length, 1);
});

check('Google remains available and Apple retains its neutral disabled control', () => {
  const html = render();
  const google = openingTag(html, 'button', 'data-signin="google"');
  const apple = openingTag(html, 'button', 'data-signin="apple"');
  assert.doesNotMatch(google, /disabled/);
  assert.match(apple, /\sdisabled(?:\s|>)/);
  assert.match(apple, /aria-disabled="true"/);
  assert.match(html, /Continue with Google<\/button>/);
  assert.match(html, /Continue with Apple<\/button>/);
  assert.doesNotMatch(html, /Not available yet/i);
  assert.equal((html.match(/data-signin=/g) || []).length, 2);
});

check('The email label, field, divider, and submit control remain in order', () => {
  const html = render({ email: 'synthetic@example.test' });
  const input = openingTag(html, 'input', 'id="gp-email"');
  for (const attribute of ['name="email"', 'type="email"', 'autocomplete="email"', 'required']) {
    assert(input.includes(attribute), 'Missing email attribute: ' + attribute);
  }
  assert.match(input, /value="synthetic@example\.test"/);
  assert.match(html, /<div class="gp-auth-divider"><span>or<\/span><\/div><form data-email-form><label for="gp-email">Email address<\/label>/);
  assert.match(html, /<button class="gp-primary gp-wide">Continue with email<\/button><\/form>/);
  assert.equal((html.match(/<input\b/g) || []).length, 1);
});

check('Create-account and privacy links retain their destinations', () => {
  const html = render();
  assert.match(html, /<nav class="gp-auth-navigation" aria-label="Account access">/);
  assert.match(html, /href="\/signup\?next=%2Freturn">New to Grove\? Create account<\/a>/);
  assert.match(html, /href="\/privacy">Privacy<\/a>/);
  assert.equal((html.match(/<a\s/g) || []).length, 2);
});

check('Signup retains its brand, title, instructions, controls, and login link', () => {
  const html = render({ mode: 'signup' });
  assert.match(html, /<header class="gp-auth-brand"><h1 id="gp-title" tabindex="-1">/);
  assert.match(html, /Give your AI a world to understand/);
  assert.match(html, /<h2>Create account<\/h2><p>Verify your email or Google account to start your Grove\.<\/p>/);
  assert.match(html, /aria-labelledby="gp-title"/);
  assert.match(html, /data-email-form/);
  assert.match(html, /data-signin="google"/);
  assert.match(html, /data-signin="apple" disabled aria-disabled="true"/);
  assert.match(html, /href="\/login\?next=%2Freturn">Already have an account\? Log in<\/a>/);
});

for (const mode of ['login', 'signup']) {
  check(mode + ' code entry retains its instructions, field, submit action, and back control', () => {
    const html = render({ mode, stage: 'code', email: 'synthetic@example.test', code: '123456' });
    assert.match(html, /<header class="gp-auth-brand">/);
    assert.match(html, /aria-labelledby="gp-title"/);
    assert.match(html, /<h2>Check your inbox<\/h2><p>If a code arrives at synthetic@example\.test, enter it below\.<\/p>/);
    assert.match(html, /<form data-code-form><label for="gp-code">Verification code<\/label>/);
    const input = openingTag(html, 'input', 'id="gp-code"');
    for (const attribute of ['inputmode="numeric"', 'pattern="[0-9]{6}"', 'maxlength="6"', 'autocomplete="one-time-code"', 'value="123456"', 'required']) {
      assert(input.includes(attribute), 'Missing code attribute: ' + attribute);
    }
    const title = mode === 'signup' ? 'Create account' : 'Log in';
    assert(html.includes('<button class="gp-primary gp-wide">' + title + '</button>'));
    assert.match(html, /data-action="auth-back">Use another email<\/button>/);
    assert.doesNotMatch(html, /data-email-form|data-signin=/);
  });
}

check('Session checking retains its status and does not display login controls', () => {
  for (const mode of ['login', 'signup']) {
    const html = render({ mode, stage: 'checking' });
    assert.match(html, /<header class="gp-auth-brand">/);
    assert.match(html, /aria-labelledby="gp-title"/);
    assert.match(html, /role="status" aria-label="Checking your sign-in"/);
    assert.doesNotMatch(html, /data-email-form|data-code-form|data-signin=|gp-auth-navigation/);
  }
});

check('Email text remains escaped in initial login and code instructions', () => {
  const email = '\"><img src=x onerror=alert(1)>&';
  for (const stage of ['signin', 'code']) {
    const html = render({ email, stage });
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&quot;&gt;&lt;img src=x onerror=alert\(1\)&gt;&amp;/);
  }
});

check('Fixture login uses the same minimal card without live account links', () => {
  const html = render({ fixture: true });
  minimalLogin(html);
  assert.match(html, /data-email-form/);
  assert.doesNotMatch(html, /gp-auth-navigation/);
});

let failures = 0;
for (const [name, run] of checks) {
  try { run(); console.log('PASS: ' + name); }
  catch (error) { failures++; console.error('FAIL: ' + name + '\n' + error.stack); }
}
console.log(`Login controls: ${checks.length - failures} passed, ${failures} failed`);
if (failures) process.exitCode = 1;
