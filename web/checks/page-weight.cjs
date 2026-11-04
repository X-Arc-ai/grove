// The sign-in and graph share index.html. Keep the actual shipped gzip payload
// below 500 KB, and prove font deduplication preserves every original face.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');
const cp = require('child_process');
const root = path.join(__dirname, '..');
cp.execFileSync('python3', [path.join(root, 'build.py')], {stdio: 'ignore'});
const html = fs.readFileSync(path.join(root, 'dist/index.html'), 'utf8');
const fontSource = fs.readFileSync(path.join(root, 'vendor/fonts.css'), 'utf8');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const expected = [...fontSource.matchAll(/@font-face\s*\{([^}]+)\}/g)].map(match => {
  const rules = Object.fromEntries([...match[1].matchAll(/([\w-]+)\s*:\s*([^;]+);/g)].map(row => [row[1], row[2].trim()]));
  const filename = rules.src.match(/url\(([^)]+)\)/)[1];
  return {
    family: rules['font-family'].replace(/^['"]|['"]$/g, ''),
    bytes: digest(fs.readFileSync(path.join(root, 'vendor', filename))),
    options: {style: rules['font-style'], weight: rules['font-weight'], display: rules['font-display'], unicodeRange: rules['unicode-range']},
  };
});
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'dist/assets/manifest.json')));
const actual = [...html.matchAll(/@font-face\s*\{([^}]+)\}/g)].map(match => {
  const rules = Object.fromEntries([...match[1].matchAll(/([\w-]+)\s*:\s*([^;]+);/g)].map(row => [row[1], row[2].trim()]));
  const filename = rules.src.match(/url\(\/assets\/grove\/([a-f0-9]{64}\.woff2)\)/)[1];
  assert.equal(manifest[filename], 'font/woff2');
  return {family: rules['font-family'].replace(/^['"]|['"]$/g, ''),
    bytes: digest(fs.readFileSync(path.join(root, 'dist/assets', filename))),
    options: {style: rules['font-style'], weight: rules['font-weight'], display: rules['font-display'], unicodeRange: rules['unicode-range']}};
});
assert.deepStrictEqual(actual, expected, 'Every font byte, face, weight and Unicode range must remain unchanged');
assert.equal(new Set(actual.map(face => face.bytes)).size, 13);
assert.equal(actual.length, 39);
assert(!html.includes('window.GroveAppScreens='), 'Guide payload must wait for its screen');
assert(!html.includes('window.GroveIntegrations='), 'Catalog payload must wait for its screen');
for (const [name, type] of Object.entries(manifest)) {
  const bytes = fs.readFileSync(path.join(root, 'dist/assets', name));
  assert.equal(name.split('.')[0], digest(bytes), 'Manifest file names bind exact content');
  assert(['font/woff2', 'text/javascript'].includes(type));
}
console.log('PASS: 39 exact font faces share 13 immutable assets; complete guide/catalog payloads are deferred');
const size = zlib.gzipSync(html, {level: 9}).length;
console.log(JSON.stringify({page: 'index.html lower bound; ui_fidelity.py measures all initial responses', raw_bytes: Buffer.byteLength(html), gzip_bytes: size, limit_bytes: 500000}));
assert(size < 500000, 'The sign-in/graph first paint exceeds 500 KB gzip');
