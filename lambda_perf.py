"""
lambda_perf.py

Common AWS Lambda performance instrumentation.

Captures:
- invocation duration
- cold/warm start
- Lambda memory configuration
- architecture
- CPU model
- vCPU count
- Graviton generation when detectable
- ARM CPU part / MIDR
- x86 vendor/model
- initialization type (on-demand, provisioned concurrency, SnapStart)
- invocation metadata
- success/error status
- custom per-invocation attributes added by the handler

The record can optionally be added to the handler's response, e.g. for
Lambdas invoked from Step Functions.

No external dependencies.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import platform
import time
from typing import Any, Callable, Mapping, TypeVar, overload

F = TypeVar("F", bound=Callable[..., Any])

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CPU identification
# ---------------------------------------------------------------------------

# ARM CPU part -> (AWS generation, ARM core, nominal MHz)
#
# Unknown parts intentionally remain "unknown" rather than guessing.
_GRAVITON_PARTS = {
    0xD0C: ("graviton2", "neoverse-n1", 2500),
    0xD40: ("graviton3", "neoverse-v1", 2600),
    0xD4F: ("graviton4", "neoverse-v2", 2800),
}


def _read_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _parse_cpuinfo() -> dict[str, str]:
    text = _read_file("/proc/cpuinfo")

    if not text:
        return {}

    # The first processor block is enough for Lambda.
    first_cpu = text.split("\n\n", 1)[0]

    result: dict[str, str] = {}

    for line in first_cpu.splitlines():
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        result[key.strip().lower()] = value.strip()

    return result


def _read_arm_midr() -> tuple[int | None, int | None, str | None]:
    """
    Read ARM MIDR_EL1 when exposed by Linux.

    Returns:
        implementer
        cpu_part
        raw_midr
    """

    paths = (
        "/sys/devices/system/cpu/cpu0/regs/identification/midr_el1",
        "/sys/devices/system/cpu/cpu0/regs/identification/midr",
    )

    for path in paths:
        raw = _read_file(path)

        if not raw:
            continue

        try:
            midr = int(raw, 0)

            implementer = (midr >> 24) & 0xFF
            cpu_part = (midr >> 4) & 0xFFF

            return implementer, cpu_part, raw

        except ValueError:
            continue

    return None, None, None


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None

    if isinstance(value, int):
        return value

    try:
        return int(str(value).strip(), 0)
    except ValueError:
        return None


def get_cpu_info() -> dict[str, Any]:
    """
    Detect CPU characteristics once per Lambda execution environment.
    """

    arch = platform.machine().lower()
    cpuinfo = _parse_cpuinfo()

    result: dict[str, Any] = {
        "architecture": arch,
        "cpu_family": "unknown",
        "cpu_model": None,
        "cpu_vendor": None,
        "cpu_count": os.cpu_count(),
        "graviton_generation": None,
        "nominal_mhz": None,
        "observed_mhz": None,
        "arm_implementer": None,
        "arm_cpu_part": None,
        "arm_midr": None,
    }

    # ------------------------------------------------------------------
    # ARM64
    # ------------------------------------------------------------------

    if arch in ("aarch64", "arm64"):

        implementer, cpu_part, raw_midr = _read_arm_midr()

        # Fall back to /proc/cpuinfo
        if implementer is None:
            implementer = _parse_int(cpuinfo.get("cpu implementer"))

        if cpu_part is None:
            cpu_part = _parse_int(cpuinfo.get("cpu part"))

        result["arm_implementer"] = (
            f"0x{implementer:02x}"
            if implementer is not None
            else None
        )

        result["arm_cpu_part"] = (
            f"0x{cpu_part:03x}"
            if cpu_part is not None
            else None
        )

        result["arm_midr"] = raw_midr

        # ARM Ltd implementer
        if implementer == 0x41:
            result["cpu_vendor"] = "arm"

        if implementer == 0x41 and cpu_part in _GRAVITON_PARTS:
            generation, core, nominal_mhz = _GRAVITON_PARTS[cpu_part]

            result["cpu_family"] = generation
            result["cpu_model"] = core
            result["graviton_generation"] = generation
            result["nominal_mhz"] = nominal_mhz

        else:
            result["cpu_family"] = "arm64"
            result["cpu_model"] = cpuinfo.get("model name")

        return result

    # ------------------------------------------------------------------
    # x86_64
    # ------------------------------------------------------------------

    if arch in ("x86_64", "amd64"):

        vendor = cpuinfo.get("vendor_id")
        model = cpuinfo.get("model name")

        result["cpu_vendor"] = vendor
        result["cpu_model"] = model

        if vendor == "GenuineIntel":
            result["cpu_family"] = "intel"

        elif vendor == "AuthenticAMD":
            result["cpu_family"] = "amd"

        else:
            result["cpu_family"] = "x86_64"

        try:
            result["observed_mhz"] = float(cpuinfo["cpu mhz"])
        except (KeyError, ValueError):
            pass

        return result

    return result


def _safe_cpu_info() -> dict[str, Any]:
    # Instrumentation must never prevent the function from initializing.
    try:
        return get_cpu_info()
    except Exception:
        return {"architecture": platform.machine().lower(), "cpu_family": "unknown"}


# Calculated once at cold start.
CPU_INFO = _safe_cpu_info()

_COLD_START = True

INCLUDE_IN_RESPONSE_ENV = "LAMBDA_PERF_INCLUDE_IN_RESPONSE"
DEFAULT_RESPONSE_KEY = "lambda_perf"


def env_flag(name: str, default: bool = False) -> bool:
    """
    Read a boolean environment variable ("1", "true", "yes", "on").
    """

    value = os.environ.get(name)

    if value is None:
        return default

    return value.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Custom attributes
# ---------------------------------------------------------------------------

# A plain global rather than a ContextVar so attributes added from worker
# threads are captured. A Python Lambda process handles one invocation at a time.
_current_attributes: dict[str, Any] | None = None


class PerfLog:
    """
    Adds custom attributes to the performance record of the current invocation.

    Attributes are emitted under the record's "attributes" key and reset at
    the start of every invocation. Calls made outside a wrapped handler are
    ignored.
    """

    def add_attribute(self, name: str, value: Any) -> None:
        if _current_attributes is not None:
            _current_attributes[name] = value

    def add_attributes(
        self,
        attributes: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        for name, value in {**(attributes or {}), **kwargs}.items():
            self.add_attribute(name, value)

    @property
    def attributes(self) -> dict[str, Any]:
        return dict(_current_attributes or {})


perf_log = PerfLog()


# ---------------------------------------------------------------------------
# Lambda instrumentation
# ---------------------------------------------------------------------------

def _lambda_environment(context: Any) -> dict[str, Any]:
    return {
        "function_name": getattr(context, "function_name", None),
        "function_version": getattr(context, "function_version", None),
        # The Lambda context exposes this as a string.
        "memory_mb": _parse_int(getattr(context, "memory_limit_in_mb", None)),
        "request_id": getattr(context, "aws_request_id", None),
        "runtime": os.environ.get("AWS_EXECUTION_ENV"),
        "region": os.environ.get("AWS_REGION"),
        # on-demand | provisioned-concurrency | snap-start
        "initialization_type": os.environ.get("AWS_LAMBDA_INITIALIZATION_TYPE"),
    }


def _emit(record: dict[str, Any]) -> None:
    """
    Output one-line JSON so CloudWatch Logs Insights, Firehose,
    Athena, etc. can parse it easily.
    """

    print(
        json.dumps(
            record,
            separators=(",", ":"),
            default=str,
        ),
        flush=True,
    )


def _record_invocation(
    metric_name: str,
    start_ns: int,
    cold_start: bool,
    context: Any,
    attributes: dict[str, Any],
    status: str,
    error_type: str | None,
) -> dict[str, Any] | None:
    # A failure here must not replace the handler's return value
    # or mask its exception.
    duration_ms = (time.perf_counter_ns() - start_ns) / 1_000_000

    try:
        record = {
            "record_type": "lambda_performance",
            "metric": metric_name,
            "duration_ms": round(duration_ms, 3),
            "status": status,
            "error_type": error_type,
            "cold_start": cold_start,

            **_lambda_environment(context),

            # CPU dimensions
            **CPU_INFO,

            "attributes": attributes,
        }
    except Exception:
        _logger.warning("lambda_perf: failed to build record", exc_info=True)
        return None

    try:
        _emit(record)
    except Exception:
        _logger.warning("lambda_perf: failed to emit record", exc_info=True)

    return record


def _add_to_response(result: Any, record: dict[str, Any], key: str) -> Any:
    try:
        # Round-trip so custom attribute values the Lambda runtime
        # can't serialize (Decimal, datetime, ...) don't fail the response.
        payload = json.loads(json.dumps(record, default=str))
    except Exception:
        _logger.warning("lambda_perf: failed to serialize record", exc_info=True)
        return result

    if isinstance(result, dict):
        return {**result, key: payload}

    return {"result": result, key: payload}


@overload
def wrap_handler(metric_name: F) -> F: ...


@overload
def wrap_handler(
    metric_name: str = ...,
    *,
    include_in_response: bool | None = ...,
    response_key: str = ...,
) -> Callable[[F], F]: ...


def wrap_handler(
    metric_name: Any = "lambda_invocation",
    *,
    include_in_response: bool | None = None,
    response_key: str = DEFAULT_RESPONSE_KEY,
) -> Any:
    """
    Decorator for AWS Lambda handlers.

    Can be used with or without a metric name:

        @wrap_handler("pricing_calculation")
        def lambda_handler(event, context):
            ...

        @wrap_handler
        def lambda_handler(event, context):
            ...

    include_in_response:
        Add the performance record to successful responses under
        `response_key`. Dict responses get the key added; any other
        response is wrapped as {"result": <response>, response_key: ...}.
        When None, the LAMBDA_PERF_INCLUDE_IN_RESPONSE environment
        variable is checked on each invocation.
    """

    if callable(metric_name):
        return wrap_handler()(metric_name)

    def decorator(handler: F) -> F:

        @functools.wraps(handler)
        def wrapped(event: Any, context: Any) -> Any:

            global _COLD_START, _current_attributes

            cold_start = _COLD_START
            _COLD_START = False

            attributes: dict[str, Any] = {}
            previous_attributes = _current_attributes
            _current_attributes = attributes

            start_ns = time.perf_counter_ns()

            try:
                result = handler(event, context)

            except BaseException as exc:
                _record_invocation(
                    metric_name, start_ns, cold_start, context,
                    attributes, "error", type(exc).__name__,
                )

                # Preserve normal Lambda exception behavior.
                raise

            finally:
                _current_attributes = previous_attributes

            record = _record_invocation(
                metric_name, start_ns, cold_start, context,
                attributes, "success", None,
            )

            include = (
                env_flag(INCLUDE_IN_RESPONSE_ENV)
                if include_in_response is None
                else include_in_response
            )

            if include and record is not None:
                return _add_to_response(result, record, response_key)

            return result

        return wrapped  # type: ignore[return-value]

    return decorator