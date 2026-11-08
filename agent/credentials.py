"""Protected persistence for coordinator-issued helper credentials.

The supported helper platform is Windows. Credential payloads are encrypted
with Windows Data Protection API (DPAPI), scoped to the current Windows user,
before they are written to the agent state directory. Tests inject small
protect/unprotect callables and never touch a real credential store.
"""

from __future__ import annotations

import ctypes
import json
import os
import uuid
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path


_CREDENTIAL_FILENAME = "registration.dpapi"
_PAYLOAD_VERSION = 1
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class CredentialStoreError(RuntimeError):
    """Raised when protected registration state cannot be safely used."""


@dataclass(frozen=True, slots=True)
class StoredRegistration:
    """Server-issued identity bound to the coordinator that created it."""

    coordinator_url: str
    node_id: str
    credentials: str = field(repr=False)


class _DataBlob(ctypes.Structure):
    """Windows ``DATA_BLOB`` structure used by DPAPI."""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(data: bytes) -> tuple[_DataBlob, object]:
    """Create a DPAPI input blob while retaining its backing buffer."""

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _windows_libraries() -> tuple[object, object]:
    """Load DPAPI libraries only when protected storage is actually used."""

    if os.name != "nt":
        raise CredentialStoreError(
            "Protected credential persistence currently requires Windows"
        )
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise CredentialStoreError("Windows credential protection is unavailable") from exc
    return crypt32, kernel32


def _protect_with_dpapi(data: bytes) -> bytes:
    """Encrypt bytes for the current Windows user with no UI prompts."""

    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _input_blob(data)
    output_blob = _DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    succeeded = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "LowLlama helper registration",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not succeeded:
        error_code = ctypes.get_last_error()
        raise CredentialStoreError(
            f"Windows could not protect helper credentials (error {error_code})"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _unprotect_with_dpapi(data: bytes) -> bytes:
    """Decrypt bytes previously protected for the current Windows user."""

    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _input_blob(data)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    succeeded = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not succeeded:
        error_code = ctypes.get_last_error()
        raise CredentialStoreError(
            f"Windows could not decrypt helper credentials (error {error_code})"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output_blob.pbData)


class CredentialStore:
    """Persist one coordinator registration as a DPAPI-protected payload."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / _CREDENTIAL_FILENAME
        self._protect = protect or _protect_with_dpapi
        self._unprotect = unprotect or _unprotect_with_dpapi

    def load(self, coordinator_url: str) -> StoredRegistration | None:
        """Load registration for ``coordinator_url``, or return ``None``."""

        if not self._path.exists():
            return None
        try:
            protected = self._path.read_bytes()
            plaintext = self._unprotect(protected)
            payload = json.loads(plaintext.decode("utf-8"))
        except CredentialStoreError:
            raise
        except (OSError, UnicodeDecodeError, JSONDecodeError, ValueError) as exc:
            raise CredentialStoreError(
                "Stored helper registration is unreadable or corrupted"
            ) from exc

        registration = self._validate_payload(payload)
        if registration.coordinator_url != coordinator_url:
            raise CredentialStoreError(
                "Stored helper registration belongs to a different coordinator"
            )
        return registration

    def save(self, registration: StoredRegistration) -> None:
        """Atomically save a validated registration without plaintext on disk."""

        registration = self._validate_registration(registration)
        plaintext = json.dumps(
            {
                "version": _PAYLOAD_VERSION,
                "coordinator_url": registration.coordinator_url,
                "node_id": registration.node_id,
                "credentials": registration.credentials,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        protected = self._protect(plaintext)
        if not isinstance(protected, bytes) or not protected:
            raise CredentialStoreError("Credential protection returned invalid data")

        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CredentialStoreError(
                "Unable to create the helper credential directory"
            ) from exc

        temporary_path = self._path.with_name(
            f".{self._path.name}.{uuid.uuid4().hex}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as credential_file:
                descriptor = None
                credential_file.write(protected)
                credential_file.flush()
                os.fsync(credential_file.fileno())
            os.replace(temporary_path, self._path)
        except OSError as exc:
            raise CredentialStoreError("Unable to persist helper credentials") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self) -> None:
        """Remove rejected registration state without affecting client identity."""

        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialStoreError("Unable to remove rejected credentials") from exc

    @staticmethod
    def _validate_payload(payload: object) -> StoredRegistration:
        """Validate decrypted JSON before constructing a registration."""

        if not isinstance(payload, dict) or payload.get("version") != _PAYLOAD_VERSION:
            raise CredentialStoreError("Stored helper registration has an invalid format")
        return CredentialStore._validate_registration(
            StoredRegistration(
                coordinator_url=payload.get("coordinator_url"),
                node_id=payload.get("node_id"),
                credentials=payload.get("credentials"),
            )
        )

    @staticmethod
    def _validate_registration(registration: StoredRegistration) -> StoredRegistration:
        """Reject empty, malformed, or unexpectedly large credential fields."""

        fields = (
            (registration.coordinator_url, 2048),
            (registration.node_id, 128),
            (registration.credentials, 4096),
        )
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > maximum
            for value, maximum in fields
        ):
            raise CredentialStoreError("Stored helper registration has invalid fields")
        return registration
