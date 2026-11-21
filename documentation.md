# Docker Manager — Dokumentation

## Übersicht

Der Docker Manager ermöglicht die zentrale Überwachung und Steuerung von Docker-Hosts über eine Remote-Proxy-HTTP-API (kein direkter Socket-Zugriff). Für jeden registrierten Host können Container in Echtzeit angezeigt und Aktionen wie Start, Stop, Restart, Log-Abruf und Shell-Zugriff ausgeführt werden.

Live-Updates laufen über den Lyndrix-Sockets-Layer (SSE). Das Plugin benötigt vor der ersten Nutzung eine manuelle Host-Konfiguration (`auto_enable_on_install=False`).

---

## Architektur

```
lyndrix-plugin-docker-manager/
├── entrypoint.py              # Manifest + Lifecycle-Hooks
├── app/
│   ├── permissions.py         # Berechtigungs-IDs (docker:logs/control/hosts/exec/audit)
│   ├── api.py                 # FastAPI-Router (Hosts + Container-Aktionen)
│   ├── model/
│   │   ├── models.py          # Tabellen docker_manager_hosts, docker_manager_audit
│   │   └── database.py        # Cluster-sichere DB-Helfer (Bootstrap-Lock, READ COMMITTED, Retry)
│   ├── logic/
│   │   ├── targets.py         # Ziel-Validierung (SSRF, IP-Pinning)
│   │   ├── tls.py             # Pro-Host-TLS, Client-Zertifikate in Vault
│   │   ├── audit.py           # Audit-Trail (docker_manager_audit)
│   │   └── service.py         # DockerManagerService: Host-Persistenz, Docker-Proxy, SSRF-Validierung
│   └── ui/
│       ├── nicegui/
│       │   ├── overview.py    # Container-Übersicht + Laufzeitsteuerung
│       │   ├── settings.py    # Host-Verwaltung im Plugin-Manager
│       │   └── widget.py      # Kompaktes Dashboard-Widget
│       └── react/             # React-Frontend (kanonisches Migrationsziel)
```

**`DockerManagerService`** verwaltet die Docker-Hosts, validiert alle Eingaben gegen SSRF-Angriffe und leitet HTTP-Anfragen an den Docker-Proxy weiter.

### Host-Speicherung und Cluster-Betrieb (ab 1.2.0)

- Die Hosts liegen in der gemeinsamen MariaDB-Tabelle `docker_manager_hosts` (Autoincrement-ID, eindeutiger Name, Adresse, Port, Schema, Labels, Quelle `manual`/`iac`, `ci_id`, Prüfergebnis). Jede Core-Instanz hinter dem Load-Balancer sieht dieselben Hosts und IDs; die Liste im Arbeitsspeicher ist nur ein Cache (nach eigenen Änderungen, beim Cluster-Event und spätestens nach 30 s neu gelesen).
- Die Tabelle wird idempotent unter dem Bootstrap-Lock `lyndrix_bootstrap` angelegt (beim Setup, wenn die DB schon verbunden ist, und bei `db:connected`).
- Schreibzugriffe laufen zeilenweise in kurzen READ-COMMITTED-Transaktionen und werden bei MariaDB-Fehler 1020/1205/1213 (Snapshot-Isolation, Lock-Timeout, Deadlock) sowie bei einem verlorenen Insert-Rennen auf denselben Namen wiederholt.
- Nach jeder Änderung sendet das Plugin `docker_manager:hosts_updated` mit `cluster=True`; die anderen Instanzen laden ihren Cache neu, wenn die Kopie `_origin` trägt.
- **Migration:** Die alte Vault-Liste `configured_hosts` wird einmalig importiert. IDs bleiben erhalten, sofern frei (sonst neue ID, wird geloggt); bereits vorhandene Namen gewinnen. Der Vault-Marker `configured_hosts_migrated_at` verhindert einen erneuten Import (gelöschte Hosts kommen nicht zurück).
- **Erreichbarkeit:** Genau eine Instanz prüft die Docker-Proxys im Hintergrund (Lease `docker_manager:prober`, Intervall `probe_interval_seconds`, Standard 60 s) und schreibt `reachable`, `checked_at` und `check_error` in die Tabelle. `health()` liest nur diese Werte (eine Abfrage) und meldet nicht erreichbare Proxys als `degraded`, nie als `error`. Der Prober nutzt einen eigenen Pool (4 Worker, 8 s Limit pro Host). `health()` führt keine DDL aus und meldet bis zum Abschluss des Bootstraps `degraded` (`schema_not_ready`).

---

## API-Referenz

Alle Routen sind unter `/api/plugins/lyndrix.plugin.docker/` erreichbar und erfordern eine gültige Authentifizierung. Seit 1.3.0 genügt das Basis-Recht `api:read`/`api:write` des Cores **nicht** mehr für sensible Routen; sie verlangen die unten genannten eigenen Berechtigungen (siehe [Berechtigungen und Rollen](#berechtigungen-und-rollen)).

| Methode | Pfad | Berechtigung | Beschreibung |
|---|---|---|---|
| `GET` | `/capabilities` | `api:read` | Was der Aufrufer darf (`logs`, `control`, `hosts`, `exec`, `audit` als Booleans); die UI blendet damit Bedienelemente aus |
| `GET` | `/hosts` | `api:read` | Liste aller Hosts (ohne `labels`, `check_error`, `tls_ca_path`, solange `docker:hosts` fehlt) |
| `POST` | `/hosts` | `docker:hosts` | Host anlegen oder (per Name/ID) aktualisieren: `name`, `ip`, `port`, `scheme`, TLS-Felder |
| `POST` | `/hosts/sync` | `docker:hosts` | Hosts per Name upserten, ohne andere zu entfernen (IaC-Orchestrator, Skripte); `port`/`scheme` optional; Antwort mit `unchanged`-Zähler |
| `POST` | `/hosts/set` | `docker:hosts` | Komplette Host-Liste ersetzen |
| `DELETE` | `/hosts/{host_id}` | `docker:hosts` | Host entfernen |
| `GET` | `/containers` | `api:read` | Container aller Hosts (`include_stopped`); ohne `docker:hosts` ohne interne Host-Details |
| `POST` | `/containers/action` | `docker:control` | Container-Aktion (`start` / `stop` / `restart`), Body `host_id`, `container_id`, `action` |
| `GET` | `/containers/{host_id}/{container_id}/logs` | `docker:logs` | Container-Logs (`tail`, Standard 200) |
| `POST` | `/containers/{host_id}/{container_id}/shell` | `docker:exec` | Shell-Snapshot per Docker-`exec` |
| `GET` | `/audit` | `docker:audit` | Audit-Trail, neueste zuerst; Keyset-Paging (`limit`, `before_id` = `next_before_id` der Vorseite), Filter `host_id`, `action`, `actor` |

Container-Referenzen werden nach der Docker-Grammatik validiert; Weiterleitungen (Redirects) folgt das Plugin nie.

---

## Berechtigungen und Rollen

Der Core vergibt `plugin:<id>:api:read` an jeden angemeldeten Benutzer und `api:write` an jeden Benutzer der Gruppe INT_USER. Deshalb sind alle sensiblen Funktionen eigene Berechtigungen (Präfix `plugin:lyndrix.plugin.docker:`), die weder durch die Basis-Rechte noch durch globales `api:read`/`api:write` erfüllt werden:

| Berechtigung | Schützt |
|---|---|
| `docker:logs` | Container-Logs |
| `docker:control` | Start / Stop / Restart |
| `docker:hosts` | Host-Registry (anlegen, ändern, löschen, synchronisieren) |
| `docker:exec` | Shell in Containern; außerdem die NiceGUI-Seite `/docker` |
| `docker:audit` | Audit-Trail lesen |

| Rolle | Rechte | Zuordnung |
|---|---|---|
| `admin` (Docker Manager Administrator) | alle fünf Rechte plus `api:read`/`api:write` | automatisch für die Gruppe `INT_ADMIN` |
| `operator` (Docker Manager Operator) | Lesen, Logs, Container-Steuerung | nicht automatisch; ein Administrator weist sie zu |
| `viewer` (Docker Manager Viewer) | Hosts und Container mit Status | nicht automatisch |

**Hinweis zum Update auf 1.3.0:** Benutzer außerhalb von `INT_ADMIN` verlieren Log-, Steuerungs- und Host-Registry-Zugriff, bis ihnen die Rolle Operator/Administrator (oder die einzelnen Rechte) zugewiesen wurde. Ein eingeschränkter Benutzer-API-Key, der `/hosts/sync` aufruft, braucht `docker:hosts`; der System-API-Key ist nicht betroffen.

### Audit-Trail

Die append-only Tabelle `docker_manager_audit` (idempotent unter dem Bootstrap-Lock angelegt) protokolliert Container-Start/-Stop/-Restart, Log-Abrufe, Shell-Exec und benutzerausgelöste Host-Änderungen. Die Instanz mit dem Prober-Lease löscht Einträge stündlich nach `audit_retention_days`. Das Bus-Event `docker_manager:container:action` enthält zusätzlich `actor`.

---

## Events

| Richtung | Topic | Beschreibung |
|---|---|---|
| subscribe | `docker_manager:hosts:set` | Komplette Host-Liste setzen |
| subscribe | `docker_manager:host:upsert` | Einzelnen Host anlegen oder aktualisieren |
| subscribe | `docker_manager:host:delete` | Einzelnen Host entfernen |
| subscribe | `docker_manager:hosts:get` | Aktuelle Host-Liste anfordern |
| subscribe | `iac:inventory_updated` | Docker-Hosts aus dem IaC-Inventar übernehmen (Kopien mit `_origin` werden ignoriert) |
| subscribe | `plugin:settings_changed` | Einstellungen neu lesen |
| subscribe/emit | `docker_manager:hosts_updated` | Cluster-Event nach jeder Host-Änderung (`{"reason", "ids"}`, `cluster=True`) |
| emit | `docker_manager:hosts:updated` | Host-Liste hat sich geändert (lokal, komplette Liste) |
| emit | `docker_manager:hosts:state` | Aktueller Host-Status (Polling-Ergebnis) |
| emit | `docker_manager:container:action` | Container-Aktion wurde ausgeführt (enthält `actor`) |

---

## Konfiguration & Einstellungen

Das Plugin besitzt keine externen Umgebungsvariablen. Die Host-Konfiguration erfolgt über die Plugin-Einstellungsseite, die REST-API oder automatisch aus dem IaC-Inventar.

| Einstellung | Standardwert | Beschreibung |
|---|---|---|
| `probe_interval_seconds` | `60` | Intervall der Erreichbarkeitsprüfung (Minimum 10 s) |
| `audit_retention_days` | `90` | Aufbewahrung des Audit-Trails in Tagen (`0` = unbegrenzt); Bereinigung stündlich durch die Prober-Instanz |

**Wichtig:** `auto_enable_on_install=False` — das Plugin muss nach der Installation manuell aktiviert werden, da ohne mindestens einen konfigurierten Host keine sinnvolle Funktion möglich ist.

### Host-Felder

| Feld | Typ | Standardwert | Beschreibung |
|---|---|---|---|
| `name` | string | — | Anzeigename des Hosts |
| `ip` | string | — | IP-Adresse oder Hostname des Docker-Proxys |
| `port` | integer | `2375` | TCP-Port des Docker-Proxys |
| `scheme` | string | `http` | `http` oder `https` |
| `source` | string | `manual` | `manual` oder `iac` (nur lesend in der UI) |
| `ci_id` | string | — | Entity-Registry-UUID, falls bekannt |
| `tls_verify` | boolean | — | Serverzertifikat prüfen (nur `https`) |
| `tls_ca_path` | string | — | CA-Bundle-Datei auf dem Core; `null` entfernt sie |
| `tls_client_cert_pem` / `tls_client_key_pem` | string | — | Client-Zertifikat und Schlüssel (nur schreibend, werden in Vault gespeichert); die Antwort enthält nur `tls_client_auth` |
| `tls_client_auth` | boolean | — | `false` entfernt das gespeicherte Client-Zertifikat |
| `security` / `insecure` / `security_warning` | — | — | Nur lesend: `security{encrypted, server_verified, client_auth}`; bestehende Hosts bleiben Klartext-`http` und werden markiert |
| `reachable` / `checked_at` / `check_error` | — | — | Letztes Ergebnis der Erreichbarkeitsprüfung (nur lesend) |

---

## Sicherheitshinweise

Die Methode `_validate_host_target` in `app/logic/service.py` schützt gegen SSRF-Angriffe (Server-Side Request Forgery). Folgende Adresstypen werden **abgelehnt**:

- Link-local-Adressen (`169.254.0.0/16`, IPv6 link-local)
- Cloud-Metadaten-Adressen
- Multicast- und reservierte Adressbereiche
- numerische IPv4-Schreibweisen und IPv6-Formen, die IPv4 einbetten, sowie aufgelöste Adressen in diesen Bereichen

Jede Anfrage verbindet sich mit der geprüften IP (kein DNS-Rebinding-Fenster); Redirects werden nie verfolgt. Die Prüfung liegt seit 1.3.0 in `app/logic/targets.py`, TLS in `app/logic/tls.py`, der Audit-Trail in `app/logic/audit.py`.

Private (`10.0.0.0/8`, `192.168.0.0/16`, `172.16.0.0/12`) und Loopback-Adressen (`127.0.0.1`) sind **erlaubt**, da Docker-Proxys typischerweise im lokalen Netz oder auf dem Host selbst betrieben werden.

---

## Internationaliserung

Das Plugin registriert den i18n-Namespace `docker`. Übersetzungsdateien liegen unter `locales/docker.<locale>.json` und werden automatisch beim Laden in den Lyndrix-Katalog aufgenommen. Der React-Client lädt sie über den Standard-i18n-Endpunkt (`GET /api/i18n/{locale}?ns=docker`).

---

## Entwicklung & Tests

```bash
# Aus dem Plugin-Verzeichnis (lyndrix-plugin-docker-manager/)
pip install -r requirements-dev.txt

# Tests ausführen
pytest

# Typprüfung
mypy .

# Linter
ruff check .

# Formatter prüfen
black --check .
```

Die Service-Schicht (`app/logic/service.py`) und das API-Modul (`app/api.py`) sind unabhängig von einem laufenden Lyndrix-Core testbar. Für Lifecycle-Hook-Tests kann `ModuleContext` gemockt werden.

Die Cluster-Tests (`tests/test_cluster.py`) laufen gegen eine SQLite-In-Memory-Datenbank und brauchen den Core im Pfad (`core.services` wird in `tests/conftest.py` zuerst importiert) — am einfachsten im Container `lyndrix-core-dev` mit `PYTHONPATH=/app` und `LYNDRIX_CLUSTER_MODE=single`.
