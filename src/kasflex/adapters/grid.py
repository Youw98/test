"""Network feasibility through power-grid-model (phase 2).

The MVP checker enforces the greenhouse's own connection contract, which is a
per-site limit. That is the right scope for a single grower, but it cannot answer
the question the Alliander proposal is actually about: whether a *cluster* of
greenhouses, each individually within contract, together overload the feeder.

This adapter answers that. It builds a radial MV feeder in
`power-grid-model <https://github.com/PowerGridModel/power-grid-model>`_ (MPL-2.0)
and runs a batch power flow over the 24 hourly net positions, returning voltage
and line-loading violations. It is imported lazily so that the MVP runs without it.

Requires ``pip install 'kasflex[grid]'``, which pulls ``numpy>=2`` and therefore
cannot share an environment with the GreenLight worker (ADR-0002).
"""

from __future__ import annotations

from dataclasses import dataclass, field


class GridModelUnavailableError(RuntimeError):
    """Raised when power-grid-model is not installed."""


@dataclass(frozen=True)
class FeederSpec:
    """A radial MV feeder with one greenhouse connection per node.

    Attributes:
        u_rated_v: Nominal line voltage of the feeder.
        segment_r_ohm / segment_x_ohm: Impedance of each line segment between
            consecutive connection points.
        segment_rating_a: Thermal current rating of each segment.
        n_connections: Number of greenhouse connections hanging off the feeder.
        power_factor: Assumed displacement power factor at each connection.
        u_min_pu / u_max_pu: Statutory voltage band (EN 50160 uses +/-10 %).
        loading_max: Line loading above which the feeder is considered overloaded.
    """

    u_rated_v: float = 10_500.0
    segment_r_ohm: float = 0.25
    segment_x_ohm: float = 0.12
    segment_rating_a: float = 300.0
    n_connections: int = 3
    power_factor: float = 0.98
    u_min_pu: float = 0.90
    u_max_pu: float = 1.10
    loading_max: float = 1.0


@dataclass(frozen=True)
class GridViolation:
    kind: str
    hour: int
    element: int
    actual: float
    bound: float
    unit: str


@dataclass(frozen=True)
class GridResult:
    violations: tuple[GridViolation, ...]
    min_u_pu: float
    max_loading: float
    feeder: FeederSpec
    metadata: dict[str, float] = field(default_factory=dict)

    @property
    def feasible(self) -> bool:
        return not self.violations


def check_feeder(
    net_kw_per_connection: list[list[float]],
    feeder: FeederSpec | None = None,
) -> GridResult:
    """Run a 24-hour batch power flow and report voltage and loading violations.

    Args:
        net_kw_per_connection: ``net_kw_per_connection[hour][connection]``, positive
            when the greenhouse draws from the grid and negative when it exports.
        feeder: Feeder to test against. Defaults to a three-connection MV feeder.

    Returns:
        A :class:`GridResult`. ``feasible`` is True when no hour breaches the
        voltage band or a line rating.

    Raises:
        GridModelUnavailableError: if ``power-grid-model`` is not installed.
        ValueError: if the profile shape does not match the feeder.
    """
    try:
        import numpy as np  # noqa: PLC0415
        from power_grid_model import (  # noqa: PLC0415
            CalculationMethod,
            CalculationType,
            ComponentType,
            DatasetType,
            LoadGenType,
            PowerGridModel,
            initialize_array,
        )
        from power_grid_model.validation import assert_valid_input_data  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise GridModelUnavailableError(
            "power-grid-model is not installed. Install the grid extra:\n"
            "    pip install 'kasflex[grid]'\n"
            "Note this pulls numpy>=2 and so cannot share an environment with the "
            "GreenLight worker (see docs/DECISIONS.md ADR-0002)."
        ) from exc

    spec = feeder or FeederSpec()
    hours = len(net_kw_per_connection)
    if hours == 0:
        raise ValueError("net_kw_per_connection must contain at least one hour")
    if any(len(row) != spec.n_connections for row in net_kw_per_connection):
        raise ValueError(
            f"every hour must supply {spec.n_connections} connection values, "
            f"to match the feeder specification"
        )

    n_nodes = spec.n_connections + 1  # node 0 is the HV/MV substation busbar

    node = initialize_array(DatasetType.input, ComponentType.node, n_nodes)
    node["id"] = np.arange(n_nodes)
    node["u_rated"] = spec.u_rated_v

    line = initialize_array(DatasetType.input, ComponentType.line, n_nodes - 1)
    line["id"] = np.arange(100, 100 + n_nodes - 1)
    line["from_node"] = np.arange(n_nodes - 1)
    line["to_node"] = np.arange(1, n_nodes)
    line["from_status"] = 1
    line["to_status"] = 1
    line["r1"] = spec.segment_r_ohm
    line["x1"] = spec.segment_x_ohm
    line["c1"] = 1e-7
    line["tan1"] = 0.0
    line["i_n"] = spec.segment_rating_a

    load = initialize_array(DatasetType.input, ComponentType.sym_load, spec.n_connections)
    load["id"] = np.arange(200, 200 + spec.n_connections)
    load["node"] = np.arange(1, n_nodes)
    load["status"] = 1
    load["type"] = LoadGenType.const_power
    load["p_specified"] = 0.0
    load["q_specified"] = 0.0

    source = initialize_array(DatasetType.input, ComponentType.source, 1)
    source["id"] = [300]
    source["node"] = [0]
    source["status"] = [1]
    source["u_ref"] = [1.0]

    input_data = {
        ComponentType.node: node,
        ComponentType.line: line,
        ComponentType.sym_load: load,
        ComponentType.source: source,
    }
    assert_valid_input_data(input_data=input_data, calculation_type=CalculationType.power_flow)
    model = PowerGridModel(input_data)

    p_w = np.asarray(net_kw_per_connection, dtype=float) * 1000.0
    tan_phi = float(np.tan(np.arccos(min(1.0, max(1e-6, spec.power_factor)))))
    update = initialize_array(
        DatasetType.update, ComponentType.sym_load, (hours, spec.n_connections)
    )
    update["id"] = load["id"]
    update["p_specified"] = p_w
    update["q_specified"] = np.abs(p_w) * tan_phi

    output = model.calculate_power_flow(
        update_data={ComponentType.sym_load: update},
        calculation_method=CalculationMethod.newton_raphson,
    )
    u_pu = output[ComponentType.node]["u_pu"]
    loading = output[ComponentType.line]["loading"]

    violations: list[GridViolation] = []
    for hour in range(hours):
        for idx in range(n_nodes):
            value = float(u_pu[hour, idx])
            if value < spec.u_min_pu:
                violations.append(
                    GridViolation("undervoltage", hour, idx, value, spec.u_min_pu, "pu")
                )
            elif value > spec.u_max_pu:
                violations.append(
                    GridViolation("overvoltage", hour, idx, value, spec.u_max_pu, "pu")
                )
        for idx in range(n_nodes - 1):
            value = float(loading[hour, idx])
            if value > spec.loading_max:
                violations.append(
                    GridViolation(
                        "line_overload", hour, idx, value, spec.loading_max, "p.u. of rating"
                    )
                )

    return GridResult(
        violations=tuple(violations),
        min_u_pu=float(u_pu.min()),
        max_loading=float(loading.max()),
        feeder=spec,
        metadata={"hours": float(hours), "connections": float(spec.n_connections)},
    )
