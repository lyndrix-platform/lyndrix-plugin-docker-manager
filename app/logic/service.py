"""Service layer for Docker Manager."""
from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

import requests

# Hostnames per RFC 1123 (labels of [a-z0-9-], no leading/trailing hyphen).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def _validate_host_target(value: str) -> None:
    """Reject SSRF-prone or malformed Docker host targets.

    A host's ``ip`` is operator-supplied and the core process then issues
    GET/POST requests to it, so we validate it is a sane IP/hostname and block
    the cloud-metadata / link-local range (169.254.0.0/16, fd00:ec2::/…) and
    other non-routable categories. Private/loopback addresses stay allowed
    because Docker proxies commonly live on the LAN or the host itself.
    """
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        # Not a literal IP — accept it only if it is a syntactically valid hostname.
        if not _HOSTNAME_RE.match(value):
            raise ValueError(f"Invalid host address: {value!r}")
        return

    if (
        addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    ):
        raise ValueError(
            f"Disallowed host address (link-local/metadata/reserved): {value!r}"
        )


class DockerManagerService:
    def __init__(self) -> None:
        self._ctx = None
        self._lock = threading.RLock()
        self._hosts: List[Dict[str, Any]] = []

    @property
    def is_ready(self) -> bool:
        return self._ctx is not None

    def set_context(self, ctx) -> None:
        self._ctx = ctx

    def _normalize_host(self, raw_host: Dict[str, Any], fallback_id: int) -> Dict[str, Any]:
        if not isinstance(raw_host, dict):
            raise ValueError("Host must be an object")

        name = str(raw_host.get("name", "")).strip()
        ip = str(raw_host.get("ip", "")).strip()
        if not name or not ip:
            raise ValueError("Host requires 'name' and 'ip'")
        # Validate the target to reduce the SSRF surface (operator-supplied value
        # that the core process will issue HTTP requests to).
        _validate_host_target(ip)

        try:
            port = int(raw_host.get("port", 2375) or 2375)
        except Exception as exc:
            raise ValueError("Port must be numeric") from exc

        scheme = str(raw_host.get("scheme", "http") or "http").strip().lower()
        if scheme not in {"http", "https"}:
            scheme = "http"

        host_id = raw_host.get("id", fallback_id)
        try:
            host_id = int(host_id)
        except Exception as exc:
            raise ValueError("Host id must be numeric") from exc

        return {
            "id": host_id,
            "name": name,
            "ip": ip,
            "port": port,
            "scheme": scheme,
        }

    def _emit_hosts_updated(self) -> None:
        if not self._ctx:
            return
        self._ctx.emit("docker_manager:hosts:updated", {"hosts": self.list_hosts()})

    def load_hosts_from_vault(self) -> int:
        if not self._ctx:
            return 0

        stored_hosts = self._ctx.get_secret("configured_hosts")
        if not stored_hosts:
            with self._lock:
                self._hosts = []
            return 0

        try:
            parsed = json.loads(stored_hosts)
            if not isinstance(parsed, list):
                raise ValueError("configured_hosts must be a list")
            self.replace_hosts(parsed, persist=False, emit=False)
        except Exception as exc:
            self._ctx.log.error(f"Docker Manager: failed to parse host data from Vault: {exc}")
            with self._lock:
                self._hosts = []

        return len(self._hosts)

    def save_hosts_to_vault(self) -> bool:
        if not self._ctx:
            return False
        with self._lock:
            payload = json.dumps(self._hosts)
        return bool(self._ctx.set_secret("configured_hosts", payload))

    def list_hosts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted([dict(host) for host in self._hosts], key=lambda h: h["id"])

    def replace_hosts(self, hosts: List[Dict[str, Any]], *, persist: bool = True, emit: bool = True) -> List[Dict[str, Any]]:
        if not isinstance(hosts, list):
            raise ValueError("hosts must be a list")

        normalized: List[Dict[str, Any]] = []
        next_id = 1
        for raw_host in hosts:
            host = self._normalize_host(raw_host, fallback_id=next_id)
            next_id = max(next_id, int(host["id"]) + 1)
            normalized.append(host)

        by_id = {host["id"]: host for host in normalized}
        final_hosts = sorted(by_id.values(), key=lambda h: h["id"])

        with self._lock:
            self._hosts = final_hosts

        if persist:
            self.save_hosts_to_vault()
        if emit:
            self._emit_hosts_updated()

        return self.list_hosts()

    def upsert_host(self, host_payload: Dict[str, Any], *, persist: bool = True, emit: bool = True) -> Dict[str, Any]:
        if not isinstance(host_payload, dict):
            raise ValueError("host payload must be an object")

        with self._lock:
            existing = list(self._hosts)
            next_id = max([h["id"] for h in existing] + [0]) + 1

            raw_id = host_payload.get("id")
            if raw_id is None:
                host = self._normalize_host(host_payload, fallback_id=next_id)
                existing.append(host)
            else:
                host_id = int(raw_id)
                host = self._normalize_host(host_payload, fallback_id=host_id)
                updated = False
                for idx, item in enumerate(existing):
                    if item["id"] == host_id:
                        existing[idx] = host
                        updated = True
                        break
                if not updated:
                    existing.append(host)

            self._hosts = sorted(existing, key=lambda h: h["id"])

        if persist:
            self.save_hosts_to_vault()
        if emit:
            self._emit_hosts_updated()

        return host

    def delete_host(self, host_id: int, *, persist: bool = True, emit: bool = True) -> bool:
        with self._lock:
            before = len(self._hosts)
            self._hosts = [h for h in self._hosts if int(h["id"]) != int(host_id)]
            changed = len(self._hosts) != before

        if changed and persist:
            self.save_hosts_to_vault()
        if changed and emit:
            self._emit_hosts_updated()

        return changed

    def _resolve_host(self, host_id: int) -> Dict[str, Any]:
        with self._lock:
            for host in self._hosts:
                if int(host["id"]) == int(host_id):
                    return dict(host)
        raise ValueError(f"Unknown host id: {host_id}")

    @staticmethod
    def _base_url(host: Dict[str, Any]) -> str:
        return f"{host['scheme']}://{host['ip']}:{host['port']}"

    @staticmethod
    def _container_to_row(container: Dict[str, Any]) -> Dict[str, Any]:
        name = (container.get("Names") or ["/unknown"])[0].lstrip("/")
        return {
            "id": (container.get("Id") or "")[:12],
            "raw_id": container.get("Id", ""),
            "name": name,
            "image": container.get("Image"),
            "state": container.get("State"),
            "status": container.get("Status"),
        }

    def fetch_single_host(self, host: Dict[str, Any], *, include_stopped: bool = True) -> List[Dict[str, Any]]:
        params = {"all": 1 if include_stopped else 0}
        url = f"{self._base_url(host)}/containers/json"
        response = requests.get(url, params=params, timeout=2.0)
        response.raise_for_status()
        return [self._container_to_row(item) for item in response.json()]

    def fetch_all_containers_parallel(self, *, include_stopped: bool = True) -> Dict[str, Any]:
        hosts = self.list_hosts()
        if not hosts:
            return {}

        results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(15, max(1, len(hosts)))) as executor:
            futures = {
                executor.submit(self.fetch_single_host, host, include_stopped=include_stopped): host
                for host in hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                host_key = f"{host['name']}#{host['id']}"
                try:
                    containers = future.result()
                    results[host_key] = {
                        "host": host,
                        "containers": containers,
                        "error": None,
                    }
                except Exception as exc:
                    results[host_key] = {
                        "host": host,
                        "containers": [],
                        "error": str(exc),
                    }

        return dict(sorted(results.items(), key=lambda item: item[1]["host"]["name"].lower()))

    @staticmethod
    def _container_refs(container_id: str, container_name: str | None = None) -> List[str]:
        refs: List[str] = []
        raw_id = (container_id or "").strip()
        if raw_id:
            refs.append(raw_id)
            if len(raw_id) > 12:
                refs.append(raw_id[:12])
        raw_name = (container_name or "").strip().lstrip("/")
        if raw_name:
            refs.append(raw_name)

        # keep order while removing duplicates
        seen = set()
        unique: List[str] = []
        for ref in refs:
            if ref not in seen:
                seen.add(ref)
                unique.append(ref)
        return unique

    def _post_container_endpoint(
        self,
        host: Dict[str, Any],
        refs: List[str],
        endpoint: str,
        *,
        params: Dict[str, Any] | None = None,
        timeout: float = 4.0,
    ) -> tuple[requests.Response, str]:
        if not refs:
            raise ValueError("Missing container reference")

        last_404: requests.Response | None = None
        for ref in refs:
            url = f"{self._base_url(host)}/containers/{ref}/{endpoint}"
            response = requests.post(url, params=params, json={}, timeout=timeout)
            if response.status_code == 404:
                last_404 = response
                continue
            return response, ref

        if last_404 is not None:
            raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")
        raise RuntimeError("Docker API request failed")

    def _container_running(self, host: Dict[str, Any], refs: List[str]) -> bool:
        for ref in refs:
            url = f"{self._base_url(host)}/containers/{ref}/json"
            response = requests.get(url, timeout=3.0)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            state = (response.json().get("State") or {}).get("Running")
            return bool(state)
        raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")

    def container_action(
        self,
        host_id: int,
        container_id: str,
        action: str,
        container_name: str | None = None,
    ) -> Dict[str, Any]:
        action_map = {
            "start": ("start", {}),
            "stop": ("stop", {"t": 10}),
            "restart": ("restart", {"t": 10}),
        }
        if action not in action_map:
            raise ValueError(f"Unsupported action: {action}")

        host = self._resolve_host(host_id)
        refs = self._container_refs(container_id, container_name)
        endpoint, params = action_map[action]
        response, used_ref = self._post_container_endpoint(host, refs, endpoint, params=params, timeout=6.0)
        if response.status_code not in (200, 204, 304):
            raise RuntimeError(f"Docker API error {response.status_code}: {response.text[:300]}")

        # Some environments immediately restart stopped containers via policies.
        # If stop did not effectively stop the container, send kill as fallback.
        if action == "stop":
            still_running = False
            try:
                for _ in range(5):
                    still_running = self._container_running(host, refs)
                    if not still_running:
                        break
                    time.sleep(0.2)
            except (requests.RequestException, RuntimeError) as exc:
                # A transient Docker API error during the liveness check must not
                # silently mask a still-running container; log and skip the kill
                # fallback rather than assert the container is stopped.
                if self._ctx:
                    self._ctx.log.warning(
                        f"Docker Manager: liveness check after stop failed: {exc}"
                    )
                still_running = False

            if still_running:
                kill_resp, _ = self._post_container_endpoint(host, refs, "kill", timeout=4.0)
                if kill_resp.status_code not in (200, 204, 304, 409):
                    raise RuntimeError(
                        f"Docker API stop/kill fallback failed {kill_resp.status_code}: {kill_resp.text[:300]}"
                    )

        if self._ctx:
            self._ctx.emit(
                "docker_manager:container:action",
                {
                    "host_id": host_id,
                    "container_id": container_id,
                    "container_ref": used_ref,
                    "action": action,
                    "status_code": response.status_code,
                },
            )

        return {
            "ok": True,
            "action": action,
            "host_id": host_id,
            "container_id": container_id,
            "container_ref": used_ref,
            "status_code": response.status_code,
        }

    def container_logs(
        self,
        host_id: int,
        container_id: str,
        *,
        tail: int = 200,
        container_name: str | None = None,
    ) -> str:
        host = self._resolve_host(host_id)
        refs = self._container_refs(container_id, container_name)

        last_404 = False
        for ref in refs:
            url = f"{self._base_url(host)}/containers/{ref}/logs"
            response = requests.get(
                url,
                params={"stdout": 1, "stderr": 1, "tail": max(1, min(int(tail), 2000))},
                timeout=6.0,
            )
            if response.status_code == 404:
                last_404 = True
                continue
            response.raise_for_status()
            if response.text:
                return response.text
            if response.content:
                return response.content.decode(errors="replace")
            return "(no logs returned)"

        if last_404:
            raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")
        return "(no logs returned)"

    def container_shell_snapshot(
        self,
        host_id: int,
        container_id: str,
        container_name: str | None = None,
    ) -> str:
        host = self._resolve_host(host_id)
        base_url = self._base_url(host)
        refs = self._container_refs(container_id, container_name)

        commands = [
            ["sh", "-lc", "whoami && pwd && ls -la | head -50"],
            ["/bin/sh", "-lc", "whoami && pwd && ls -la | head -50"],
            ["bash", "-lc", "whoami && pwd && ls -la | head -50"],
            ["/bin/bash", "-lc", "whoami && pwd && ls -la | head -50"],
        ]

        errors: List[str] = []
        saw_exec_forbidden = False

        for ref in refs:
            for cmd in commands:
                create_payload = {
                    "AttachStdout": True,
                    "AttachStderr": True,
                    "Tty": True,
                    "Cmd": cmd,
                }
                create_resp = requests.post(
                    f"{base_url}/containers/{ref}/exec",
                    json=create_payload,
                    timeout=5.0,
                )
                if create_resp.status_code == 404:
                    errors.append(f"{ref}: not found")
                    break
                if create_resp.status_code == 409:
                    raise RuntimeError("Container is not running. Start it before opening shell snapshot.")
                if create_resp.status_code >= 400:
                    errors.append(f"{ref}/{cmd[0]}: create failed {create_resp.status_code}")
                    continue

                exec_id = create_resp.json().get("Id")
                if not exec_id:
                    errors.append(f"{ref}/{cmd[0]}: exec id missing")
                    continue

                start_resp = requests.post(
                    f"{base_url}/exec/{exec_id}/start",
                    json={"Detach": False, "Tty": True},
                    timeout=8.0,
                )
                if start_resp.status_code == 403:
                    saw_exec_forbidden = True
                    errors.append(f"{ref}/{cmd[0]}: exec forbidden (403)")
                    continue
                if start_resp.status_code >= 400:
                    errors.append(f"{ref}/{cmd[0]}: start failed {start_resp.status_code}")
                    continue

                if start_resp.text:
                    return start_resp.text
                if start_resp.content:
                    return start_resp.content.decode(errors="replace")
                return "(shell command returned no output)"

        if saw_exec_forbidden:
            return self._read_only_runtime_snapshot(host, refs)

        details = "; ".join(errors[-6:]) if errors else "unknown error"
        raise RuntimeError(f"Shell snapshot failed: {details}")

    def _read_only_runtime_snapshot(self, host: Dict[str, Any], refs: List[str]) -> str:
        """Fallback when /exec is blocked by a Docker socket proxy.

        Uses only read-only endpoints to provide useful diagnostics.
        """
        lines: List[str] = []
        lines.append("Docker proxy denied exec (HTTP 403).")
        lines.append("Showing read-only runtime snapshot instead.\n")

        selected_ref = refs[0] if refs else ""
        inspect_data = None
        for ref in refs:
            inspect_resp = requests.get(f"{self._base_url(host)}/containers/{ref}/json", timeout=4.0)
            if inspect_resp.status_code == 404:
                continue
            inspect_resp.raise_for_status()
            inspect_data = inspect_resp.json()
            selected_ref = ref
            break

        if inspect_data is None:
            raise RuntimeError(f"Container not found on host (tried: {', '.join(refs)})")

        name = (inspect_data.get("Name") or "").lstrip("/") or selected_ref
        image = inspect_data.get("Config", {}).get("Image", "unknown")
        state = inspect_data.get("State", {})
        status = state.get("Status", "unknown")
        running = state.get("Running", False)
        started_at = state.get("StartedAt", "unknown")
        pid = state.get("Pid", "unknown")

        lines.append(f"Container: {name}")
        lines.append(f"Reference: {selected_ref}")
        lines.append(f"Image: {image}")
        lines.append(f"Status: {status} (running={running})")
        lines.append(f"PID: {pid}")
        lines.append(f"StartedAt: {started_at}\n")

        top_resp = requests.get(
            f"{self._base_url(host)}/containers/{selected_ref}/top",
            params={"ps_args": "aux"},
            timeout=5.0,
        )
        if top_resp.status_code in (200, 201):
            data = top_resp.json() if top_resp.content else {}
            titles = data.get("Titles") or []
            processes = data.get("Processes") or []
            if titles and processes:
                lines.append("Processes:")
                lines.append(" | ".join(titles))
                for row in processes[:30]:
                    lines.append(" | ".join(str(item) for item in row))
            else:
                lines.append("Processes: no process data returned")
        elif top_resp.status_code == 404:
            lines.append("Processes: container not found for /top")
        elif top_resp.status_code == 403:
            lines.append("Processes: /top also forbidden by proxy (403)")
        else:
            lines.append(f"Processes: /top failed with HTTP {top_resp.status_code}")

        return "\n".join(lines)


docker_manager_service = DockerManagerService()
