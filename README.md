# AWS Lambda CPU & Performance Instrumentation

A lightweight, dependency-free Python utility for instrumenting AWS Lambda functions with CPU and invocation performance metadata.

The primary goal is to make Lambda performance analysis more useful by recording the underlying processor type alongside execution duration.

This is particularly useful when comparing:

- AWS Graviton generations
- ARM64 vs x86_64
- Intel vs AMD Lambda hosts
- Lambda memory allocations
- Cold vs warm invocations
- Runtime versions
- Function versions

The instrumentation is implemented as a Python decorator and requires minimal changes to an existing Lambda handler.

---

## Files

```text
.
├── lambda_function.py
├── lambda_perf.py
├── tests/
│   └── test_lambda_perf.py
└── README.md
```

### `lambda_perf.py`

Reusable instrumentation module that:

- Detects CPU architecture
- Detects known AWS Graviton CPU generations
- Reads ARM MIDR / CPU part information where available
- Detects Intel or AMD processors on x86_64
- Records Lambda memory allocation
- Measures handler execution duration
- Records the vCPU count available to the function
- Detects cold starts and the initialization type (on-demand, provisioned concurrency, SnapStart)
- Records runtime, region, function version, and request ID
- Lets the handler add custom per-invocation attributes (input size, transaction type, ...)
- Preserves normal handler return values, or optionally adds the performance record to the response (e.g. for Step Functions)
- Preserves normal exception behavior
- Never lets an instrumentation failure break the handler
- Emits structured JSON to stdout / CloudWatch Logs

CPU detection occurs once when the Lambda execution environment starts rather than on every invocation.

### `lambda_function.py`

Your normal Lambda handler with the performance decorator applied.

---

# Basic Usage

Import the decorator:

```python
from lambda_perf import wrap_handler
```

Apply it to the Lambda handler:

```python
from lambda_perf import wrap_handler


@wrap_handler(metric_name="my_lambda")
def lambda_handler(event, context):

    result = do_work(event)

    return result
```

The decorator wraps the existing Lambda invocation without changing the handler's return value (unless [including metrics in the response](#including-metrics-in-the-response-step-functions) is enabled).

If the handler raises an exception, the exception is re-raised normally after the performance information is recorded.

---

# Minimal Example

Before:

```python
def lambda_handler(event, context):
    return do_work(event)
```

After:

```python
from lambda_perf import wrap_handler


@wrap_handler("my_function")
def lambda_handler(event, context):
    return do_work(event)
```

No other application changes are required.

The metric name is optional. Used without arguments, records are tagged with `"metric": "lambda_invocation"`:

```python
@wrap_handler
def lambda_handler(event, context):
    return do_work(event)
```

---

# Custom Attributes

The handler can attach its own attributes to the current invocation's record with `perf_log`. This is useful for recording input complexity so duration can be analyzed relative to workload size:

```python
import json

from lambda_perf import env_flag, perf_log, wrap_handler

MEASURE_INPUT = env_flag("PERF_MEASURE_INPUT")


@wrap_handler("my_lambda")
def lambda_handler(event, context):

    if MEASURE_INPUT:
        perf_log.add_attribute("input_size_bytes", len(json.dumps(event)))
        perf_log.add_attribute("transaction_requested", event.get("transaction"))

    return do_work(event)
```

Several attributes can be added at once:

```python
perf_log.add_attributes({"rows": 1200}, transaction_requested="quote")
```

Attributes appear under the record's `attributes` key:

```json
{
  "record_type": "lambda_performance",
  "duration_ms": 183.427,
  "attributes": {
    "input_size_bytes": 10240,
    "transaction_requested": "quote"
  }
}
```

Attributes are reset at the start of every invocation, are recorded for failed invocations too, and can be added from any thread during the invocation. Calls made outside a wrapped handler (e.g. at import time) are ignored. Prefer `snake_case` names so they are easy to query; CloudWatch Logs Insights exposes them as `attributes.input_size_bytes`.

`env_flag(name)` treats `1`, `true`, `yes`, and `on` (case-insensitive) as enabled.

---

# Including Metrics in the Response (Step Functions)

When a Lambda is invoked from a Step Function, it is often easier to carry the performance record in the task output than to collect it from CloudWatch Logs.

Enable it per function with an environment variable:

```text
LAMBDA_PERF_INCLUDE_IN_RESPONSE=true
```

or explicitly in code:

```python
@wrap_handler("my_lambda", include_in_response=True)
def lambda_handler(event, context):
    ...
```

The decorator adds the record to the response, because the duration is only known after the handler returns. A dict response gets an extra key:

```json
{
  "statusCode": 200,
  "body": "OK",
  "lambda_perf": {
    "record_type": "lambda_performance",
    "metric": "my_lambda",
    "duration_ms": 183.427,
    "attributes": { "transaction_requested": "quote" }
  }
}
```

Any other response (list, string, number, `None`) is wrapped:

```json
{
  "result": [1, 2, 3],
  "lambda_perf": { }
}
```

Notes:

- Pass `include_in_response=False` to turn it off regardless of the environment variable.
- Use `response_key="..."` to change the key name from `lambda_perf`.
- The record is still written to CloudWatch Logs as well.
- Failed invocations have no response, so their records only appear in the logs.
- Attribute values that aren't JSON-serializable (`Decimal`, `datetime`, ...) are converted to strings in the response.
- With the optimized `arn:aws:states:::lambda:invoke` integration, the record is at `$.Payload.lambda_perf` in the task result. Step Functions limits state payloads to 256 KB; the record adds roughly 1 KB plus your attributes.

---

# Example Performance Record

Each invocation emits a single-line JSON record similar to:

```json
{
  "record_type": "lambda_performance",
  "metric": "my_function",
  "duration_ms": 183.427,
  "status": "success",
  "error_type": null,
  "cold_start": false,
  "function_name": "pricing-engine",
  "function_version": "$LATEST",
  "memory_mb": 4096,
  "request_id": "39d41875-...",
  "runtime": "AWS_Lambda_python3.12",
  "region": "us-east-1",
  "initialization_type": "on-demand",
  "architecture": "aarch64",
  "cpu_family": "graviton2",
  "cpu_model": "neoverse-n1",
  "cpu_vendor": "arm",
  "cpu_count": 3,
  "graviton_generation": "graviton2",
  "nominal_mhz": 2500,
  "observed_mhz": null,
  "arm_implementer": "0x41",
  "arm_cpu_part": "0xd0c",
  "arm_midr": "0x...",
  "attributes": {}
}
```

Because the output is JSON, it can be queried directly with CloudWatch Logs Insights or forwarded to another analytics platform.

---

# Captured Fields

## Invocation Metrics

| Field | Description |
|---|---|
| `record_type` | Identifies the record as Lambda performance telemetry |
| `metric` | Logical workload or function metric name |
| `duration_ms` | Handler execution time in milliseconds |
| `status` | `success` or `error` |
| `error_type` | Python exception type when an invocation fails |
| `cold_start` | Whether this was the first invocation in the Lambda execution environment |
| `attributes` | Custom attributes added by the handler via `perf_log` |

---

## Lambda Environment

| Field | Description |
|---|---|
| `function_name` | Lambda function name |
| `function_version` | Lambda function version |
| `memory_mb` | Configured Lambda memory (integer) |
| `request_id` | AWS Lambda request ID |
| `runtime` | Lambda runtime environment |
| `region` | AWS region |
| `initialization_type` | `on-demand`, `provisioned-concurrency`, or `snap-start` |

---

## CPU Information

| Field | Description |
|---|---|
| `architecture` | CPU architecture such as `aarch64` or `x86_64` |
| `cpu_family` | Normalized processor family |
| `cpu_model` | Detected CPU model |
| `cpu_vendor` | ARM, Intel, AMD, or other detected vendor |
| `cpu_count` | vCPUs visible to the function (Lambda scales this with memory) |
| `graviton_generation` | Known Graviton generation if detected |
| `nominal_mhz` | Known nominal frequency for recognized Graviton processors |
| `observed_mhz` | Frequency reported by Linux where available |
| `arm_implementer` | ARM CPU implementer ID |
| `arm_cpu_part` | ARM CPU part ID |
| `arm_midr` | Raw ARM Main ID Register value where available |

---

# Graviton Detection

The utility identifies known Graviton processors using the ARM CPU implementation and CPU part exposed by Linux.

Known mappings currently include:

| CPU Family | ARM Core | CPU Part | Nominal Frequency |
|---|---|---:|---:|
| Graviton2 | Neoverse N1 | `0xd0c` | 2500 MHz |
| Graviton3 | Neoverse V1 | `0xd40` | 2600 MHz |
| Graviton4 | Neoverse V2 | `0xd4f` | 2800 MHz |

Unknown ARM CPU parts are intentionally reported as generic `arm64`.

The instrumentation does not guess a Graviton generation when the CPU cannot be positively identified.

This makes the resulting telemetry safer to use for long-running performance studies if AWS changes the underlying Lambda hardware.

---

# x86_64 Support

The same utility supports Lambda functions running on x86_64.

Processors are categorized using Linux CPU vendor information.

Typical values include:

```text
cpu_family = intel
```

or:

```text
cpu_family = amd
```

The full processor model is also captured when available.

For example:

```json
{
  "architecture": "x86_64",
  "cpu_family": "intel",
  "cpu_model": "Intel(R) Xeon(R) Processor ...",
  "cpu_vendor": "GenuineIntel"
}
```

This allows the same application telemetry to continue working if the Lambda architecture changes from ARM64 back to x86_64.

---

# Performance Analysis

For Lambda performance analysis, the most useful dimensions are generally:

```text
function_name
function_version
memory_mb
architecture
cpu_family
cold_start
runtime
```

The primary measured value is:

```text
duration_ms
```

For example:

```text
memory_mb × cpu_family → p50 duration_ms
memory_mb × cpu_family → p95 duration_ms
memory_mb × architecture → throughput
cold_start × cpu_family → startup duration
```

A useful dataset might look like:

| Memory | CPU | Architecture | Cold Start | Duration |
|---:|---|---|---|---:|
| 2048 MB | Graviton2 | ARM64 | No | 411 ms |
| 2048 MB | Graviton2 | ARM64 | No | 398 ms |
| 2048 MB | Intel | x86_64 | No | 435 ms |
| 4096 MB | Graviton2 | ARM64 | No | 224 ms |

This makes it possible to compare execution environments without relying only on Lambda architecture.

---

# Why Not Normalize Performance by GHz?

Clock frequency should be treated as metadata rather than the primary performance comparison.

For example:

```text
Graviton2 @ 2.5 GHz
```

and:

```text
Graviton3 @ 2.6 GHz
```

do not necessarily perform similarly simply because their clock frequencies are close.

Different CPU architectures can perform substantially different amounts of work per clock cycle.

For performance analysis, prefer comparing actual measurements such as:

```text
duration_ms
records_per_second
bytes_per_second
cpu_time_ms
```

while using CPU generation and architecture as dimensions.

---

# Cold Start Detection

The module maintains a process-level flag:

```python
_COLD_START = True
```

The first invocation inside a Lambda execution environment is recorded as:

```json
{
  "cold_start": true
}
```

Subsequent invocations using the same execution environment are recorded as:

```json
{
  "cold_start": false
}
```

This allows cold starts to be excluded from steady-state performance analysis or analyzed independently.

`cold_start` only means "first invocation in this execution environment". With provisioned concurrency or SnapStart, initialization happens before the request arrives, so that first invocation is usually not slow. Use `initialization_type` to tell these apart:

```text
filter record_type = "lambda_performance" and initialization_type = "on-demand"
| stats pct(duration_ms, 50) as p50_ms by cold_start
```

---

# CloudWatch Logs Insights

Because each performance record is emitted as JSON, CloudWatch Logs Insights can automatically discover most fields.

For example:

```text
fields @timestamp,
       duration_ms,
       memory_mb,
       cpu_family,
       architecture,
       cold_start
| filter record_type = "lambda_performance"
| stats
    count(*) as invocations,
    avg(duration_ms) as avg_ms,
    pct(duration_ms, 50) as p50_ms,
    pct(duration_ms, 95) as p95_ms,
    pct(duration_ms, 99) as p99_ms
  by memory_mb, cpu_family, architecture
| sort p50_ms asc
```

To compare cold and warm invocations:

```text
fields duration_ms, cpu_family, cold_start
| filter record_type = "lambda_performance"
| stats
    count(*) as invocations,
    avg(duration_ms) as avg_ms,
    pct(duration_ms, 50) as p50_ms,
    pct(duration_ms, 95) as p95_ms
  by cpu_family, cold_start
```

---

# Error Handling

The decorator records failed invocations without interfering with normal Lambda exception handling.

For example:

```python
@wrap_handler("pricing")
def lambda_handler(event, context):
    raise ValueError("Invalid request")
```

The performance record will contain:

```json
{
  "status": "error",
  "error_type": "ValueError"
}
```

The original `ValueError` is then re-raised normally.

This means Lambda retries, destinations, DLQs, and other AWS failure behavior continue to work as expected.

The instrumentation itself is also fail-safe: if CPU detection fails at import time, the CPU fields fall back to `unknown`; if emitting a record fails, a warning is logged and the handler's return value or exception is passed through unchanged.

---

# Runtime Overhead

CPU detection occurs when the module is imported during execution environment initialization:

```python
CPU_INFO = _safe_cpu_info()
```

It is not repeated for every invocation.

Invocation timing uses:

```python
time.perf_counter_ns()
```

The per-invocation instrumentation is therefore limited primarily to:

- Two high-resolution timer reads
- Construction of a small Python dictionary
- JSON serialization
- One stdout log entry

For most Lambda workloads this overhead should be small relative to the application workload.

---

# Deployment

No third-party Python packages are required.

Deploy both files with the Lambda:

```text
lambda_function.py
lambda_perf.py
```

If using a ZIP deployment:

```bash
zip lambda.zip lambda_function.py lambda_perf.py
```

The same module can also be included in:

- Lambda container images
- Lambda Layers
- Shared internal Python libraries
- Multiple Lambda deployment packages

For a large Lambda estate, packaging `lambda_perf.py` as a shared internal library or Lambda Layer may be preferable.

---

# Running Tests

The tests use only the standard library:

```bash
python -m unittest discover -s tests -v
```

---

# Recommended Analysis Dimensions

For controlled Lambda benchmarking, keep the following constant where possible:

```text
function code
function version
runtime
memory_mb
input workload
AWS region
```

Then compare:

```text
architecture
cpu_family
graviton_generation
```

A recommended performance record therefore contains at least:

```json
{
  "duration_ms": 183.427,
  "memory_mb": 4096,
  "architecture": "aarch64",
  "cpu_family": "graviton2",
  "graviton_generation": "graviton2",
  "cold_start": false,
  "function_version": "42"
}
```

This provides enough information to perform meaningful percentile and throughput comparisons across Lambda execution environments.

---

# Future Improvements

Potential extensions include:

- CloudWatch Embedded Metric Format (EMF)
- Native CloudWatch custom metrics
- OpenTelemetry spans and attributes
- CPU-time measurement
- Process memory metrics
- Peak RSS
- X-Ray trace IDs
- AWS Powertools integration
- Sampling support for very high-volume Lambdas
- Firehose delivery to S3
- Athena / Parquet performance-analysis datasets

For large-scale analysis, a natural next step is to emit the same telemetry using **CloudWatch EMF**, allowing `duration_ms` to become a native CloudWatch metric while CPU family, Lambda memory, architecture, and cold-start state are retained as dimensions.