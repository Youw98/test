"""Phase-2 network feasibility via power-grid-model.

Skipped when the grid extra is not installed, because the MVP must run without it.
"""

from __future__ import annotations

import pytest

from kasflex.adapters.grid import FeederSpec, check_feeder

pytest.importorskip("power_grid_model", reason="install with: pip install 'kasflex[grid]'")


def test_light_loading_is_feasible():
    result = check_feeder([[200.0, 200.0, 200.0] for _ in range(24)])
    assert result.feasible, [v.kind for v in result.violations]
    assert 0.9 < result.min_u_pu <= 1.0


def test_heavy_loading_overloads_the_feeder():
    """Three 5 ha greenhouses lighting simultaneously is the congestion story."""
    result = check_feeder([[6000.0, 6000.0, 6000.0] for _ in range(24)])
    assert not result.feasible
    kinds = {v.kind for v in result.violations}
    assert "line_overload" in kinds or "undervoltage" in kinds
    assert result.max_loading > 1.0


def test_staggering_relieves_what_simultaneity_causes():
    """The whole premise: the same energy, spread out, fits where it did not before."""
    simultaneous = [[5000.0, 5000.0, 5000.0] if h == 17 else [100.0] * 3 for h in range(24)]
    staggered = [
        [5000.0 if h == 16 else 100.0, 5000.0 if h == 17 else 100.0,
         5000.0 if h == 18 else 100.0]
        for h in range(24)
    ]
    assert len(check_feeder(staggered).violations) < len(check_feeder(simultaneous).violations)


def test_export_is_modelled_as_negative_power():
    result = check_feeder([[-1500.0, -1500.0, -1500.0] for _ in range(24)])
    assert result.max_loading > 0


def test_profile_shape_is_validated():
    with pytest.raises(ValueError, match="connection values"):
        check_feeder([[100.0, 100.0] for _ in range(24)], FeederSpec(n_connections=3))
    with pytest.raises(ValueError, match="at least one hour"):
        check_feeder([])


def test_feeder_spec_is_configurable():
    weak = FeederSpec(n_connections=2, segment_rating_a=50.0)
    result = check_feeder([[2000.0, 2000.0] for _ in range(24)], weak)
    assert not result.feasible
    assert result.feeder.n_connections == 2
