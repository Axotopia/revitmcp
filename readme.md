# Axoworks Revit MCP Logic Engine for AnythingLLM

> [!CAUTION]
> **Experimental MVP:** This project is a technical experiment in "Intent Engineering." It requires significant fine-tuning, prompt calibration, and domain-specific logic refinement before it can be considered production-ready.

---

## 🌪️ From Closed Loops to "Open Loop Chaos Audits"

The architectural industry is currently limited by "Closed Loop" AI assistants—tools like Autodesk Assistant that operate within the proprietary boundaries of a single software instance. While useful for basic tasks, they lack the firm-specific context, local zoning nuances, and jurisdictional RAG (Retrieval-Augmented Generation) data required for true professional auditing.

**The Axoworks Revit MCP Engine** is designed to break this loop. By bridging the gap between the **Autodesk Revit MCP (Model Context Protocol) Server** and **AnythingLLM**, we enable an **"Open Loop Chaos Audit."**

### Core Objectives:
1.  **Extract & Assemble:** Move beyond simple data retrieval. This engine focuses on translating raw Revit elements into semantically rich structures that an LLM can actually "reason" about.
2.  **RAG-Powered Compliance:** Query extracted model data against a firm’s proprietary knowledge base, local building codes, and site-specific documents stored in a vector database.
3.  **Intent Engineering:** Shift the developer's focus from "reinventing the wheel" (LLM hosting, vector storage, UI) to refining the Python-based logic and intent that drives the extraction process.

---

## 🏗️ Architecture & Protocols

This application serves as a high-performance Python MCP (Model Context Protocol) Proxy layer, bridging AnythingLLM with the Autodesk Revit server to ensure stability, translation, and speed.

*   **Revit Bridge (Internal):** Uses **JSON-RPC 2.0 over NDJSON** to communicate with the Autodesk Revit MCP server via Windows Named Pipes. This ensures a low-latency connection to the BIM environment.
*   **MCP Proxy (External):** Exposes a **native MCP Server via standard I/O (stdio)**, allowing AnythingLLM to connect to it directly as a first-class citizen, completely bypassing buggy "Agent Skills" plugins.
*   **Coordinate Translation Layer:** Silently intercepts MCP responses from Revit and translates Global Z coordinates (Internal Origin) to Project Z coordinates (Project Base Point), preventing LLM hallucination.
*   **Governance Middleware (Request Governor):** Intercepts all traffic between the MCP Proxy and the Revit pipe. It provides **Request Deduplication**, **Asynchronous Heartbeats** to prevent agent timeouts, and **Payload Auditing** to block dangerous queries before they reach Revit's main thread.
*   **Custom Audit Tools:** Injects deterministic logic tools (e.g., Septic Setbacks) directly into the MCP payload, sitting seamlessly alongside native Revit tools.

---

## 🛑 Prerequisites: Windows System Setup

Before setting up the engine, you need the core technologies installed on your Windows machine.

### 1. Install Python (Windows)
If you do not already have Python installed:
1. Go to the official Python website: [https://www.python.org/downloads/windows/](https://www.python.org/downloads/windows/)
2. Download the latest **Windows installer (64-bit)** for Python 3.10 or higher.
3. Run the installer.
4. **CRITICAL STEP:** At the very bottom of the first installation screen, you MUST check the box that says **"Add python.exe to PATH"** before clicking "Install Now".
5. Once installed, open your Command Prompt (or PowerShell) and type `python --version` to verify it installed correctly.

### 2. Install Ollama
1. Download and install Ollama from [https://ollama.com/](https://ollama.com/).
2. Open your terminal and pull the Qwen model to your local machine:
   ```bash
   ollama pull qwen3.6:35b
   ```
   *(Note: You will select and configure this model directly inside AnythingLLM's settings).*

---

## 🛠️ Installation & Virtual Environment Setup

To keep this project isolated and prevent dependency conflicts, we use a Python Virtual Environment (`venv`).

### 1. Create the Virtual Environment
Inside your project folder, create the isolated environment by running:
```bash
python -m venv venv
```

### 2. Activate the Virtual Environment
You must activate the environment every time you want to work on or run the engine.
* **Command Prompt:** `.\venv\Scripts\activate.bat`
* **PowerShell:** `.\venv\Scripts\Activate.ps1`
*(Note: If PowerShell gives you an "Execution Policy" error, run this command as Administrator first: `Set-ExecutionPolicy Unrestricted -Scope CurrentUser`, then try activating again).*

### 3. Install Dependencies
With the `(venv)` active, install the required packages:
```bash
pip install -r requirements.txt
```

---

## ⚙️ Configuration & Deployment

The engine uses a `.env` file to ensure smooth deployment across different firms and networks without breaking. Before running, copy the `.env.example` file (if provided) or create a `.env` file in the root directory.

### Pipe Auto-Discovery
Autodesk Revit generates dynamic Named Pipes (e.g., `\\.\pipe\revit-mcp-1a2b3c...`) that change every time the software starts. 
* You do **not** need to hardcode the exact pipe name. 
* The engine uses a robust PowerShell auto-discovery script to scan the host machine for any active pipe starting with the `REVIT_PIPE_PREFIX` defined in the `.env` file (default: `\\.\pipe\revit-mcp`). It will automatically connect.

### Bypassing API Ports
Because the system now runs as a native MCP standard I/O proxy, it does not bind to any network ports (e.g., `8000`). It uses stdin/stdout, completely eliminating port collisions on strict firm networks.


### Governance Layer Tuning
The governance layer prevents Revit deadlocks from rapid LLM retries. You can tune these in `.env`:
* `GOVERNOR_HEARTBEAT_THRESHOLD_S=25`: Seconds before sending a "still processing" response to prevent client timeout.
* `GOVERNOR_CACHE_TTL_S=10`: How long completed results stay in the deduplication cache.
* `GOVERNOR_DANGEROUS_CATEGORIES`: Comma-separated list of categories that require filters when geometry is requested (e.g., `Generic Models`).

---

## 🚀 Running the Engine

You **do not** need to manually start a server! Because this operates as a native MCP server, AnythingLLM automatically manages its lifecycle.

1. Once configured in AnythingLLM (see below), AnythingLLM will spawn `main_mcp.py` in the background automatically when you open a workspace.
2. The proxy will transparently connect to the Autodesk Revit Named Pipe, discover the native tools, inject its custom audit tools, and start proxying traffic.

---

## 🧪 Available Tools

The MCP Proxy dynamically exposes the following tools directly to your AnythingLLM agent:

### 1. Native Revit Tools (Pass-through)
* `query_model`, `get_element_data`, `zoom_to_elements`, etc. 
* **Key Feature:** All geometry coordinates (`boundingBox`, `geometry` points) returned by these tools are automatically intercepted and translated from Global Z to Project Z in transit.

### 2. Custom Audit Tools
* `axo_audit_septic`: Runs strict, deterministic Python logic for septic setback compliance.
* `axo_audit_energy`: (Placeholder) Energy envelope checks.
* `axo_audit_wwr`: (Placeholder) Window-to-Wall ratio compliance.

*(Note: These custom tools bypass LLM math errors by executing local calculations and only using the LLM for final narrative summaries).*

---

## 🛡️ Governance Layer: Main Thread Protection

Revit enforces **single-threaded API access**. All operations related to reading and updating the model are queued and executed sequentially on the main UI thread. AnythingLLM’s agent executor is asynchronous and "impatient"—if a complex Revit query takes longer than 30 seconds, the agent assumes failure and initiates rapid-fire retries. 

Without governance, these retries flood the Revit `ExternalEvent` queue and permanently deadlock the host. The Axoworks engine implements a triple-layer governor to decouple the asynchronous agent from the synchronous host:

1.  **Request Deduplication (The State Manager):** 
    The engine tracks the signature (method + parameters) of every in-flight request. If a duplicate retry arrives while the host is still processing, the governor silently coalesces the request and waits for the original task to finish instead of forwarding a second trigger to Revit.

2.  **Asynchronous Heartbeat (Timeout Mitigation):** 
    If a Revit task approaches the client timeout threshold (25 seconds), the governor intercepts. It sends a system-level response back to the LLM: *"Tool execution in progress. Host is processing complex geometry. Wait and do not retry."* The original task stays alive in the background, and its results are cached for the LLM's next poll.

3.  **Payload Auditing (Pre-validation):** 
    Strict validation rules block broad or dangerous queries (e.g., querying "Generic Models" with `include_geometry: true` without a filter) before they ever touch the Revit thread. This prevents "bad" requests from ever reaching the main thread's queue.

---

## 🧠 AnythingLLM Integration: The Intelligence Backbone

We have intentionally chosen **AnythingLLM** as our frontend and orchestration layer. This allows us to deliver a functional MVP without reinventing the wheel of local LLM management or vector storage. By leveraging AnythingLLM's existing infrastructure, we can focus 100% on the **Intent Engineering** of the Revit-Python bridge.

### 1. Configure the MCP Proxy
Instead of connecting AnythingLLM directly to the Autodesk `.exe`, we route it through our Python Proxy.

**Option A: Via AnythingLLM UI (Recommended)**
1. Open AnythingLLM and go to **Settings → Agent Configuration → MCP Servers**.
2. Click **Add New Server** and configure it as follows:
   - **Name:** `revit-2027`
   - **Type:** `command`
   - **Command:** Absolute path to your virtual environment's Python executable (e.g., `C:\Users\YourName\Documents\GitHub\revitmcp\venv\Scripts\python.exe`)
   - **Args:** Absolute path to `main_mcp.py` (e.g., `C:\Users\YourName\Documents\GitHub\revitmcp\main_mcp.py`)
3. Save and wait for AnythingLLM to discover the tools.

**Option B: Manual JSON Configuration**
1. Navigate to your AnythingLLM plugins folder (e.g., `%APPDATA%\AnythingLLMDesktop\storage\plugins` or equivalent).
2. Copy the `anythingllm_mcp_servers.json` file from the `anythingllm\mcp` directory of this repo into that directory.
3. **CRITICAL STEP:** Open the copied JSON file and edit the absolute paths. You **must** change `C:\path\to\your\revitmcp\...` to the actual location where you cloned this repository.
   - `command` should point to your `venv\Scripts\python.exe`
   - `args` should point to your `main_mcp.py`
4. Restart AnythingLLM. The new tools (`axo_audit_septic`, etc.) will appear natively in your agent's toolkit alongside the native Autodesk tools.

### 3. Configure the Workspace System Prompt

> [!IMPORTANT]
> This step is required for reliable results. Without it, LLM agents — especially smaller models — will omit critical query parameters and return empty results (e.g., `{ "levels": [] }`).

The Revit MCP tools require a **specific tool-call sequence** to return data correctly. Different LLMs infer this sequence inconsistently across new threads. A workspace system prompt enforces the correct behavior for every model, every time.

**How to apply:**
1. Open your AnythingLLM workspace.
2. Go to **Workspace Settings → Agent Configuration → System Prompt**.
3. Paste the prompt below and save. You do **not** need to restart the server.
4. Always start a **new thread** after updating the system prompt — existing threads won't inherit it.

**Copy this prompt exactly:**

```
You are a Revit BIM assistant with direct access to a live Revit 2027 model via the revit-2027 MCP tools.

## REQUIRED TOOL CALL SEQUENCE

**Step 1 — Discover the active Revit instance (ALWAYS first)**
Call `get_running_revit_instances` before any other tool. Extract the numeric `revitInstanceId` from the response. Never assume, guess, or reuse an ID from a previous message — always re-discover it.

**Step 2 — Query elements (ALWAYS include searchScope)**
When calling `query_model`, you MUST include `"searchScope": "AllViews"` in every call. Omitting this parameter causes the Revit API to search only the active view, which returns empty results for most element types including Levels, Walls, Rooms, and Sheets.

**Step 3 — Retrieve element details**
After `query_model` returns a list of element IDs, call `get_element_data` with those IDs to retrieve names, parameters, and properties. Never report raw element IDs as a final answer.

## VALID query_model PARAMETERS

Only these parameters are accepted by `query_model`. Never add any others:
- `revitInstanceId` (required, number) — from get_running_revit_instances
- `input.categories` (required, array of strings) — e.g. `["OST_Levels"]`
- `input.searchScope` (required, string) — ALWAYS set to `"AllViews"`
- `input.maxResults` (optional, number) — limit results returned
- `input.searchText` (optional, string) — filter by name

Do NOT add `elementInclusionMode`, `includeTypes`, `scope`, or any other parameter not listed above. The MCP server will reject unknown parameters with an error.

## RULES

- Never skip Step 1. `revitInstanceId: 0`, `1`, or any hardcoded value will return empty results.
- Never omit `"searchScope": "AllViews"` from `query_model`.
- **If `query_model` returns an error (not an empty list), STOP immediately.** Do not retry with different parameters, categories, or maxResults values. Report the exact error to the user and wait for instructions.
- If a query returns an empty list, do NOT retry with different categories. Instead, verify the `revitInstanceId` is correct and that `"searchScope": "AllViews"` is present.
- For Levels use category `"OST_Levels"`, for Walls use `"OST_Walls"`, for Roofs use `"OST_Roofs"`, for Rooms use `"OST_Rooms"`.
- Always present results in a readable, structured format — never dump raw JSON to the user.
```

**Why this is necessary:** The Autodesk Revit MCP Server defaults `searchScope` to the **active view** when the parameter is omitted. Since elements like Levels exist across all views, omitting `"AllViews"` causes the server to return an empty array even when the model is fully loaded. This is not a bug — it is expected API behavior that the agent must be explicitly instructed to handle.

---

### 4. Why AnythingLLM?
*   **Local RAG:** Instantly query BIM data against localized PDF building codes and zoning ordinances.
*   **Agentic Workflows:** Multi-step reasoning for complex compliance checks (e.g., "Find all walls, check their fire rating against the IBC documents in the vector store, and report failures").
*   **Privacy:** Keep all BIM data and firm-specific documents on your local network.

---

## ⚠️ Known Limitations & Bugs

### 1. Revit MCP Server Main Thread Deadlock

> [!CAUTION]
> The Autodesk Revit MCP Server relies on **single-threaded API access**. If an LLM agent issues multiple rapid-fire `query_model` calls that fail (e.g., due to invalid parameters or timeouts), the server's internal query processing thread can become permanently blocked — even though the pipe transport layer remains alive.

**Symptoms:**
* `tools/list` responds instantly, but all `query_model` calls hang indefinitely — including categories that previously worked (e.g., `OST_Levels`).
* AnythingLLM shows "Agent complete" with no results, or the thread appears stuck and eventually times out.

**Workaround:** Restart the Autodesk Revit MCP Server plugin or restart Revit entirely. There is currently no way to clear the deadlock without a restart.

**Root cause:** When an LLM agent retries a failing tool call in a tight loop (common in AnythingLLM's agent executor), the queued JSON-RPC requests pile up on the pipe. The MCP server's main thread gets blocked processing a failed request and never releases, starving all subsequent queries.

**Mitigation:** The **Governance Layer** (see section above) significantly reduces the risk of this deadlock by intercepting and coalescing duplicate retries and providing heartbeat keep-alives. However, a hard freeze can still occur if the Revit API itself hits an unrecoverable state during a geometry-heavy operation.

### 2. AnythingLLM Agent Retry Behavior Cannot Be Fully Controlled via System Prompt

AnythingLLM's agent executor has **built-in retry logic** at the platform level. Even if the workspace system prompt instructs the model to "stop immediately on error," the agent loop may still re-invoke the tool. This is an AnythingLLM platform limitation — the system prompt controls the LLM's reasoning but not the executor's retry policy. This retry behavior is the primary trigger for the main thread deadlock described above.

### 3. `OST_Roofs` Queries May Fail on Complex Models

Querying `OST_Roofs` via `query_model` has been observed to fail or time out on models with complex roof geometry, even when other categories (`OST_Levels`, `OST_Walls`) return successfully. This may be a limitation of the Autodesk MCP Server's Technical Preview. If roof queries consistently fail, try reducing `maxResults` to `1` to isolate whether the category is supported for the current model.

### 4. Inherent Latency and Governance Heartbeats

Because Revit processes all database queries sequentially on a single main thread, complex queries (like extracting parameter data for hundreds of elements) inherently take time. If multiple requests are sent, they are forced to queue up and wait their turn.

The Governance Layer does not *create* this latency, but it actively manages it. If a Revit task approaches the client timeout threshold, the governor intervenes by sending a heartbeat back to AnythingLLM to keep the connection alive. From the user's perspective, this means you may experience noticeable delays (sometimes 30-60+ seconds) while waiting for an agent to finish a complex task. This is expected behavior and a direct result of Revit's single-threaded architecture queuing the workload.

---

## ⚖️ Legal Disclaimer & Liability

**Important Notice:** The Axoworks Logic Engine is an experimental, AI-powered QA/QC reference tool. 

* **Not Professional Advice:** This software does not constitute professional architectural, engineering, or legal advice. It is not a substitute for the judgment of a licensed Architect or Engineer of Record.
* **No Guarantee of Compliance:** While the deterministic engine is designed to accurately apply strict mathematical formulas to Revit data, building codes are subject to localized interpretations, exceptions, and updates. The results of any `/audit` (e.g., septic setbacks, energy envelopes, WWR) must be independently verified by a qualified professional.
* **Use at Your Own Risk:** By using this software, you agree that the authors, Axoworks, and contributors are not liable for any code violations, construction defects, damages, or financial losses resulting from the use of (or reliance upon) this software.

See the `LICENSE` file for full warranty and liability details.
