import contextlib
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lambda_perf  # noqa: E402


def _context(**overrides):
    values = {
        "function_name": "test-fn",
        "function_version": "$LATEST",
        "memory_limit_in_mb": "1024",
        "aws_request_id": "req-123",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fake_files(files):
    return lambda path: files.get(path)


ARM_CPUINFO = """processor\t: 0
BogoMIPS\t: 243.75
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x3
CPU part\t: 0xd0c
CPU revision\t: 1

processor\t: 1
CPU part\t: 0xfff
"""

X86_CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
model name\t: Intel(R) Xeon(R) Processor @ 2.90GHz
cpu MHz\t\t: 2899.998

processor\t: 1
vendor_id\t: AuthenticAMD
"""

MIDR_PATH = "/sys/devices/system/cpu/cpu0/regs/identification/midr_el1"


class GetCpuInfoTests(unittest.TestCase):

    def test_graviton_from_midr(self):
        files = {MIDR_PATH: "0x00000000413fd401", "/proc/cpuinfo": ""}
        with mock.patch.object(lambda_perf, "_read_file", _fake_files(files)), \
                mock.patch("platform.machine", return_value="aarch64"):
            info = lambda_perf.get_cpu_info()

        self.assertEqual(info["cpu_family"], "graviton3")
        self.assertEqual(info["cpu_model"], "neoverse-v1")
        self.assertEqual(info["cpu_vendor"], "arm")
        self.assertEqual(info["arm_implementer"], "0x41")
        self.assertEqual(info["arm_cpu_part"], "0xd40")
        self.assertEqual(info["arm_midr"], "0x00000000413fd401")
        self.assertEqual(info["nominal_mhz"], 2600)

    def test_graviton_from_cpuinfo_fallback(self):
        files = {"/proc/cpuinfo": ARM_CPUINFO}
        with mock.patch.object(lambda_perf, "_read_file", _fake_files(files)), \
                mock.patch("platform.machine", return_value="aarch64"):
            info = lambda_perf.get_cpu_info()

        self.assertEqual(info["graviton_generation"], "graviton2")
        self.assertEqual(info["arm_cpu_part"], "0xd0c")
        self.assertIsNone(info["arm_midr"])

    def test_unknown_arm_part_is_not_guessed(self):
        files = {"/proc/cpuinfo": "CPU implementer\t: 0x41\nCPU part\t: 0xd99\n"}
        with mock.patch.object(lambda_perf, "_read_file", _fake_files(files)), \
                mock.patch("platform.machine", return_value="aarch64"):
            info = lambda_perf.get_cpu_info()

        self.assertEqual(info["cpu_family"], "arm64")
        self.assertIsNone(info["graviton_generation"])
        self.assertIsNone(info["nominal_mhz"])

    def test_intel(self):
        files = {"/proc/cpuinfo": X86_CPUINFO}
        with mock.patch.object(lambda_perf, "_read_file", _fake_files(files)), \
                mock.patch("platform.machine", return_value="x86_64"):
            info = lambda_perf.get_cpu_info()

        self.assertEqual(info["cpu_family"], "intel")
        self.assertEqual(info["cpu_vendor"], "GenuineIntel")
        self.assertEqual(info["cpu_model"], "Intel(R) Xeon(R) Processor @ 2.90GHz")
        self.assertAlmostEqual(info["observed_mhz"], 2899.998)

    def test_amd(self):
        files = {"/proc/cpuinfo": "vendor_id\t: AuthenticAMD\n"}
        with mock.patch.object(lambda_perf, "_read_file", _fake_files(files)), \
                mock.patch("platform.machine", return_value="x86_64"):
            info = lambda_perf.get_cpu_info()

        self.assertEqual(info["cpu_family"], "amd")
        self.assertIsNone(info["observed_mhz"])

    def test_missing_cpuinfo(self):
        with mock.patch.object(lambda_perf, "_read_file", _fake_files({})), \
                mock.patch("platform.machine", return_value="x86_64"):
            info = lambda_perf.get_cpu_info()

        self.assertEqual(info["cpu_family"], "x86_64")
        self.assertIsNone(info["cpu_model"])


class WrapHandlerTests(unittest.TestCase):

    def setUp(self):
        lambda_perf._COLD_START = True

    def _invoke(self, handler, context=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            try:
                result = handler({}, context or _context())
                error = None
            except BaseException as exc:
                result = None
                error = exc
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        return result, error, records

    def test_success_record(self):
        @lambda_perf.wrap_handler("my_metric")
        def handler(event, context):
            return {"statusCode": 200}

        with mock.patch.dict(os.environ, {
            "AWS_REGION": "us-east-1",
            "AWS_EXECUTION_ENV": "AWS_Lambda_python3.12",
            "AWS_LAMBDA_INITIALIZATION_TYPE": "on-demand",
        }):
            result, error, records = self._invoke(handler)

        self.assertEqual(result, {"statusCode": 200})
        self.assertIsNone(error)
        self.assertEqual(len(records), 1)

        record = records[0]
        self.assertEqual(record["record_type"], "lambda_performance")
        self.assertEqual(record["metric"], "my_metric")
        self.assertEqual(record["status"], "success")
        self.assertIsNone(record["error_type"])
        self.assertIs(record["cold_start"], True)
        self.assertEqual(record["memory_mb"], 1024)
        self.assertEqual(record["function_name"], "test-fn")
        self.assertEqual(record["request_id"], "req-123")
        self.assertEqual(record["region"], "us-east-1")
        self.assertEqual(record["runtime"], "AWS_Lambda_python3.12")
        self.assertEqual(record["initialization_type"], "on-demand")
        self.assertIsInstance(record["duration_ms"], float)
        self.assertGreaterEqual(record["duration_ms"], 0)
        self.assertIn("cpu_family", record)
        self.assertIn("cpu_count", record)

    def test_cold_start_only_first_invocation(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            return None

        _, _, first = self._invoke(handler)
        _, _, second = self._invoke(handler)

        self.assertIs(first[0]["cold_start"], True)
        self.assertIs(second[0]["cold_start"], False)

    def test_error_is_recorded_and_reraised(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            raise ValueError("bad input")

        _, error, records = self._invoke(handler)

        self.assertIsInstance(error, ValueError)
        self.assertEqual(records[0]["status"], "error")
        self.assertEqual(records[0]["error_type"], "ValueError")

    def test_bare_decorator(self):
        @lambda_perf.wrap_handler
        def handler(event, context):
            return "ok"

        result, _, records = self._invoke(handler)

        self.assertEqual(result, "ok")
        self.assertEqual(records[0]["metric"], "lambda_invocation")
        self.assertEqual(handler.__name__, "handler")

    def test_emit_failure_does_not_break_handler(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            return "ok"

        with mock.patch.object(lambda_perf, "_emit", side_effect=RuntimeError("boom")), \
                self.assertLogs("lambda_perf", level="WARNING"):
            result, error, _ = self._invoke(handler)

        self.assertEqual(result, "ok")
        self.assertIsNone(error)

    def test_emit_failure_does_not_mask_handler_error(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            raise KeyError("missing")

        with mock.patch.object(lambda_perf, "_emit", side_effect=RuntimeError("boom")), \
                self.assertLogs("lambda_perf", level="WARNING"):
            _, error, _ = self._invoke(handler)

        self.assertIsInstance(error, KeyError)

    def test_missing_context_attributes(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            return None

        _, _, records = self._invoke(handler, context=object())

        self.assertIsNone(records[0]["memory_mb"])
        self.assertIsNone(records[0]["function_name"])


class CustomAttributeTests(unittest.TestCase):

    def setUp(self):
        lambda_perf._COLD_START = True

    def _invoke(self, handler, event=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            try:
                result = handler(event or {}, _context())
                error = None
            except BaseException as exc:
                result = None
                error = exc
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        return result, error, records

    def test_attributes_are_recorded(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            lambda_perf.perf_log.add_attribute("input_size_bytes", 10240)
            lambda_perf.perf_log.add_attributes(
                {"transaction_requested": "quote"}, rows=42,
            )
            return "ok"

        _, _, records = self._invoke(handler)

        self.assertEqual(records[0]["attributes"], {
            "input_size_bytes": 10240,
            "transaction_requested": "quote",
            "rows": 42,
        })

    def test_attributes_reset_between_invocations(self):
        calls = []

        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            if not calls:
                lambda_perf.perf_log.add_attribute("first_only", True)
            calls.append(1)

        _, _, first = self._invoke(handler)
        _, _, second = self._invoke(handler)

        self.assertEqual(first[0]["attributes"], {"first_only": True})
        self.assertEqual(second[0]["attributes"], {})

    def test_attributes_recorded_on_error(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            lambda_perf.perf_log.add_attribute("step", "parse")
            raise ValueError("bad")

        _, error, records = self._invoke(handler)

        self.assertIsInstance(error, ValueError)
        self.assertEqual(records[0]["attributes"], {"step": "parse"})

    def test_add_attribute_outside_invocation_is_ignored(self):
        lambda_perf.perf_log.add_attribute("orphan", 1)

        self.assertEqual(lambda_perf.perf_log.attributes, {})

    def test_attributes_from_worker_thread(self):
        import threading

        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            t = threading.Thread(
                target=lambda_perf.perf_log.add_attribute, args=("worker", True),
            )
            t.start()
            t.join()

        _, _, records = self._invoke(handler)

        self.assertEqual(records[0]["attributes"], {"worker": True})


class IncludeInResponseTests(unittest.TestCase):

    def setUp(self):
        lambda_perf._COLD_START = True

    def _invoke(self, handler):
        with contextlib.redirect_stdout(io.StringIO()):
            return handler({}, _context())

    def test_disabled_by_default(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            return {"statusCode": 200}

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self._invoke(handler), {"statusCode": 200})

    def test_dict_response_gets_perf_key(self):
        @lambda_perf.wrap_handler("m", include_in_response=True)
        def handler(event, context):
            lambda_perf.perf_log.add_attribute("rows", 3)
            return {"statusCode": 200}

        result = self._invoke(handler)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["lambda_perf"]["metric"], "m")
        self.assertEqual(result["lambda_perf"]["status"], "success")
        self.assertEqual(result["lambda_perf"]["attributes"], {"rows": 3})
        self.assertIn("duration_ms", result["lambda_perf"])

    def test_non_dict_response_is_wrapped(self):
        @lambda_perf.wrap_handler("m", include_in_response=True)
        def handler(event, context):
            return [1, 2, 3]

        result = self._invoke(handler)

        self.assertEqual(result["result"], [1, 2, 3])
        self.assertIn("lambda_perf", result)

    def test_enabled_by_env_var(self):
        @lambda_perf.wrap_handler("m")
        def handler(event, context):
            return {}

        with mock.patch.dict(os.environ, {"LAMBDA_PERF_INCLUDE_IN_RESPONSE": "true"}):
            self.assertIn("lambda_perf", self._invoke(handler))

        with mock.patch.dict(os.environ, {"LAMBDA_PERF_INCLUDE_IN_RESPONSE": "false"}):
            self.assertNotIn("lambda_perf", self._invoke(handler))

    def test_explicit_false_overrides_env_var(self):
        @lambda_perf.wrap_handler("m", include_in_response=False)
        def handler(event, context):
            return {}

        with mock.patch.dict(os.environ, {"LAMBDA_PERF_INCLUDE_IN_RESPONSE": "true"}):
            self.assertEqual(self._invoke(handler), {})

    def test_custom_response_key(self):
        @lambda_perf.wrap_handler("m", include_in_response=True, response_key="perf")
        def handler(event, context):
            return {}

        self.assertIn("perf", self._invoke(handler))

    def test_response_is_json_serializable(self):
        from datetime import datetime
        from decimal import Decimal

        @lambda_perf.wrap_handler("m", include_in_response=True)
        def handler(event, context):
            lambda_perf.perf_log.add_attribute("amount", Decimal("1.50"))
            lambda_perf.perf_log.add_attribute("at", datetime(2026, 1, 1))
            return {}

        result = self._invoke(handler)

        json.dumps(result)
        self.assertEqual(result["lambda_perf"]["attributes"]["amount"], "1.50")

    def test_original_response_not_mutated(self):
        original = {"statusCode": 200}

        @lambda_perf.wrap_handler("m", include_in_response=True)
        def handler(event, context):
            return original

        self._invoke(handler)

        self.assertEqual(original, {"statusCode": 200})


class EnvFlagTests(unittest.TestCase):

    def test_values(self):
        for value, expected in [
            ("true", True), ("TRUE", True), ("1", True), ("yes", True),
            ("on", True), ("false", False), ("0", False), ("", False),
        ]:
            with mock.patch.dict(os.environ, {"X_FLAG": value}):
                self.assertIs(lambda_perf.env_flag("X_FLAG"), expected, value)

    def test_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(lambda_perf.env_flag("X_FLAG"))
            self.assertTrue(lambda_perf.env_flag("X_FLAG", default=True))


class ExampleHandlerTests(unittest.TestCase):

    def test_example_handler(self):
        import importlib

        with mock.patch.dict(os.environ, {
            "PERF_MEASURE_INPUT": "true",
            "LAMBDA_PERF_INCLUDE_IN_RESPONSE": "true",
        }):
            import lambda_function
            importlib.reload(lambda_function)

            with contextlib.redirect_stdout(io.StringIO()):
                result = lambda_function.lambda_handler(
                    {"transaction": "quote"}, _context(),
                )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(
            result["lambda_perf"]["attributes"]["transaction_requested"], "quote",
        )
        self.assertIn("input_size_bytes", result["lambda_perf"]["attributes"])


if __name__ == "__main__":
    unittest.main()
