import subprocess
import sys


def test_importing_discover_does_not_load_scrapy() -> None:
    code = (
        "import sys, qbkb.discover, qbkb.fetch, qbkb.fetch.settings, qbkb.fetch.outcome;"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in ('scrapy', 'twisted'));"
        "print(','.join(loaded))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert proc.stdout.strip() == ""
