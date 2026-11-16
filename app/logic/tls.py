"""TLS settings of a Docker proxy connection and the client-certificate store.

A host row carries only non-secret TLS settings (verify flag, CA bundle path,
"has a client certificate"). The client certificate and its private key live in
Vault (plugin secret ``host_tls_client_<id>``) and are never written to the
database, an API response, the bus or a log line.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# Vault is re-read after this many seconds so a rotated certificate is picked
# up without a restart (and at once on the instance that stored it).
CLIENT_CERT_TTL_SECONDS = 300.0
MAX_PEM_BYTES = 64 * 1024

WARNING_PLAINTEXT = "unencrypted_unauthenticated"
WARNING_UNVERIFIED = "tls_unverified"
WARNING_NO_CLIENT_AUTH = "tls_no_client_auth"


def secret_key(host_id: int) -> str:
    return f"host_tls_client_{int(host_id)}"


def normalize_ca_path(value: Any) -> Optional[str]:
    """``None``/empty clears the setting; otherwise an absolute file path."""
    if value is None or str(value).strip() == "":
        return None
    path = str(value).strip()
    if "\x00" in path or not os.path.isabs(path) or len(path) > 1024:
        raise ValueError("tls_ca_path must be an absolute path to a PEM CA bundle (max 1024 chars)")
    return path


def normalize_client_pem(cert: Any, key: Any) -> Dict[str, str]:
    """Validate a client certificate + private key pair (PEM text)."""
    if not isinstance(cert, str) or not isinstance(key, str) or not cert.strip() or not key.strip():
        raise ValueError("tls_client_cert_pem and tls_client_key_pem must be given together")
    if len(cert.encode()) > MAX_PEM_BYTES or len(key.encode()) > MAX_PEM_BYTES:
        raise ValueError("Client certificate/key too large")
    if "-----BEGIN CERTIFICATE-----" not in cert:
        raise ValueError("tls_client_cert_pem is not a PEM certificate")
    if "-----BEGIN" not in key or "PRIVATE KEY-----" not in key:
        raise ValueError("tls_client_key_pem is not a PEM private key")
    return {"cert": cert.strip() + "\n", "key": key.strip() + "\n"}


def security_summary(scheme: str, verify: bool, client_auth: bool) -> Dict[str, Any]:
    """How well a host's connection is protected, for API consumers.

    ``insecure`` is true when the traffic can be read or redirected on the
    path (plain http, or https without certificate verification). A verified
    TLS connection without a client certificate is confidential but the proxy
    still accepts anyone who can reach its port — reported as a warning only.
    """
    encrypted = scheme == "https"
    verified = encrypted and verify
    client = encrypted and client_auth
    if not encrypted:
        warning: Optional[str] = WARNING_PLAINTEXT
    elif not verified:
        warning = WARNING_UNVERIFIED
    elif not client:
        warning = WARNING_NO_CLIENT_AUTH
    else:
        warning = None
    return {
        "security": {"encrypted": encrypted, "server_verified": verified, "client_auth": client},
        "insecure": not verified,
        "security_warning": warning,
    }


class ClientCertStore:
    """Client certificates in Vault, materialised as private files on demand.

    ``requests``/``ssl`` can only load a client certificate from files, so the
    PEM pair is written to a 0700 directory (0600 files) owned by this process
    and removed on ``close()``. File names carry a content hash: a rotated
    certificate gets new files and requests in flight keep theirs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dir: Optional[str] = None
        self._cache: Dict[int, Tuple[float, Tuple[str, str]]] = {}

    def invalidate(self, host_id: Optional[int] = None) -> None:
        with self._lock:
            if host_id is None:
                self._cache.clear()
            else:
                self._cache.pop(int(host_id), None)

    def close(self) -> None:
        with self._lock:
            self._cache.clear()
            directory, self._dir = self._dir, None
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    @staticmethod
    def store(set_secret: Callable[[str, str], Any], host_id: int, pem: Optional[Dict[str, str]]) -> None:
        """Write (or, with ``pem=None``, blank) the Vault secret. Blocking."""
        value = json.dumps(pem) if pem else ""
        if not set_secret(secret_key(host_id), value):
            raise RuntimeError("Vault is not available — client certificate not stored")

    def files(self, get_secret: Callable[[str], Any], host_id: int) -> Tuple[str, str]:
        """``(cert_file, key_file)`` for ``requests``' ``cert=``. Blocking.

        Raises ``RuntimeError`` when the material is missing: a host that is
        configured for client authentication must never be contacted without it.
        """
        host_id = int(host_id)
        with self._lock:
            cached = self._cache.get(host_id)
            if cached and time.monotonic() - cached[0] < CLIENT_CERT_TTL_SECONDS and all(
                os.path.exists(path) for path in cached[1]
            ):
                return cached[1]
        raw = get_secret(secret_key(host_id))
        try:
            pem = json.loads(raw) if raw else None
            cert, key = pem["cert"], pem["key"]
        except Exception:
            raise RuntimeError(
                f"Client certificate of host {host_id} is not available in Vault"
            ) from None  # never chain: the original error may quote the secret
        with self._lock:
            if self._dir is None or not os.path.isdir(self._dir):
                self._dir = tempfile.mkdtemp(prefix="lyndrix-docker-tls-")  # mode 0700
            digest = hashlib.sha256((cert + key).encode()).hexdigest()[:16]
            paths = (
                os.path.join(self._dir, f"host-{host_id}-{digest}.crt"),
                os.path.join(self._dir, f"host-{host_id}-{digest}.key"),
            )
            for path, content in zip(paths, (cert, key)):
                if not os.path.exists(path):
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as handle:
                        handle.write(content)
            self._cache[host_id] = (time.monotonic(), paths)
            return paths
