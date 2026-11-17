"""terraform/ must build what template.yaml builds. Fails when one changes without the other."""
import os
import subprocess
import sys

import pytest

pytest.importorskip('hcl2', reason='pip install python-hcl2 to check terraform/ against template.yaml')

ROOT = os.path.join(os.path.dirname(__file__), '..')


def test_terraform_matches_template():
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'terraform', 'tests', 'check_parity.py')],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
