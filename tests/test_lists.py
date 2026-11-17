"""pytest entry point for the Node.js lists service.

The real tests are in tests/node/lists.test.js (node:test + moto_server);
this wrapper runs them so `pytest v1/tests` covers every service.

    cd v1/tests/node && npm install && node --test     # direct
    pytest v1/tests/test_lists.py                       # via pytest
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NODE_DIR = Path(__file__).parent / "node"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_lists_node_suite():
    if not (NODE_DIR / "node_modules" / "@aws-sdk" / "client-dynamodb").exists():
        subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=NODE_DIR, check=True)
    env = dict(os.environ)
    env.pop("AWS_ENDPOINT_URL", None)          # the suite starts its own moto_server
    env.setdefault("PYTHON", sys.executable)    # ...with this interpreter (moto_serial.py)
    proc = subprocess.run(
        ["node", "--test", "lists.test.js"],
        cwd=NODE_DIR, env=env, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-20000:] + proc.stderr[-5000:]
