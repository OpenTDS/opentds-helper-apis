"""Session setup shared by every test file.

Every response the services return during the run is appended to a temporary
CONTRACT_LOG (see common.run in src/common.py and src/common.js);
tests/test_zz_contract.py, which runs last, checks each one against the
published OpenAPI documents.
"""
import os
import tempfile


def pytest_configure(config):
    if not os.environ.get('CONTRACT_LOG'):
        fd, path = tempfile.mkstemp(prefix='contract-', suffix='.jsonl')
        os.close(fd)
        os.environ['CONTRACT_LOG'] = path
        config._contract_log_tmp = path


def pytest_unconfigure(config):
    path = getattr(config, '_contract_log_tmp', None)
    if path and os.path.exists(path):
        os.remove(path)
