"""Unit tests for protected coordinator credential persistence."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.credentials import (
    CredentialStore,
    CredentialStoreError,
    StoredRegistration,
)


def _protect(data: bytes) -> bytes:
    """Make test ciphertext visibly distinct without using machine DPAPI."""

    return b"protected:" + data[::-1]


def _unprotect(data: bytes) -> bytes:
    if not data.startswith(b"protected:"):
        raise CredentialStoreError("test ciphertext is invalid")
    return data.removeprefix(b"protected:")[::-1]


class CredentialStoreTests(unittest.TestCase):
    """Verify encryption boundaries, validation, and coordinator binding."""

    def test_saves_and_loads_registration_without_plaintext(self) -> None:
        credential = "server-secret-must-not-be-plaintext"
        registration = StoredRegistration(
            "https://coordinator.test:8000",
            "node-123",
            credential,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = CredentialStore(
                temporary_directory,
                protect=_protect,
                unprotect=_unprotect,
            )

            store.save(registration)
            raw_file = (Path(temporary_directory) / "registration.dpapi").read_bytes()
            loaded = store.load("https://coordinator.test:8000")

        self.assertEqual(loaded, registration)
        self.assertNotIn(credential.encode("utf-8"), raw_file)
        self.assertNotIn(credential, repr(registration))

    def test_missing_registration_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = CredentialStore(
                temporary_directory,
                protect=_protect,
                unprotect=_unprotect,
            )

            self.assertIsNone(store.load("https://coordinator.test:8000"))

    def test_clear_removes_only_registration_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            identity_path = state_dir / "client_id"
            identity_path.write_text("local-client\n", encoding="utf-8")
            store = CredentialStore(
                state_dir,
                protect=_protect,
                unprotect=_unprotect,
            )
            store.save(
                StoredRegistration(
                    "https://coordinator.test:8000",
                    "node-123",
                    "credential",
                )
            )

            store.clear()

            self.assertIsNone(store.load("https://coordinator.test:8000"))
            self.assertTrue(identity_path.exists())

    def test_rejects_registration_for_different_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = CredentialStore(
                temporary_directory,
                protect=_protect,
                unprotect=_unprotect,
            )
            store.save(
                StoredRegistration(
                    "https://first-coordinator.test:8000",
                    "node-123",
                    "credential",
                )
            )

            with self.assertRaises(CredentialStoreError):
                store.load("https://second-coordinator.test:8000")

    def test_rejects_corrupted_or_invalid_payloads(self) -> None:
        invalid_plaintexts = (
            b"not-json",
            json.dumps({"version": 99}).encode("utf-8"),
            json.dumps(
                {
                    "version": 1,
                    "coordinator_url": "https://coordinator.test:8000",
                    "node_id": "",
                    "credentials": "credential",
                }
            ).encode("utf-8"),
        )
        for plaintext in invalid_plaintexts:
            with self.subTest(plaintext=plaintext):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    path = Path(temporary_directory) / "registration.dpapi"
                    path.write_bytes(_protect(plaintext))
                    store = CredentialStore(
                        temporary_directory,
                        protect=_protect,
                        unprotect=_unprotect,
                    )

                    with self.assertRaises(CredentialStoreError):
                        store.load("https://coordinator.test:8000")

    def test_rejects_invalid_protector_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = CredentialStore(
                temporary_directory,
                protect=lambda _data: b"",
                unprotect=_unprotect,
            )

            with self.assertRaises(CredentialStoreError):
                store.save(
                    StoredRegistration(
                        "https://coordinator.test:8000",
                        "node-123",
                        "credential",
                    )
                )


if __name__ == "__main__":
    unittest.main()
