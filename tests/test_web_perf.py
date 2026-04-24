import math
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from web.lp_web import _annualized_twr_apr_pct  # noqa: E402


def test_annualized_twr_apr_hides_short_samples():
    apr = _annualized_twr_apr_pct(1.01, 3600)
    assert apr is None


def test_annualized_twr_apr_is_finite_for_sensible_window():
    apr = _annualized_twr_apr_pct(1.10, 30 * 86400)
    assert apr is not None
    assert math.isfinite(apr)
    assert 100 < apr < 500


def test_annualized_twr_apr_rejects_invalid_inputs():
    assert _annualized_twr_apr_pct(float("inf"), 30 * 86400) is None
    assert _annualized_twr_apr_pct(1.05, float("nan")) is None
    assert _annualized_twr_apr_pct(-1.0, 30 * 86400) is None
