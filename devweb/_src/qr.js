// ============================================================================
// qr - JSON (PNG checked byte-level: signature and IHDR size), SVG and HTML
// formats; text/text64; ecc, scale, border and version; determinism; limits
// and validation (including the HTML error page).
// ============================================================================

load.initialize(async function () { startScript(); });

function checkPng(b) {
  const png = Buffer.from(b.png, 'base64');
  eq(png.slice(0, 8).toString('hex'), '89504e470d0a1a0a', 'PNG signature');
  eq(png.slice(12, 16).toString('latin1'), 'IHDR', 'IHDR chunk');
  eq([png.readUInt32BE(16), png.readUInt32BE(20)], [b.pixels, b.pixels], 'image is pixels x pixels');
  eq(png.length, b.pngBytes, 'pngBytes');
  eq(b.dataUri, `data:image/png;base64,${b.png}`, 'dataUri');
  eq(b.modules, 17 + 4 * b.version, 'modules = 17 + 4 x version');
  eq(b.pixels, (b.modules + 2 * b.border) * b.scale, 'pixels = (modules + 2 x border) x scale');
}

load.action('qr', async function () {
  call('service_index', { path: '/qr' }, (r, b) => ok(b.actions.includes('get'), 'get'));
  const url = `https://shop.example.com/join?code=${uid('c')}&ref=a+b#x`;

  const j = call('json_default', { path: '/qr/get', query: { text: url } }, (r, b) => {
    eq([b.text, b.bytes, b.ecc, b.scale, b.border], [url, url.length, 'M', 8, 4], 'defaults'); between(b.mask, 0, 7, 'mask');
    checkPng(b);
  }).json;
  call('text64_same_image', { path: '/qr/get', query: { text64: b64(url) } }, (r, b) => { eq(b.text, url, 'decoded text'); eq(b.png, j.png, 'same PNG'); });
  call('deterministic', { path: '/qr/get', query: { text: url } }, (r, b) => eq(b.png, j.png, 'same input, same PNG'));
  call('unicode_text', { path: '/qr/get', query: { text: 'Grüße ✓ 你好' } }, (r, b) => { eq(b.text, 'Grüße ✓ 你好', 'unicode'); checkPng(b); });

  // Options.
  call('scale_border', { path: '/qr/get', query: { text: 'hello', scale: 2, border: 1 } }, (r, b) => { eq([b.scale, b.border, b.pixels], [2, 1, 46], 'size'); checkPng(b); });
  call('scale_clamped', { path: '/qr/get', query: { text: 'hi', scale: 99 } }, (r, b) => { eq(b.scale, 40, 'scale clamped to 40'); checkPng(b); });
  call('version_minimum', { path: '/qr/get', query: { text: 'hi', version: 10, scale: 1 } }, (r, b) => { eq(b.version, 10, 'minimum version'); checkPng(b); });
  const long = 'x'.repeat(300);
  let lastVersion = 0;
  for (const ecc of ['L', 'M', 'Q', 'H']) {
    call(`ecc_${ecc}`, { path: '/qr/get', query: { text: long, ecc: ecc.toLowerCase(), scale: 1 } }, (r, b) => {
      eq(b.ecc, ecc, 'ecc'); ok(b.version > lastVersion, `version grows with ecc (${b.version})`); lastVersion = b.version; checkPng(b);
    });
  }
  call('largest_l', { path: '/qr/get', query: { text: 'A'.repeat(2900), ecc: 'L', scale: 1, border: 0 } }, (r, b) => eq(b.version, 40, 'fits in version 40'));

  // Other formats.
  call('svg', { path: '/qr/get', query: { text: url, format: 'svg' } }, (r) => {
    eq(r.contentType, 'image/svg+xml', 'SVG'); has(r.body, '<svg', 'svg element'); has(r.body, 'viewBox', 'viewBox');
  });
  call('html', { path: '/qr/get', query: { text: url, format: 'html' } }, (r) => {
    eq(r.contentType, 'text/html', 'HTML'); has(r.body, 'data:image/png;base64,', 'embedded image');
  });

  // Errors.
  call('missing_text_400', { path: '/qr/get', expect: 400 }, (r) => isError(r, 400));
  call('both_texts_400', { path: '/qr/get', query: { text: 'a', text64: 'YQ' }, expect: 400 }, (r) => isError(r, 400, 'not both'));
  call('too_long_400', { path: '/qr/get', query: { text: 'a'.repeat(3000) }, expect: 400 }, (r, b) => { isError(r, 400, 'too long'); isNum(b.maximum, 'maximum'); });
  call('bad_ecc_400', { path: '/qr/get', query: { text: 'a', ecc: 'Z' }, expect: 400 }, (r, b) => { isError(r, 400); eq(b.allowed, ['L', 'M', 'Q', 'H'], 'allowed'); });
  call('bad_format_400', { path: '/qr/get', query: { text: 'a', format: 'gif' }, expect: 400 }, (r) => isError(r, 400, 'format'));
  call('html_error_page_400', { path: '/qr/get', query: { format: 'html' }, expect: 400 }, (r) => eq(r.contentType, 'text/html', 'an HTML error page'));
});
