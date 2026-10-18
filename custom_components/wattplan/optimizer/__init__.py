from .models import OptimizationParams, has_mobility_inputs


def optimize(params: OptimizationParams):
    """The unchanged production HiGHS entry point, loaded only when called."""
    if has_mobility_inputs(params):
        raise ValueError("HiGHS does not support battery mobility inputs")
    from .mpc_power_optimizer import optimize as optimize_highs

    return optimize_highs(params)

__all__ = ["OptimizationParams", "optimize"]
