from .models import OptimizationParams


def optimize(params: OptimizationParams):
    """The unchanged production HiGHS entry point, loaded only when called."""
    from .mpc_power_optimizer import optimize as optimize_highs

    return optimize_highs(params)

__all__ = ["OptimizationParams", "optimize"]
