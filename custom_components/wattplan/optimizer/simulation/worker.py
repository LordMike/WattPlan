"""Isolated planner calls with a killable whole-call timeout."""

import importlib
import json
import multiprocessing
import time
import traceback

from ..models import OptimizationParams


def _serve(connection, factory_path: str, options: dict) -> None:
    try:
        module, name = factory_path.split(":", 1)
        factory = getattr(importlib.import_module(module), name)
        planner = None
        while True:
            message = connection.recv()
            if message is None:
                break
            payload, reset = message
            if reset or planner is None:
                planner = factory(**options)
            params = OptimizationParams(**payload)
            wall, cpu = time.perf_counter(), time.process_time()
            result = planner(params)
            elapsed, used = time.perf_counter() - wall, time.process_time() - cpu
            # Reject nonfinite/unserializable output before it crosses the boundary.
            result = json.loads(json.dumps(result, allow_nan=False))
            connection.send(
                {"result": result, "wall_seconds": elapsed, "cpu_seconds": used}
            )
    except EOFError:
        pass
    except BaseException:
        connection.send({"error": traceback.format_exc()})
    finally:
        connection.close()


class PlannerWorker:
    """One process per variant/repeat, fresh adapter state per coverage segment."""

    def __init__(self, factory: str, options: dict | None = None):
        context = multiprocessing.get_context("spawn")
        self._connection, child = context.Pipe()
        self._process = context.Process(
            target=_serve, args=(child, factory, options or {})
        )
        self._process.start()
        child.close()

    def call(self, payload: dict, *, reset: bool, timeout: float) -> dict:
        self._connection.send((payload, reset))
        if not self._connection.poll(timeout):
            self.close()
            raise TimeoutError(f"Planner exceeded {timeout:g}s whole-call limit")
        try:
            response = self._connection.recv()
        except (EOFError, OSError) as err:
            raise RuntimeError("Planner worker exited without a result") from err
        if "error" in response:
            raise RuntimeError(response["error"])
        return response

    def close(self) -> None:
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=5)
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
