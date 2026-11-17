// The lists/upload page: one HTML page for CSV and JSON. ?format=csv|json
// preselects the format; the page lets the user switch. All calls go to the
// v1 URLs under the injected base (BASE_URL).
const { KEY_RE } = require('./common');

// JSON for embedding inside <script>: '<' escaped so no value can close the tag.
const js = (v) => JSON.stringify(v).replace(/</g, '\\u003c');

function renderPage({ format, base, limits }) {
  const L = limits;
  return PAGE
    .replace('__CONFIG__', () => js({
      format,
      base,
      keyRe: KEY_RE.source,
      maxBytes: L.MAX_FILE_KB * 1024,
    }))
    .replace(/__MAXMB__/g, String(L.MAX_FILE_KB / 1024))
    .replace(/__MAXBODYKB__/g, String(L.MAX_BODY_KB))
    .replace(/__MAXRECORDKB__/g, String(L.MAX_RECORD_BYTES / 1024))
    .replace(/__MAXRECORDS__/g, L.MAX_RECORDS.toLocaleString('en-US'));
}

const PAGE = String.raw`<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test-data loader</title>
<style>
body{font-family:system-ui,'Segoe UI',Roboto,sans-serif;background:#111827;color:#e5e7eb;margin:0;padding:2rem 1rem;line-height:1.5}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:1.4rem;margin:0 0 .3rem}
h2{font-size:1.05rem;margin:2rem 0 .6rem;padding-top:1rem;border-top:1px solid #374151}
h3{font-size:.92rem;margin:1.2rem 0 .4rem;color:#d1d5db}
p,li{font-size:.9rem}
p.sub{color:#9ca3af;margin:0 0 1rem}
a{color:#93c5fd}
code{background:#1f2937;padding:.08rem .35rem;border-radius:4px;font-size:.84em;font-family:ui-monospace,Consolas,monospace}
pre{background:#0b1220;border:1px solid #374151;border-radius:8px;padding:.8rem;overflow-x:auto;font-size:.8rem;font-family:ui-monospace,Consolas,monospace;margin:.4rem 0 .8rem;white-space:pre}
ul{padding-left:1.2rem;margin:.3rem 0}
li{margin:.2rem 0}
.warn{border-left:4px solid #f59e0b;background:#1f2937;padding:.7rem 1rem;border-radius:0 8px 8px 0;margin:1rem 0;font-size:.9rem}
.card{background:#1f2937;border:1px solid #374151;border-radius:12px;padding:1.2rem;margin-top:1rem}
label{display:block;font-size:.82rem;color:#9ca3af;margin:.2rem 0 .25rem}
input.txt,select{background:#111827;border:1px solid #4b5563;color:#e5e7eb;border-radius:6px;padding:.45rem .55rem;font-family:ui-monospace,Consolas,monospace;font-size:.88rem;width:100%;box-sizing:border-box}
.row{display:flex;gap:1rem;flex-wrap:wrap}
.row > div{flex:1 1 220px}
#drop{border:2px dashed #4b5563;border-radius:12px;padding:2rem 1rem;text-align:center;cursor:pointer;color:#9ca3af;margin:1rem 0;transition:border-color .15s,background .15s}
#drop.hover{border-color:#f59e0b;background:#111827;color:#e5e7eb}
#drop b{color:#e5e7eb}
button{background:#f59e0b;color:#111827;border:0;border-radius:8px;padding:.55rem 1.1rem;font-weight:600;cursor:pointer;font-size:.88rem}
button.sec{background:#374151;color:#e5e7eb;padding:.3rem .7rem;font-size:.78rem}
button:disabled{opacity:.4;cursor:default}
.ok{color:#34d399}.err{color:#f87171}.dim{color:#9ca3af}
.bar{height:6px;background:#374151;border-radius:3px;overflow:hidden;margin:.6rem 0}
.bar > i{display:block;height:100%;width:0;background:#f59e0b;transition:width .2s linear}
table{width:100%;border-collapse:collapse;margin:.4rem 0}
th,td{padding:.35rem .5rem;text-align:left;border-bottom:1px solid #374151;font-size:.84rem;vertical-align:top}
th{color:#9ca3af;font-weight:600;white-space:nowrap}
td.url{font-family:ui-monospace,Consolas,monospace;font-size:.78rem;word-break:break-all}
.tabs{margin:.2rem 0 .6rem}
.tabs button{background:#374151;color:#e5e7eb;border-radius:6px 6px 0 0;padding:.35rem .8rem;font-size:.8rem;margin-right:.2rem}
.tabs button.on{background:#f59e0b;color:#111827}
.fmt{display:inline-flex;border:1px solid #4b5563;border-radius:8px;overflow:hidden}
.fmt button{background:transparent;color:#e5e7eb;border-radius:0;padding:.45rem 1rem}
.fmt button.on{background:#f59e0b;color:#111827}
[hidden]{display:none!important}
</style></head>
<body><div class="wrap">

<h1>Test-data loader: <span class="fmtName">CSV</span></h1>
<p class="sub">Load a CSV or JSON file of test records into a <code>lists</code> key, then hand them out one at a
time with <code>lists/pop</code> (each record once), <code>lists/random</code> (a random one) and
<code>lists/reset</code> (start over).</p>

<div class="fmt" role="tablist" aria-label="File format">
  <button type="button" data-set-fmt="csv">CSV</button><button type="button" data-set-fmt="json">JSON</button>
</div>

<div class="warn"><b>Loading always replaces.</b> Everything already stored under the key is swapped out for the file's
records, and the pop pointer goes back to record 1. The swap happens in one step: until then, anything reading the key
sees the old records. After it, it sees the new ones, never a mix. If the import fails, nothing changes.</div>

<h2>1. Prepare the file</h2>
<div data-fmt="csv">
<pre>email,customer_id,first_name,last_name,postcode
alice@example.com,51824753556,Alice,Nguyen,0800
bob@example.com,33102417032,Bob,"O'Brien, Jr",3000
carol@example.com,53004085616,Carol,"Says ""hi""",2600</pre>
<ul>
<li><b>The first row must be the header.</b> Its names become the field names of every record (trimmed; a blank name
becomes <code>col3</code>, a repeated one <code>email_2</code>). <code>key</code>, <code>index</code> and
<code>createdTime</code> are reserved (every record already has them), so such a column is renamed
<code>key_2</code> and reported.</li>
<li><b>Delimiter:</b> comma, semicolon, tab or pipe, auto-detected from the header row, or choose it below.</li>
<li><b>Quoting:</b> wrap a value in double quotes if it contains the delimiter, a quote or a line break, and double any
quote inside it (<code>"Says ""hi"""</code>). Excel does this for you.</li>
<li><b>Every value is stored as text, exactly as written.</b> Nothing is trimmed or converted, so leading zeros survive
(<code>0800</code>, phone numbers, IDs).</li>
<li><b>Encoding:</b> UTF-8 (with or without BOM), Excel's plain <i>CSV (Comma delimited)</i> (Windows-1252), or
UTF-16 <i>Unicode Text</i>. All three are detected automatically.</li>
<li><b>Blank lines are ignored.</b> A row with fewer values than the header is loaded with the missing trailing values
empty (and reported as a warning). A row with <i>extra</i> non-empty values is skipped and reported by line number,
because it usually means an unquoted delimiter shifted the columns.</li>
<li><b>A quote that is never closed fails the whole import</b> (and changes nothing), because everything after it
would otherwise be read as one value.</li>
</ul>
</div>
<div data-fmt="json">
<pre>[
  { "email": "alice@example.com", "customerId": "51824753556", "age": 34, "roles": ["admin"] },
  { "email": "bob@example.com",   "customerId": "33102417032", "age": 51, "address": { "postcode": "3000" } }
]</pre>
<ul>
<li><b>Accepted shapes:</b> an array of objects (above); <b>JSON Lines</b> (one object per line, no surrounding
<code>[ ]</code>); an object wrapping one array (<code>{"records":[ ... ]}</code>, as many APIs return it); or a single
object, which loads as <b>one</b> record.</li>
<li><b>Each object is one record,</b> stored as-is: strings, numbers, booleans, nulls, nested objects and arrays all keep
their JSON types. A top-level property named <code>key</code>, <code>index</code> or <code>createdTime</code> is
renamed (<code>key_2</code>) and reported, because every record already has those.</li>
<li><b>Put IDs, account numbers and postcodes in quotes.</b> A bare integer too large for a JavaScript number
(over 9007199254740991) is kept exactly, as a string, and reported. But a quoted value is never ambiguous.</li>
<li>An item that is not an object (<code>"abc"</code>, <code>42</code>) is stored as <code>{"value": ...}</code>.</li>
<li><b>Encoding:</b> UTF-8, or UTF-16 with a byte-order mark (which is what Windows PowerShell 5.1's
<code>Out-File</code> writes).</li>
<li><b>From PowerShell:</b> <code>ConvertTo-Json</code> defaults to a depth of 2 and flattens anything deeper into
text, so always pass <code>-Depth 10</code>. A one-item collection becomes a bare object: use
<code>-AsArray</code> (PowerShell 7) or <code>ConvertTo-Json -InputObject @($rows)</code>.</li>
<li>A record with a property whose name is empty, or a number beyond about 1e125, is skipped and reported.</li>
</ul>
</div>

<h3>Limits</h3>
<ul>
<li>File: up to <b>__MAXMB__ MB</b> through this page or <code>lists/upload/url</code>; up to <b>__MAXBODYKB__ KB</b>
when POSTed straight to <code>lists/import</code> in one call.</li>
<li>Each record: up to <b>__MAXRECORDKB__ KB</b>. Records: up to __MAXRECORDS__ per file.</li>
<li>Speed: bulk writes run in parallel across several partitions. 100,000 small records typically load in well under
a minute, and 1,000 in a second or two.</li>
<li>Uploaded files are kept for 90 days, so a key can be reloaded from the same file (by its <code>ref</code>) without
uploading it again.</li>
</ul>

<h2>2. Load it</h2>
<div class="card">
  <div class="row">
    <div><label for="key">Key (letters, digits and <code>. _ - : @ + =</code>, up to 128)</label>
      <input class="txt" id="key" placeholder="e.g. run3-customers" spellcheck="false" autocomplete="off"></div>
    <div data-fmt="csv"><label for="delim">Delimiter</label>
      <select id="delim">
        <option value="">Auto-detect</option>
        <option value="comma">Comma  ,</option>
        <option value="semicolon">Semicolon  ;</option>
        <option value="tab">Tab</option>
        <option value="pipe">Pipe  |</option>
      </select></div>
  </div>
  <div id="drop">Drop a <b>.<span class="fmtExt">csv</span></b> file here, or click to choose one
    <input type="file" id="fi" hidden></div>
  <p id="chosen" class="dim" hidden></p>
  <button id="go" disabled>Upload and load</button>
  <div class="bar" id="bar" hidden><i></i></div>
  <p id="state" class="dim"></p>

  <div id="result" hidden>
    <h3>Result</h3>
    <table id="summary"></table>
    <div id="warnBox" hidden><h3>Warnings</h3><ul id="warnList"></ul></div>
    <div id="skipBox" hidden><h3>Skipped records</h3><p class="dim" id="skipNote"></p>
      <table><thead><tr><th id="skipAt">Line</th><th>Reason</th></tr></thead><tbody id="skipBody"></tbody></table></div>
    <div id="sampleBox" hidden><h3>Record 1, as <code>lists/pop</code> returns it</h3><pre id="sample"></pre></div>
    <div id="linkBox" hidden><h3>Ready-to-use URLs</h3><table id="links"></table></div>
  </div>
</div>

<h2>3. Use the records</h2>
<table>
<tr><th><code>lists/pop?key=YOURKEY</code></th><td>The next unused record; each record is handed out once, across all
virtual users. <code>"exhausted": true</code> when they have all been used.</td></tr>
<tr><th><code>lists/random?key=YOURKEY</code></th><td>A random record. Does not use it up.</td></tr>
<tr><th><code>lists/reset?key=YOURKEY</code></th><td>Rewind, so <code>pop</code> starts again at record 1. The records are untouched.</td></tr>
<tr><th><code>lists/list?key=YOURKEY&amp;limit=10</code></th><td>Look at what is loaded. Large keys come back in pages:
follow <code>nextStart</code> with <code>&amp;start=N</code>.</td></tr>
<tr><th><code>lists/clear?key=YOURKEY</code></th><td>Delete every record under the key.</td></tr>
</table>
<p>A <code>pop</code> response. Your file's fields sit in <code>record</code> next to <code>key</code>,
<code>index</code> and <code>createdTime</code>:</p>
<pre class="ex">{
  "key": "YOURKEY",
  "exhausted": false,
  "index": 1,
  "count": 3,
  "remaining": 2,
  "record": {
    "email": "alice@example.com",
    "customer_id": "51824753556",
    "key": "YOURKEY",
    "index": 1,
    "createdTime": 1790000000
  }
}</pre>
<h3>LoadRunner</h3>
<pre class="ex">web_reg_save_param_json("ParamName=email", "QueryString=$.record.email", SEARCH_FILTERS, "Scope=Body", LAST);
web_reg_save_param_json("ParamName=exhausted", "QueryString=$.exhausted", SEARCH_FILTERS, "Scope=Body", LAST);
web_url("lists_pop", "URL=BASEURL/lists/pop?key=YOURKEY", "Resource=0", "Mode=HTTP", LAST);</pre>
<h3>PowerShell</h3>
<pre class="ex">$rec = Invoke-RestMethod "BASEURL/lists/pop?key=YOURKEY"
if ($rec.exhausted) { throw "out of test data" }
$rec.record.email</pre>

<h2>4. Load from a script (pre-test step)</h2>
<p>The same thing without the page. The import runs in the background. By default the call waits up to 25 seconds
and returns the final result if the load is done by then, which it is for anything up to tens of thousands of records.
Otherwise it returns <code>"state": "running"</code> with a <code>ref</code> to poll at <code>lists/import/status</code>.
<b>Always check <code>state</code></b>: <code>done</code> (HTTP 200), <code>queued</code>/<code>running</code>
(HTTP 202) or <code>failed</code> (HTTP 422, with <code>error</code>). <code>&amp;wait=0</code> returns at once;
<code>&amp;wait=N</code> waits up to N seconds.</p>
<div class="tabs"><button data-t="ps" class="on">PowerShell</button><button data-t="curl">curl</button></div>
<div data-p="ps">
<h3>Files up to __MAXBODYKB__ KB: one call</h3>
<pre class="ex">$base = "BASEURL"
try {
  $p = @{ Method = "Post"; Uri = "$base/lists/import?key=YOURKEY&amp;format=FMT"; InFile = ".\records.FMT"
          ContentType = "CTYPE" }
  $r = Invoke-RestMethod @p
  while ($r.state -in "queued","running") {
    Start-Sleep 2
    $r = Invoke-RestMethod "$base/lists/import/status?key=YOURKEY&amp;ref=$($r.ref)"
  }
} catch { throw "Import failed: $($_.ErrorDetails.Message)" }   # HTTP 422 carries the reason
"Loaded $($r.loaded) records into YOURKEY; skipped $($r.skippedCount)"</pre>
<h3>Bigger files (up to __MAXMB__ MB): upload to S3, then import. Needs PowerShell 7 for <code>-Form</code></h3>
<pre class="ex">$base = "BASEURL"
$file = Get-Item .\records.FMT
$u = Invoke-RestMethod "$base/lists/upload/url?key=YOURKEY&amp;format=FMT&amp;size=$($file.Length)"
$form = [ordered]@{}                                   # order matters: policy fields first, file last
$u.fields.PSObject.Properties | ForEach-Object { $form[$_.Name] = $_.Value }
$form.file = $file
Invoke-RestMethod -Method Post -Uri $u.url -Form $form | Out-Null
$r = Invoke-RestMethod $u.next
while ($r.state -in "queued","running") {
  Start-Sleep 2
  $r = Invoke-RestMethod "$base/lists/import/status?key=YOURKEY&amp;ref=$($u.ref)"
}
"Loaded $($r.loaded) records; skipped $($r.skippedCount)"</pre>
</div>
<div data-p="curl" hidden>
<h3>Files up to __MAXBODYKB__ KB: one call</h3>
<pre class="ex">curl -sS -X POST -H "Content-Type: CTYPE" --data-binary @records.FMT \
  "BASEURL/lists/import?key=YOURKEY&amp;format=FMT"</pre>
<h3>Bigger files (up to __MAXMB__ MB): upload to S3, then import (needs <code>jq</code>)</h3>
<pre class="ex">U=$(curl -sS "BASEURL/lists/upload/url?key=YOURKEY&amp;format=FMT")
curl -sS $(echo "$U" | jq -r '.fields | to_entries[] | "-F \(.key)=\(.value)"') \
  -F file=@records.FMT "$(echo "$U" | jq -r .url)"
curl -sS "$(echo "$U" | jq -r .next)"</pre>
<h3>Poll, if the answer was <code>running</code></h3>
<pre class="ex">curl -sS "BASEURL/lists/import/status?key=YOURKEY&amp;ref=REF"</pre>
</div>
<p data-fmt="csv">Semicolon, tab or pipe file and don't want to rely on auto-detection? Add
<code>&amp;delimiter=semicolon</code> (or <code>tab</code>, <code>pipe</code>) to the import URL.</p>
<p><code>lists/import/status?key=YOURKEY</code> with no <code>ref</code> reports the newest import for the key and
lists recent uploads. To load one of them again (after a <code>lists/clear</code>, say), call
<code>lists/import?key=YOURKEY&amp;format=FMT&amp;ref=REF</code>. No re-upload is needed.</p>

</div>
<script>
(function(){
  var CFG = __CONFIG__;
  var FORMAT = CFG.format;
  var API = CFG.base.replace(/\/+$/, '') + '/lists';
  var KEY_RE = new RegExp(CFG.keyRe);
  var MAX_BYTES = CFG.maxBytes;
  var CTYPES = { csv: 'text/csv', json: 'application/json' };
  var ACCEPT = { csv: '.csv,.txt,text/csv,text/plain', json: '.json,.jsonl,.ndjson,application/json,text/plain' };
  var $ = function(id){ return document.getElementById(id); };
  var all = function(sel){ return [].slice.call(document.querySelectorAll(sel)); };
  var keyIn = $('key'), drop = $('drop'), fi = $('fi'), go = $('go'), bar = $('bar'), state = $('state');
  var delimSel = $('delim');
  var file = null;

  // Examples: fill in the base URL, the key as it is typed, and the format.
  var exs = all('pre.ex');
  exs.forEach(function(p){ p.setAttribute('data-src', p.textContent); });
  function renderExamples(){
    var k = keyIn.value.trim() || 'YOURKEY';
    exs.forEach(function(p){
      p.textContent = p.getAttribute('data-src').split('BASEURL').join(CFG.base).split('YOURKEY').join(k)
        .split('CTYPE').join(CTYPES[FORMAT]).split('FMT').join(FORMAT);
    });
  }

  function setFormat(f){
    FORMAT = f === 'json' ? 'json' : 'csv';
    all('[data-set-fmt]').forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-set-fmt') === FORMAT); });
    all('[data-fmt]').forEach(function(el){ el.hidden = el.getAttribute('data-fmt') !== FORMAT; });
    all('.fmtName').forEach(function(el){ el.textContent = FORMAT.toUpperCase(); });
    all('.fmtExt').forEach(function(el){ el.textContent = FORMAT; });
    document.title = FORMAT.toUpperCase() + ' test-data loader';
    fi.setAttribute('accept', ACCEPT[FORMAT]);
    try { history.replaceState(null, '', '?format=' + FORMAT); } catch (e) {}
    renderExamples();
  }
  all('[data-set-fmt]').forEach(function(b){
    b.addEventListener('click', function(){ setFormat(b.getAttribute('data-set-fmt')); });
  });

  all('.tabs button').forEach(function(b){
    b.addEventListener('click', function(){
      all('.tabs button').forEach(function(x){ x.classList.toggle('on', x === b); });
      all('[data-p]').forEach(function(d){ d.hidden = d.getAttribute('data-p') !== b.getAttribute('data-t'); });
    });
  });

  function keyOk(){ return KEY_RE.test(keyIn.value.trim()); }
  function refresh(){ go.disabled = !(file && keyOk()); }
  keyIn.addEventListener('input', function(){ renderExamples(); refresh(); });

  function fmt(b){ return b >= 1048576 ? (b / 1048576).toFixed(2) + ' MB' : Math.max(1, Math.ceil(b / 1024)) + ' KB'; }
  function setState(cls, text){ state.className = cls; state.textContent = text; }
  function progress(f){ bar.hidden = false; bar.firstChild.style.width = Math.round(f * 100) + '%'; }

  function choose(f){
    if (!f) return;
    file = f;
    var c = $('chosen'); c.hidden = false;
    c.textContent = f.name + ' (' + fmt(f.size) + ')';
    if (f.size > MAX_BYTES) { c.className = 'err'; c.textContent += ' is larger than the ' + fmt(MAX_BYTES) + ' limit'; file = null; }
    else c.className = 'dim';
    // A file whose extension names the other format switches the page to it.
    var m = /\.(csv|json|jsonl|ndjson)$/i.exec(f.name);
    if (m) setFormat(m[1].toLowerCase() === 'csv' ? 'csv' : 'json');
    refresh();
  }
  drop.addEventListener('click', function(){ fi.click(); });
  fi.addEventListener('change', function(){ choose(fi.files[0]); fi.value = ''; });
  ['dragenter','dragover'].forEach(function(ev){ drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.add('hover'); }); });
  ['dragleave','drop'].forEach(function(ev){ drop.addEventListener(ev, function(e){ e.preventDefault(); drop.classList.remove('hover'); }); });
  drop.addEventListener('drop', function(e){ if (e.dataTransfer && e.dataTransfer.files.length) choose(e.dataTransfer.files[0]); });

  function getJson(url){
    return fetch(url).then(function(r){ return r.json().then(function(j){ return { ok: r.ok, code: r.status, j: j }; }); });
  }
  function busy(on){
    go.disabled = on; keyIn.disabled = on; if (delimSel) delimSel.disabled = on;
    all('[data-set-fmt]').forEach(function(b){ b.disabled = on; });
  }

  go.addEventListener('click', function(){
    var key = keyIn.value.trim();
    if (!file || !keyOk()) return;
    var format = FORMAT;
    busy(true); $('result').hidden = true;
    setState('dim', 'Requesting an upload URL...');
    getJson(API + '/upload/url?key=' + encodeURIComponent(key) + '&format=' + format + '&size=' + file.size)
      .then(function(res){
        if (!res.ok) throw new Error(res.j.error || ('HTTP ' + res.code));
        return upload(res.j).then(function(){ return res.j.ref; });
      })
      .then(function(ref){
        setState('dim', 'Uploaded. Starting the import...');
        var d = format === 'csv' && delimSel.value ? '&delimiter=' + delimSel.value : '';
        return getJson(API + '/import?key=' + encodeURIComponent(key) + '&format=' + format + '&ref=' + ref + '&wait=0' + d)
          .then(function(res){
            if (res.code >= 400 && res.code !== 422) throw new Error(res.j.error || ('HTTP ' + res.code));
            return poll(key, ref, res.j);
          });
      })
      .catch(function(e){ setState('err', e.message || String(e)); bar.hidden = true; busy(false); });
  });

  function upload(ps){
    return new Promise(function(resolve, reject){
      // Field order matters to S3: every policy field first, the file last.
      var fd = new FormData();
      Object.keys(ps.fields).forEach(function(k){ fd.append(k, ps.fields[k]); });
      fd.append('file', file, file.name);
      var xhr = new XMLHttpRequest();
      xhr.open('POST', ps.url, true);
      setState('dim', 'Uploading ' + fmt(file.size) + '...');
      progress(0);
      xhr.upload.onprogress = function(e){ if (e.lengthComputable) progress(e.loaded / e.total); };
      xhr.onload = function(){
        if (xhr.status >= 200 && xhr.status < 300) return resolve();
        var m = /<Message>([^<]*)<\/Message>/.exec(xhr.responseText || '');
        reject(new Error(m ? m[1] : 'S3 rejected the upload (HTTP ' + xhr.status + ')'));
      };
      xhr.onerror = function(){ reject(new Error('Network error during upload (check the bucket CORS).')); };
      xhr.send(fd);
    });
  }

  function poll(key, ref, st){
    if (st.state === 'done' || st.state === 'failed') return finish(st);
    if (st.phase === 'writing' && st.total) {
      progress((st.loaded || 0) / st.total);
      setState('dim', 'Writing records: ' + (st.loaded || 0).toLocaleString() + ' of ' + st.total.toLocaleString() + '...');
    } else {
      progress(0.02);
      setState('dim', st.state === 'queued' ? 'Queued...' : 'Reading and checking the file...');
    }
    return new Promise(function(r){ setTimeout(r, 1000); })
      .then(function(){ return getJson(API + '/import/status?key=' + encodeURIComponent(key) + '&ref=' + ref); })
      .then(function(res){ return poll(key, ref, res.j); });
  }

  function cell(tr, tag, text, cls){ var c = document.createElement(tag); c.textContent = text; if (cls) c.className = cls; tr.appendChild(c); return c; }
  function addRow(tbl, k, v){ var tr = document.createElement('tr'); cell(tr, 'th', k); cell(tr, 'td', v); tbl.appendChild(tr); }

  function finish(st){
    busy(false); bar.hidden = true;
    $('result').hidden = false;
    var sum = $('summary'); sum.textContent = '';
    if (st.state === 'failed') {
      setState('err', 'Import failed. ' + (st.message || ''));
      addRow(sum, 'Error', st.error || 'unknown');
    } else {
      setState('ok', 'Loaded ' + st.loaded.toLocaleString() + ' records into "' + st.key + '" in ' + (st.durationMs / 1000).toFixed(1) + ' s.');
      addRow(sum, 'Key', st.key);
      addRow(sum, 'Records loaded', st.loaded.toLocaleString() + '  (index 1 to ' + st.lastIndex.toLocaleString() + ')');
      addRow(sum, 'Skipped', String(st.skippedCount || 0));
      if (st.replaced) addRow(sum, 'Replaced', st.replaced.count.toLocaleString() + ' previous records (being deleted in the background)');
      if (st.columns) addRow(sum, 'Fields', st.columns.join(', '));
      if (st.delimiter) addRow(sum, 'Delimiter', st.delimiter);
      if (st.shape) addRow(sum, 'Shape', st.shape);
      addRow(sum, 'Encoding', st.encoding || '');
      addRow(sum, 'Reference', st.ref + '  (kept 90 days; reload with the URL below)');
    }
    var w = st.warnings || [];
    $('warnBox').hidden = !w.length;
    var wl = $('warnList'); wl.textContent = '';
    w.forEach(function(x){ var li = document.createElement('li'); li.textContent = x; wl.appendChild(li); });

    var sk = st.skipped || [];
    $('skipBox').hidden = !sk.length;
    $('skipNote').textContent = st.skippedNote || '';
    $('skipAt').textContent = sk.length && sk[0].item ? 'Item' : 'Line';
    var sb = $('skipBody'); sb.textContent = '';
    sk.forEach(function(s){ var tr = document.createElement('tr'); cell(tr, 'td', String(s.line || s.item)); cell(tr, 'td', s.reason); sb.appendChild(tr); });

    $('sampleBox').hidden = !st.sample;
    if (st.sample) $('sample').textContent = JSON.stringify(st.sample, null, 2);

    var lt = $('links'); lt.textContent = '';
    $('linkBox').hidden = st.state !== 'done';
    if (st.state === 'done' && st.next) {
      ['pop', 'random', 'reset', 'list', 'reload'].forEach(function(name){
        var url = st.next[name];
        if (!url) return;
        var tr = document.createElement('tr');
        cell(tr, 'th', name);
        var td = cell(tr, 'td', '', 'url');
        var a = document.createElement('a'); a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = url;
        td.appendChild(a);
        var bt = document.createElement('button'); bt.className = 'sec'; bt.textContent = 'copy';
        bt.addEventListener('click', function(){
          if (navigator.clipboard) navigator.clipboard.writeText(url).then(function(){ bt.textContent = 'copied'; });
          else window.prompt('URL:', url);
        });
        var tdb = document.createElement('td'); tdb.appendChild(bt); tr.appendChild(tdb);
        lt.appendChild(tr);
      });
    }
  }

  setFormat(FORMAT);
})();
</script></body></html>
`;

module.exports = { renderPage };
