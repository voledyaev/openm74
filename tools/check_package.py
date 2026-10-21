#!/usr/bin/env python3
"""Install the built wheel outside the checkout and check its shipped resources.

Run after `uv build`. Editable installs can hide both broken wheel configuration and
missing package data, so this check uses an isolated interpreter and Python's -I mode.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    wheels = list((ROOT / "dist").glob("openm74-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("expected one wheel in dist/; remove old wheels and run uv build")
    code = """
from importlib.resources import files
from openm74 import klinebsl as K
assert K.check_monitor_blob() is True, 'monitor blob missing or inconsistent'
for name in ('stage1recv.bin', 'stage2dump.bin', 'stage2mon.bin'):
    assert files('openm74').joinpath('stages', name).read_bytes(), name
for name in ('openm74.png', 'openm74.ico', 'openm74.icns'):
    assert files('openm74').joinpath('icons', name).read_bytes(), name
import io
try:
    K.main(['--help'], log=io.StringIO())
except SystemExit as e:
    assert e.code == 0
else:
    raise AssertionError('--help did not exit')
print('Installed wheel: imports, resources and entry point OK')
"""
    # argparse exits with 0 on --help; any preceding failed check must exit nonzero.
    with tempfile.TemporaryDirectory(prefix="openm74-package-") as tmp:
        subprocess.run(["uv", "run", "--no-project", "--isolated", "--with",
                        str(wheels[0]), "python", "-I", "-c", code], cwd=tmp, check=True)


if __name__ == "__main__":
    main()
