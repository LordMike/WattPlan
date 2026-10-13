"""One JSON request/response process for live planning, independent of HA APIs."""

import json
import sys

from .models import OptimizationParams
from .planners.presets import run_preset


def main() -> None:
    """Validate inputs and serialize the complete plan before returning."""
    request = json.load(sys.stdin)
    params = OptimizationParams(**request["params"])
    result = run_preset(params, request["preset"])
    sys.stdout.write(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
