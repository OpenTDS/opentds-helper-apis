"""voice_page - the page behind voice/say?play=true and voice/get?play=true.

Opening an audio URL in a browser shows the browser's own player. This page
plays the audio from script instead, so nothing but the words is on screen.

Whether a page may make sound without a click is the browser's decision, not
ours: it is allowed when the visitor has already interacted with the site (a
click on a link that led here), and refused otherwise. So the page tries to
play at once and, when the browser refuses (NotAllowedError), shows one Play
button. Automated browsers lift the rule with
--autoplay-policy=no-user-gesture-required.

Everything a caller supplied (the words, the key) is HTML-escaped, the audio
URL goes into the script as an escaped JSON string, and the response carries a
Content-Security-Policy that allows only this page's own script and style
(by nonce): the endpoint is open, so the page must be safe to link to with any
text in it.
"""
import html
import json
import re
import secrets

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style nonce="__NONCE__">
:root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--line:#e3e3e0;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#191918;--fg:#eeeeec;--mut:#9a9a96;--line:#33332f;--card:#222220}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:2rem 1rem}
.wrap{max-width:44rem;margin:0 auto}
h1{font-size:1.05rem;font-weight:600;margin:0 0 1.25rem;letter-spacing:-.01em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.5rem}
.said{margin:0;font-size:1.2rem;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}
.status{margin:1rem 0 0;color:var(--mut);min-height:1.55em}
button{margin:.9rem 0 0;padding:.6rem 1.4rem;border:1px solid var(--line);border-radius:8px;background:var(--card);
color:var(--fg);font:15px ui-sans-serif,system-ui,sans-serif;cursor:pointer}
button:hover{border-color:var(--mut)}
dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(7rem,1fr));gap:.9rem;margin:1.5rem 0 0;
padding-top:1.25rem;border-top:1px solid var(--line)}
dt{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:0 0 .15rem}
dd{margin:0;font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
p.foot{color:var(--mut);font-size:12.5px;margin:1.25rem 0 0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
</style></head><body><div class="wrap">
<h1>voice</h1>
<div class="card">
<p class="said">__SAID__</p>
<p class="status" id="status" role="status" aria-live="polite">Loading&hellip;</p>
<button id="play" type="button" hidden>Play</button>
<dl>__FACTS__</dl>
</div>
<p class="foot">This page plays the audio from script, so there is no player. A browser only
lets a page make sound once you have clicked something on the site; when it refuses, a Play
button appears. For the audio file itself use <code>view=true</code>; for a script, leave
<code>play</code> off and read the JSON.</p>
</div>
<script nonce="__NONCE__">
(function () {
  var src = __SRC__;
  var status = document.getElementById('status');
  var button = document.getElementById('play');
  var audio = new Audio();
  audio.preload = 'auto';
  function tell(text) { status.textContent = text; }
  function offer(label) { button.textContent = label; button.hidden = false; button.focus(); }
  function refused(e) {
    if (e && e.name === 'AbortError') { return; }
    if (e && e.name === 'NotAllowedError') {
      tell('Your browser blocks sound until you click.');
      offer('Play');
    } else {
      tell('Could not play the audio' + (e && e.message ? ': ' + e.message : '.'));
      offer('Try again');
    }
  }
  function start() {
    var attempt;
    button.hidden = true;
    tell('Playing\\u2026');
    try { audio.currentTime = 0; } catch (ignore) {}
    try { attempt = audio.play(); } catch (e) { refused(e); return; }
    if (attempt && attempt.catch) { attempt.catch(refused); }
  }
  audio.addEventListener('ended', function () { tell('Played.'); offer('Play again'); });
  audio.addEventListener('error', function () {
    tell('Could not load the audio. Reload the page for a fresh link.');
  });
  button.addEventListener('click', start);
  audio.src = src;
  start();
})();
</script>
</body></html>
"""

_TOKEN = re.compile(r'__([A-Z]+)__')


def _js_string(value):
    """A JSON string literal that is also safe inside <script>: no '<', '>' or
    '&' survive, so nothing in it can close the element or open a comment."""
    return (json.dumps(str(value)).replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def _length(ms):
    return '%.1f s' % (ms / 1000.0) if isinstance(ms, (int, float)) else None


def render(info, url):
    """-> (html, headers). info: the public description of the audio (the
    fields voice/say and voice/get return); url: where the audio is."""
    said = info.get('text') or info.get('filename') or info.get('key') or 'Audio'
    facts = [('Voice', info.get('voice')), ('Engine', info.get('engine')),
             ('Format', ' '.join(str(x) for x in (info.get('format'),
                                                  info.get('hz') and '%s Hz' % info['hz']) if x)),
             ('Length', _length(info.get('durationMs'))),
             ('Ref', info.get('ref')), ('Key', info.get('key'))]
    nonce = secrets.token_urlsafe(16)
    values = {
        'TITLE': html.escape('voice: ' + said[:80]),
        'SAID': html.escape(said),
        'FACTS': ''.join('<div><dt>%s</dt><dd>%s</dd></div>' % (label, html.escape(str(value)))
                         for label, value in facts if value),
        'SRC': _js_string(url),
        'NONCE': nonce,
    }
    # One pass: a token that appears inside a caller's own text is not substituted again.
    page = _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), PAGE)
    headers = {
        'Content-Security-Policy': ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; "
                                    "media-src https:; base-uri 'none'; form-action 'none'" % (nonce, nonce)),
        'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
    }
    return page, headers
