"""Isolated planner calls with a killable whole-call timeout."""

import json
import math
import multiprocessing
import threading
import time
import traceback

from ..models import OptimizationParams
from ..planners.registry import create_planner

PLANNER_TIMEOUT_SECONDS = 120.0


class PlannerTimeout(TimeoutError):
    """A killed planner invocation, including its observed timeout duration."""

    def __init__(self, timeout: float, elapsed: float):
        super().__init__(f"Planner exceeded {timeout:g}s whole-call limit")
        self.timeout_seconds = timeout
        self.elapsed_seconds = elapsed


def _serve(connection, factory_path: str, options: dict) -> None:
    try:
        planner = None
        while True:
            message = connection.recv()
            if message is None:
                break
            payload, reset = message
            if reset or planner is None:
                planner = create_planner(factory_path, options)
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
        self._closed = False
        self._exchange = None

    def call(self, payload: dict, *, reset: bool, timeout: float) -> dict:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Planner timeout must be finite and positive")
        timeout = min(timeout, PLANNER_TIMEOUT_SECONDS)
        if self._closed:
            raise RuntimeError("Planner worker is closed")
        started = time.perf_counter()
        finished = threading.Event()
        outcome = []

        def exchange():
            try:
                self._connection.send((payload, reset))
                outcome.append(self._connection.recv())
            except BaseException as err:
                # Do not retain pipe frames/buffers after killing a blocked send.
                outcome.append(err.with_traceback(None))
            finally:
                outcome.append(time.perf_counter())
                finished.set()

        # Pipe send can block before a starting worker reads, and poll only
        # promises some bytes, not a complete response. Bound the whole exchange.
        self._exchange = threading.Thread(target=exchange, daemon=True)
        self._exchange.start()
        deadline = started + timeout
        while not finished.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            # Recheck the absolute deadline if a timed wait returns early.
            finished.wait(remaining)
        if not finished.is_set() or outcome[-1] > deadline:
            elapsed = time.perf_counter() - started
            self.close(kill=True)
            raise PlannerTimeout(timeout, elapsed)
        self._exchange.join()
        response = outcome[0]
        if isinstance(response, BaseException):
            self.close(kill=True)
            if isinstance(response, (EOFError, OSError)):
                raise RuntimeError(
                    "Planner worker exited without a result"
                ) from response
            raise response
        if "error" in response:
            self.close()
            raise RuntimeError(response["error"])
        return response

    def close(self, *, kill: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.is_alive():
            if kill:
                self._process.kill()
            else:
                self._process.terminate()
        self._process.join(timeout=5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=5)
        if self._exchange is not None:
            self._exchange.join(timeout=5)
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
