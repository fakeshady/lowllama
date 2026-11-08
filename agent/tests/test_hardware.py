"""Unit tests for helper hardware discovery."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

# Allow this file to be run directly as well as through unittest discovery.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.hardware import get_available_ram_gb, get_hardware_info


class GetHardwareInfoTests(unittest.TestCase):
    """Verify successful discovery and graceful probe failures."""

    @patch("agent.hardware.platform.python_version", return_value="3.12.3")
    @patch("agent.hardware.platform.machine", return_value="AMD64")
    @patch("agent.hardware.platform.processor", return_value="Example CPU")
    @patch("agent.hardware.platform.version", return_value="10.0.26100")
    @patch("agent.hardware.platform.system", return_value="Windows")
    @patch("agent.hardware.socket.gethostname", return_value="helper-one")
    @patch("agent.hardware.psutil.cpu_count")
    @patch("agent.hardware.psutil.virtual_memory")
    def test_returns_structured_hardware_information(
        self,
        virtual_memory,
        cpu_count,
        _gethostname,
        _system,
        _version,
        _processor,
        _machine,
        _python_version,
    ) -> None:
        virtual_memory.return_value = SimpleNamespace(
            total=16 * 1024**3,
            available=6.5 * 1024**3,
        )
        cpu_count.return_value = 8

        result = get_hardware_info()

        self.assertEqual(
            result,
            {
                "hostname": "helper-one",
                "operating_system": "Windows",
                "os_version": "10.0.26100",
                "cpu_model": "Example CPU",
                "cpu_architecture": "AMD64",
                "cpu_core_count": 8,
                "total_ram_gb": 16.0,
                "available_ram_gb": 6.5,
                "python_version": "3.12.3",
            },
        )
        cpu_count.assert_called_once_with(logical=False)

    @patch("agent.hardware.platform.python_version", side_effect=RuntimeError)
    @patch("agent.hardware.platform.machine", return_value="  ")
    @patch("agent.hardware.platform.processor", return_value=None)
    @patch("agent.hardware.platform.version", side_effect=OSError)
    @patch("agent.hardware.platform.system", return_value="Windows")
    @patch("agent.hardware.socket.gethostname", side_effect=OSError)
    @patch("agent.hardware.psutil.cpu_count", side_effect=OSError)
    @patch("agent.hardware.psutil.virtual_memory", side_effect=OSError)
    def test_probe_failures_return_none_without_crashing(
        self,
        _virtual_memory,
        _cpu_count,
        _gethostname,
        _system,
        _version,
        _processor,
        _machine,
        _python_version,
    ) -> None:
        result = get_hardware_info()

        self.assertEqual(result["operating_system"], "Windows")
        for field in (
            "hostname",
            "os_version",
            "cpu_model",
            "cpu_architecture",
            "cpu_core_count",
            "total_ram_gb",
            "available_ram_gb",
            "python_version",
        ):
            self.assertIsNone(result[field])

    @patch("agent.hardware.psutil.cpu_count", side_effect=[None, 20])
    @patch("agent.hardware.psutil.virtual_memory")
    def test_falls_back_to_logical_cores_and_rejects_invalid_memory(
        self,
        virtual_memory,
        cpu_count,
    ) -> None:
        virtual_memory.return_value = SimpleNamespace(total=-1, available="unknown")

        result = get_hardware_info()

        self.assertEqual(result["cpu_core_count"], 20)
        self.assertIsNone(result["total_ram_gb"])
        self.assertIsNone(result["available_ram_gb"])
        self.assertEqual(
            cpu_count.call_args_list,
            [call(logical=False), call(logical=True)],
        )

    @patch("agent.hardware.psutil.virtual_memory")
    def test_available_ram_probe_is_dynamic_and_safe(self, virtual_memory) -> None:
        virtual_memory.return_value = SimpleNamespace(
            total=16 * 1024**3,
            available=5 * 1024**3,
        )
        self.assertEqual(get_available_ram_gb(), 5.0)

        virtual_memory.side_effect = OSError("unavailable")
        self.assertIsNone(get_available_ram_gb())

    @patch("agent.hardware.psutil.virtual_memory")
    def test_available_ram_never_exceeds_total_ram(self, virtual_memory) -> None:
        virtual_memory.return_value = SimpleNamespace(
            total=4 * 1024**3,
            available=8 * 1024**3,
        )

        result = get_hardware_info()

        self.assertEqual(result["total_ram_gb"], 4.0)
        self.assertEqual(result["available_ram_gb"], 4.0)
        self.assertEqual(get_available_ram_gb(), 4.0)


if __name__ == "__main__":
    unittest.main()
