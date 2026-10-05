"""
Per-request scratch space. Every fetched and generated file lives in one
temporary directory that is removed when the request finishes — on success,
on error, always. Nothing is kept between requests (the app is stateless), so
there is no session to expire and nothing left after the ZIP is downloaded.

`FSGDPR_TMP_ROOT` relocates the directories (tests point it at a pytest tmp
dir to assert that it is empty afterwards).
"""
import contextlib
import os
import shutil
import tempfile
from pathlib import Path


@contextlib.contextmanager
def workspace():
    root = os.environ.get("FSGDPR_TMP_ROOT") or None
    path = Path(tempfile.mkdtemp(prefix="fsgdpr-", dir=root))
    try:
        os.chmod(path, 0o700)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
