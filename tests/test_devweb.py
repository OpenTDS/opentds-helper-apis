"""The DevWeb regression scripts (devweb/): built from devweb/_src, valid JavaScript,
and every script uses only endpoints that exist.

The scripts themselves run against a deployment (see devweb/README.md); this
file only checks they are current and well-formed."""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
DEVWEB = os.path.join(ROOT, 'devweb')
SCRIPTS = ['init', 'vars', 'lists', 'names', 'mail', 'sms', 'totp', 'webhook', 'lock', 'qr', 'perf', 'multi']


def test_built_scripts_are_current():
    r = subprocess.run([sys.executable, os.path.join(DEVWEB, 'build.py'), '--check'], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
@pytest.mark.parametrize('name', SCRIPTS)
def test_script_is_valid_javascript(name):
    r = subprocess.run(['node', '--check', os.path.join(DEVWEB, name, 'main.js')], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_scripts_call_documented_paths():
    spec = json.load(open(os.path.join(ROOT, 'src', 'discovery_openapi.json'), encoding='utf-8'))
    paths = set(spec['paths'])
    bad = []
    for name in SCRIPTS:
        src = open(os.path.join(DEVWEB, '_src', name + '.js'), encoding='utf-8').read()
        for p in re.findall(r"path: [`']([^`'$]+)[`']", src):
            if p and p not in paths and not p.startswith('/nosuch') and p not in ('/lists/peek', '/names/random'):
                bad.append((name, p))
    assert not bad, bad
