"""Safe hardware discovery for a LowLlama helper laptop."""

from __future__ import annotations

import platform
import socket
from collections.abc import Callable
from typing import TypedDict

import psutil


_BYTES_PER_GIB = 1024**3


class HardwareInfo(TypedDict):
    """Hardware fields reported by a helper agent.

    A value is ``None`` when the operating system or runtime cannot provide it.
    RAM values are expressed in gibibytes and rounded to two decimal places.
    """

    hostname: str | None
    operating_system: str | None
    os_version: str | None
    cpu_model: str | None
    cpu_architecture: str | None
    cpu_core_count: int | None
    total_ram_gb: float | None
    available_ram_gb: float | None
    python_version: str | None


def _safe_text(probe: Callable[[], object]) -> str | None:
    """Return a normalized text result, or ``None`` if a probe fails."""

    try:
        value = probe()
    except Exception:
        return None

    if value is None:
        return None

    text = str(value).strip()
    return text or None


def _detect_cpu_core_count() -> int | None:
    """Prefer physical CPU cores and fall back to the logical core count."""

    for logical in (False, True):
        try:
            count = psutil.cpu_count(logical=logical)
        except Exception:
            continue
        if isinstance(count, int) and count > 0:
            return count
    return None


def _bytes_to_gib(value: object) -> float | None:
    """Convert a non-negative byte count to GiB without propagating errors."""

    try:
        byte_count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if byte_count < 0:
        return None
    return round(byte_count / _BYTES_PER_GIB, 2)


def _detect_memory() -> tuple[float | None, float | None]:
    """Return total and currently available RAM in GiB."""

    try:
        memory = psutil.virtual_memory()
    except Exception:
        return None, None

    try:
        total = memory.total
    except Exception:
        total = None
    try:
        available = memory.available
    except Exception:
        available = None

    return _bytes_to_gib(total), _bytes_to_gib(available)


def get_available_ram_gb() -> float | None:
    """Return current available RAM in GiB, or ``None`` when unavailable."""

    try:
        available = psutil.virtual_memory().available
    except Exception:
        return None
    return _bytes_to_gib(available)


def get_hardware_info() -> HardwareInfo:
    """Collect a small, non-sensitive hardware summary for this helper.

    Each probe is isolated so an unavailable field does not prevent the other
    fields from being returned. The function reads only general system and
    runtime properties; it does not inspect user files, credentials, or other
    personal data.
    """

    total_ram_gb, available_ram_gb = _detect_memory()

    return {
        "hostname": _safe_text(socket.gethostname),
        "operating_system": _safe_text(platform.system),
        "os_version": _safe_text(platform.version),
        "cpu_model": _safe_text(platform.processor),
        "cpu_architecture": _safe_text(platform.machine),
        "cpu_core_count": _detect_cpu_core_count(),
        "total_ram_gb": total_ram_gb,
        "available_ram_gb": available_ram_gb,
        "python_version": _safe_text(platform.python_version),
    }
