# LowLlama Cross-Team Change Log

Date: November 7, 2026  
Baseline reviewed: `5ceefef`

This log records changes made outside Person 2's normal `agent/` ownership,
why they were needed, and how the combined project was verified. No source
file under `coordinator/` was modified.

## Commit map

- `57383f2` — `feat(agent): implement helper node lifecycle`
- `fb63a42` — `build: add FastAPI test client dependency`
- `50e1897` — `test: isolate coordinator mocks and discover integrations`
- `edc7d9a` — `docs: update coordinator status and launch target`

## Cross-team files changed

### `requirements.txt`

Added `httpx2`. The installed FastAPI/Starlette `TestClient` requires this
package and previously raised an exception before coordinator API tests could
run. The current environment resolved `httpx2==2.16.0`,
`httpcore2==2.16.0`, and `truststore==0.10.5`.

### `tests/test_coordinator_api.py`

Removed registry and invitation `MagicMock` modules inserted globally into
`sys.modules`. Replaced them with targeted `unittest.mock.patch` decorators on
the functions used by each test.

The previous global replacement could leak mocked Person 3 modules into other
tests and made results depend on import order. Targeted patches preserve API
test isolation while allowing real registry and invitation modules to run in
the same test process. Endpoint payloads and expected responses were not
changed.

### `tests/test_registry_invites.py`

Added a `unittest.TestCase` wrapper around the existing registry, invitation,
and coordinator-wiring checks. The functions previously passed when the file
was run directly but were silently skipped by `unittest discover`.

### `README.md`

Updated the stale coordinator status and changed the Uvicorn target from
`coordinator:app` to `coordinator.main:app`.

`coordinator:app` resolves to `coordinator/__init__.py`, which exposes only `/`
and `/nodes`. The active helper endpoints `/health`, `/register`, and
`/heartbeat`, along with `/invite` and `/join`, are on
`coordinator.main:app`.

## Person 2 implementation included

The helper-agent commit adds:

- Safe hostname, operating system, CPU, memory, and Python discovery.
- Validated environment configuration and persistent local node IDs.
- A coordinator-only HTTP client for registration and heartbeat requests.
- Explicit timeout, HTTP, authentication, connection, and response errors.
- Registration-before-heartbeat orchestration.
- Exponential retry backoff, recovery, structured logging, and clean shutdown.
- Agent documentation and 43 automated tests.

The default heartbeat interval is five seconds. Person 3's registry marks a
helper offline after ten seconds, so the previous ten-second helper interval
had no allowance for normal scheduling or network delay.

## Verification results

Coordinator, registry, and invitation suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Result: **13 tests passed**.

Helper-agent suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -v
```

Result: **43 tests passed**.

Dependency and syntax checks:

```powershell
uv --no-cache pip check --python .\.venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m compileall -q agent coordinator tests
```

Results: all 18 installed packages were compatible and all inspected Python
files compiled successfully.

A real local Uvicorn instance using `coordinator.main:app` was also exercised
with the real `agent.agent.create_agent()` path. The following passed:

- Coordinator health check.
- Environment configuration, hardware detection, and HTTP client creation.
- Helper registration and repeated heartbeats.
- No duplicate registration during heartbeat operation.
- Online node visibility without exposed credentials.
- Clean helper shutdown without a live agent thread.
- Invite creation and registration through `/join`.

## Remaining integration decisions

1. The helper currently registers through `/register`; it does not consume an
   invite or call `/join`. Both paths work separately, but the team must approve
   the official onboarding contract before merging them.
2. `/register` currently permits registration without an invitation.
3. The invite administrator token remains hardcoded in
   `coordinator/main.py`; the coordinator owner should move it to secret
   configuration before production use.
4. Registry entries and invitations are in memory and are lost on coordinator
   restart.
5. Invite creation can encode a missing master address when
   `LOWLLAMA_MASTER_IP` is unset.
6. Local HTTP integration passed, but physical Tailscale and two-computer
   llama.cpp RPC allocation still require teammate execution and runtime-log
   evidence.
