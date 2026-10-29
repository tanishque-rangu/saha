# Saha (सह) — Persistent, Drift-Resilient Personal AI

[![Saha CI](https://github.com/OWNER/REPOSITORY/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPOSITORY/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-emerald.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![Nebius Token Factory](https://img.shields.io/badge/Nebius-Token%20Factory-purple.svg)](https://tokenfactory.nebius.com/)
[![NVIDIA Nemotron](https://img.shields.io/badge/NVIDIA-Nemotron-76b900.svg)](https://build.nvidia.com/)

> **"Your work should never have to start over just because your AI does."**

Saha is an autonomous, state-aware personal AI assistant built for the **Nebius x NVIDIA Global AI Hackathon** (Personal AI Track). 

Unlike chat assistants that lose context or reset their state between sessions, Saha maintains an **append-only ledger** of multi-step task state in SQLite. It executes tasks using strictly sandboxed tools, verifies post-conditions against physical reality using deterministic code probes (never LLM hallucinations), and reconciles reality divergence (drift) without starting over.

---

## Architecture

```mermaid
flowchart TD
    User([User / Browser / CLI]) -->|Task Goal / Control| API[FastAPI Server :8080]
    
    subgraph Core Engine
        API --> Ledger[(Append-Only Ledger\nSQLite WAL)]
        API --> Executor[Step Executor]
        API --> Reconciler[Reality Reconciler]
    end

    subgraph Nebius Token Factory Models
        Executor -.->|Plan & Replan| PLANNER[Nemotron-3-Ultra-550B]
        Executor -.->|Execute & Generate| WORKER[nemotron-3-super-120b]
        Reconciler -.->|Audit Drift Fact| FAST[Nemotron-3-Nano-30B]
    end

    subgraph Sandboxed Execution
        Executor --> Tools[Restricted Sandbox]
        Tools -->|Native FS Ops| Files[(Workspace Files)]
        Tools -->|Scrubbed Env| PySub[python script.py]
        Tools -->|SSRF Guarded| HTTP[HTTP GET Probes]
    end

    subgraph Deterministic Verification
        Files -.-> Probes{Code Probes}
        HTTP -.-> Probes
        PySub -.-> Probes
        Probes -->|Pass / Fail| Ledger
    end

    Reconciler -->|Audit Divergence| Probes
```

---

## Model Specialization (Nebius Token Factory)

All model inference routes strictly through `backend/llm.py` via the OpenAI Python SDK pointing to **Nebius Token Factory** (`https://api.tokenfactory.nebius.com/v1/`).

| Role | Nemotron Model Identifier | Specialization | Avg Latency* | Real Token Telemetry* |
| :--- | :--- | :--- | :--- | :--- |
| **PLANNER** | `nvidia/Nemotron-3-Ultra-550b-a55b` | Decomposes high-level goals into DAG steps with dependencies (`depends_on`), tool payloads, and deterministic probe assertions. Replans on reality drift. | **2,817 ms** | 25,472 prompt / 9,715 completion |
| **WORKER** | `nvidia/nemotron-3-super-120b-a12b` | Generates verified file contents, analysis scripts, and performs synthetic workflows inside the task workspace sandbox. | **2,430 ms** | 3,696 prompt / 8,872 completion |
| **FAST** | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Synthesizes a factual, 1-sentence drift explanation from structured probe failure data without guessing causes. Runs non-blocking in background. | **5,817 ms** | 827 prompt / 2,004 completion |

*\*Empirical averages recorded directly from real API calls in `llm_calls` SQLite telemetry table during development.*

---

## Key Features

1. **Immutable Append-Only State Machine**:
   - Steps transition strictly: `pending` → `running` → `done` → `verified`.
   - If a step fails physical verification, it is marked `drifted`.
   - **History is preserved forever**: drifted steps are **never** overwritten or deleted; they point to their repair step via `resolved_by`. Dependent steps transition to `superseded` and point to replacement steps via `superseded_by`.
   - A task is complete only when every non-superseded step is `verified`, and every `drifted` step has a verified `resolved_by` step.

2. **Deterministic Reality Probes (Zero LLM Hallucination)**:
   - Verification probes are 100% deterministic code: SHA-256 digests, file existence and size checks, command exit codes, and HTTP status codes.
   - The LLM never evaluates its own success.

3. **Re-Execution Repair Semantics**:
   - On drift reconciliation, Saha **re-executes the repair step** in the workspace environment rather than attempting a byte-exact file restoration. This guarantees the reconstructed state is alive, reproducible, and verifiable.

4. **Non-Blocking Drift Explanation**:
   - Reconciling reality records the deterministic probe failure reason immediately.
   - The FAST Nano-30B model generates a user-friendly 1-sentence audit summary asynchronously in a background thread (with a 15-second timeout fallback to deterministic reason).

---

## Quickstart

### 1. Prerequisites
- Python 3.11+
- A Nebius Token Factory API key (`NEBIUS_API_KEY`)

### 2. Setup Virtual Environment
```bash
git clone https://github.com/OWNER/saha.git
cd saha

python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 3. Configure Environment
Create `.env` in the project root (never commit this file):
```env
NEBIUS_API_KEY=your_nebius_api_key_here
SAHA_PUBLIC_MODE=false
SAHA_DEMO_MODE=true
```

### 4. Run the API Server & Web UI
```bash
uvicorn backend.api:app --host 127.0.0.1 --port 8000
```
Open your browser to `http://127.0.0.1:8000`.

### 5. Run via CLI
```bash
# Execute directly through CLI engine
python -m cli.saha "Create two small data files with facts about Hyderabad, combine them into a summary, and check example.com"
```

### 6. Run Unit & Regression Tests (Offline / No Key Needed)
The entire test suite runs offline without calling the real API or requiring a `NEBIUS_API_KEY`:
```bash
pytest -v
```

---

## Live Demonstration Walkthrough

You can test Saha's drift resilience in 4 simple steps:

1. **Create a Task**: In the Web UI, click the example chip: *"Hyderabad facts summary & reachability check"*, then click **Plan & Create**.
2. **Execute Task**: Click **Run Task**. Saha invokes the `WORKER` model to create `facts1.txt`, `facts2.txt`, combine them into `summary.txt`, and check external HTTP reachability. All steps turn `[VERIFIED]`.
3. **Simulate Reality Drift ("Sabotage")**: Click **Simulate Drift**. The server deletes `facts1.txt` directly from the task workspace on disk without touching the database ledger.
4. **Resume & Reconcile**: Click **Resume & Reconcile**:
   - Saha re-probes physical reality: `facts1.txt` probe fails.
   - Step 1 is marked `[DRIFTED]` (preserved in ledger).
   - Step 3 (which depends on Step 1) is marked `[SUPERSEDED]`.
   - Step 2 and Step 4 remain `[VERIFIED]`.
   - FAST model produces a 1-sentence explanation of what diverged.
   - PLANNER model appends Step 5 (repair for Step 1) and Step 6 (replacement for Step 3).
   - WORKER re-executes pending repair steps and deterministic probes confirm reality. Task reaches `COMPLETE`.

---

## Security Architecture

Saha is engineered with defensive sandbox boundaries:

1. **Path Traversal Sandboxing**:
   - All workspace paths are resolved strictly against `workspace/<task_id>`.
   - Rejects `..`, absolute paths (`/etc/passwd`, `C:\Windows`), UNC paths (`\\server\share`), drive-relative paths (`C:foo`), and symlink escapes.
2. **Subprocess Isolation**:
   - Removed all `shell=True` invocation. No `cmd.exe`, `powershell.exe`, or `bash` execution.
   - Only native Python implementations for `ls`, `cat`, `head`, `tail`, `grep`, and `echo`.
   - Single isolated execution capability: `python script.py` strictly targeting scripts within the task workspace sandbox.
   - Arguments containing shell metacharacters (`;`, `|`, `&`, `>`, `<`, `$`, `` ` ``) are rejected.
3. **Environment Scrubbing**:
   - Child execution processes receive a scrubbed environment containing only bare minimum system paths (`SYSTEMROOT`, `PATH`, `TEMP`).
   - `NEBIUS_API_KEY` and all `.env` secrets are strictly stripped before spawning any child process.
4. **SSRF Defense (`http_get`)**:
   - Rejects IPv4 private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.0/8`), link-local metadata endpoints (`169.254.0.0/16`), and IPv6 equivalents (`::1`, `fe80::/10`).
   - Re-evaluates target IP on every HTTP redirect.
   - Size capped at 64 KB with a strict 10-second timeout.
5. **Public Mode Safeguards**:
   - Max goal length capped at 300 characters.
   - In-memory IP rate limiter (10 task creations per hour per IP).
   - Domain allowlist restriction (`allowed_domains`).
   - Generic 500 responses with zero path leakage or stack traces.
6. **Honest Limitations**:
   - **DNS Rebinding**: While IP resolution is checked pre-flight, a malicious DNS server altering records between DNS resolution and HTTP socket connection (TOCTOU) remains an inherent limitation of application-level validation. Production hardening would mandate network-layer egress proxies or patched transport adapters.

---

## Deploy

Saha packages into a self-contained, unprivileged Docker container designed to run on standard container hosting providers (such as Docker hosts, Render, or Railway).

### Docker Image Build & Run

```bash
# Build the production container image
docker build -t saha:latest .

# Run with persistent volume mounted to /data
docker run -d \
  --name saha \
  -p 8080:8080 \
  -e NEBIUS_API_KEY="your-nebius-api-key" \
  -e SAHA_PUBLIC_MODE=true \
  -e SAHA_DEMO_MODE=true \
  -e SAHA_REQUIRE_PERSISTENT=true \
  -v saha_data:/data \
  saha:latest
```

### Required & Supported Environment Variables

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `NEBIUS_API_KEY` | **Yes** | — | Nebius Token Factory API key for PLANNER, WORKER, and FAST models. |
| `SAHA_PUBLIC_MODE` | No | `true` (in Docker) | Enables multi-tenant private owner spaces, token hashing (`X-Saha-Owner`), and disables `run_shell`. |
| `SAHA_DEMO_MODE` | No | `true` (in Docker) | Enables live reality drift simulation endpoints (`/tasks/{id}/sabotage`). |
| `SAHA_REQUIRE_PERSISTENT`| No | `false` | When `true`, startup checks enforce that `/data` resides on a persistent, non-ephemeral volume. |
| `SAHA_DB_PATH` | No | `/data/saha.db` | Absolute path to the SQLite ledger database. |
| `SAHA_WORKSPACE_ROOT` | No | `/data/workspace` | Absolute path to the sandboxed workspace root directory. |
| `PORT` | No | `8080` | Port for the HTTP server to bind to (automatically handled by `docker-entrypoint.sh`). |

### Persistent Storage at `/data` (Must Persist)

Saha's core capability is an append-only state ledger and task file recovery across restarts. The `/data` volume **must persist across container restarts and redeployments**:
- `saha.db` contains tasks, immutable steps, approval audit records, and policy definitions.
- `workspace/` contains tenant files and verifiable artifacts.

If `/data` is ephemeral, task history and workspace states are lost upon container recreation. When running as root, `docker-entrypoint.sh` automatically creates `/data/workspace`, sets ownership to UID `10001:10001`, and drops privileges to `appuser` via `gosu`.

### Dynamic PORT Handling

Container platforms like Render and Railway dynamically assign an external HTTP port via the `$PORT` environment variable (e.g., `PORT=10000`). `docker-entrypoint.sh` inspects `$PORT` at startup and binds `uvicorn` and container healthchecks accordingly.

### Short Notes for Railway and Render

- **Render**:
  - The repository includes an optional [render.yaml](render.yaml) blueprint configuring a `starter` web service, Docker runtime, healthcheck path `/health`, and a 1 GB persistent disk at `/data`.
  - In manual setup, select **Web Service -> Docker**, attach a persistent disk mounted at `/data`, and set `NEBIUS_API_KEY` in environment secrets.
- **Railway**:
  - Deploy from GitHub repository using the Dockerfile.
  - Add a **Persistent Volume** mounted at `/data`.
  - Add `NEBIUS_API_KEY` under Service Variables. Railway automatically injects `$PORT`.
- **Tested Verification**:
  - Local container builds, entrypoint privilege drops (UID `10001`), root volume permissions adjustments, dynamic `$PORT` rewriting, and GitHub Actions CI smoke testing are tested and verified. Cloud deployments on third-party PaaS platforms (Railway/Render) depend on their respective persistent disk provisioning SLAs.

---

## Interruption Recovery & Mutation Deduplication

Saha is engineered to survive unexpected process terminations (crashes, power loss, SIGKILL) without data corruption or duplicate external side effects:

- **Idempotent Local Filesystem Steps**: Interrupted local file writes are safely re-executed since writes to the isolated workspace are deterministic and overwrite-safe. Completed steps verified by physical SHA-256 probes are never executed twice.
- **Owner-Controlled Idempotency Trust Boundary**: Planner-generated step data, arguments, and supplied headers are untrusted LLM outputs and **cannot** authorize automatic retry of an ambiguous external mutation. Idempotency support is trusted only when established by owner-controlled configuration (policy.trusted_idempotency_domains) or environment settings (SAHA_TRUSTED_IDEMPOTENCY_DOMAINS).
- **Trusted HTTP Targets (Stable Idempotency Key)**: When an external HTTP mutation (POST, PUT, DELETE, PATCH) targets an owner-trusted domain, Saha generates, transactionally persists to SQLite (step.args_json), and reuses a stable Idempotency-Key across retries. The remote service deduplicates without applying duplicate mutations.
- **Ambiguous & Untrusted Mutations (Conservative Halting)**: If an external mutation on an untrusted or non-idempotent target is interrupted before an HTTP response is recorded, its remote outcome is unknown. Saha refuses to blindly retry it. It records an explicit locked state and creates an owner approval request with a clear warning explaining that the remote outcome is unknown, that approving will re-dispatch (retry) the mutation, and that denying will fail the step without network calls.
- **Strict Policy Enforcement**: Resumed actions strictly evaluate owner security policies (allowed tools, domain allowlists, path restrictions) before every network dispatch.

### Real vs. Mocked Telemetry Accounting
- **Real Systems**: SQLite WAL persistence (saha.db), step execution state machine, filesystem isolation, SHA-256 verification probes, policy engine, approval ledger, and HTTP client (httpx).
- **Mocked Components**: In automated tests and demo scripts, remote external HTTP mutation targets are mocked in-process to deterministically simulate mid-flight socket drops and SIGKILL interrupts without reliance on third-party API availability. LLM calls are mocked in offline test runs.

---

## Limitations & Roadmap

We are candid about Saha's current scope and future trajectory:
- **Non-Idempotent External APIs without Status Probes**: If a third-party remote service does not support idempotency keys and does not expose a queryable status/read endpoint, automated recovery cannot resolve whether an interrupted request was committed, requiring owner reconciliation.
- **Cross-Device Ledger Synchronization**: Current ledger is backed by local SQLite WAL. Cloud ledger sync (CRDTs / Postgres replication) across mobile and desktop nodes is planned.
- **Always-On Background Daemon**: Currently execution is session- or user-triggered. An autonomous heartbeat runner with cron scheduling and webhook triggers is future work.
- **Call Triage & Multi-Agent Workforce**: Saha currently focuses on single-agent planner/worker/auditor coordination. Hierarchical multi-agent delegation and autonomous call triage will be added in v2.0.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
