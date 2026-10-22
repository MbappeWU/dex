"""The shipped values-free contract, run against this release (#591).

The contract lives in the package so a host can run it against the release it
pins; this is the same suite run here, so a change that breaks the guarantee
fails this repository's CI before it reaches a host's.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from exmergo_dex_core.values_free_conformance import ValuesFreeContract


class TestValuesFreeConformance(ValuesFreeContract):
    pass
