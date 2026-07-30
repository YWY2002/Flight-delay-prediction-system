"""Smoke test: proves the package is importable and installed correctly.

This is the 'one trivial test' from Phase 0. Its job isn't to test logic -
it's to prove the whole toolchain (uv install -> import -> pytest) works
end to end, so CI has something real to go green on.
"""

import flight_delay


def test_package_has_version() -> None:
    assert isinstance(flight_delay.__version__, str)
    assert flight_delay.__version__ != ""
