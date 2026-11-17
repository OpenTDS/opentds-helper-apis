// Helper for devweb-mock.js: performs ONE HTTP request (JSON on stdin) and
// prints the response as JSON. Run as a child process to make sendSync()
// synchronous. Not used by DevWeb itself.
const chunks = [];
if (require.main === module) process.stdin.on('data', (c) => chunks.push(c));
if (require.main === module) process.stdin.on('end', async () => {
  const req = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  try {
    const out = await doFetch(req);
    process.stdout.write(JSON.stringify(out));
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: String(e && e.cause ? e.cause : e) }));
  }
});

async function doFetch(req) {
  const init = { method: req.method, headers: req.headers || {}, redirect: req.noRedirect ? 'manual' : 'follow' };
  if (req.bodyB64 !== undefined) init.body = Buffer.from(req.bodyB64, 'base64');
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), (req.timeoutS || 120) * 1000);
  init.signal = ac.signal;
  const res = await fetch(req.url, init);
  const buf = Buffer.from(await res.arrayBuffer());
  clearTimeout(timer);
  const headers = {};
  res.headers.forEach((v, k) => { headers[k] = v; });
  return { status: res.status, headers, bodyB64: buf.toString('base64') };
}
module.exports = { doFetch };
