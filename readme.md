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

This application serves as a high-performance Python middleware layer, leveraging modular infrastructure to ensure stability and speed.

*   **Revit Bridge (Internal):** Uses **JSON-RPC 2.0 over NDJSON** (Newline-Delimited JSON) to communicate with the Revit MCP server via Windows Named Pipes. This ensures a low-latency, thread-safe connection to the BIM environment.
*   **API Layer (External):** Exposes a **REST-based JSON API** (FastAPI) to external agents and orchestrators like AnythingLLM.
*   **Orchestration Backbone:** By using **AnythingLLM**, we leverage a pre-built ecosystem for Local LLMs, Vector Databases, and Agentic workflows. This allows the Axoworks engine to function as a specialized "skill" or "toolset" within a larger AI brain.

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
   *(Note: You can configure the exact model used via the `.env` file).*

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

### Avoiding Port Collisions
If you deploy this to a firm where port `8000` is already in use by another application:
* Open the `.env` file and change `API_PORT=8000` to a free port (e.g., `8080`).
* The engine will safely spin up on the new port.

### Centralized AI Infrastructure
If a firm prefers to host their LLMs on a centralized server rather than running 23GB models on individual laptops:
* Open the `.env` file and change `OLLAMA_BASE_URL` from `http://localhost:11434` to the IP address of their centralized server (e.g., `http://192.168.1.100:11434`).
* The engine will automatically route all heavy AI reasoning to the server while maintaining the local connection to the user's Revit pipe.

---

## 🚀 Running the Engine

1. **Activate the Virtual Environment**: `.\venv\Scripts\activate`
2. **Start the FastAPI Server**:
   ```bash
   python main.py
   ```
The server will spin up on **`http://127.0.0.1:8000`** (or whichever port you set in `.env`).

---

## 🧪 Testing the API

The backend currently exposes the following endpoints (which can be consumed by AnythingLLM or tested via `curl`):

### 1. `/chat` (Exploratory Mode)
* **Purpose:** Runs a LangGraph ReAct agent that dynamically selects Revit tools to answer arbitrary questions about the BIM model.

### 2. `/audit` (Deterministic Mode)
* **Purpose:** Runs strict, deterministic Python logic for specific compliance checks (e.g., Septic setbacks, Energy code compliance) bypassing LLM math errors, using the LLM only for final narrative summaries.

---

## 🧠 AnythingLLM Integration: The Intelligence Backbone

We have intentionally chosen **AnythingLLM** as our frontend and orchestration layer. This allows us to deliver a functional MVP without reinventing the wheel of local LLM management or vector storage. By leveraging AnythingLLM's existing infrastructure, we can focus 100% on the **Intent Engineering** of the Revit-Python bridge.

### 1. Configure Agent Skills (Custom Tools)
We provide pre-configured custom skills in the `anythingllm` folder of this repository (`axo-revit-chat` and `axo-revit-audit`).
* Navigate to your AnythingLLM plugin folder: `%APPDATA%\anythingllm-desktop\storage\plugins\agent-skills` (e.g., `C:\Users\<YourUsername>\AppData\Roaming\anythingllm-desktop\storage\plugins\agent-skills`).
* Copy the skill folders from the `anythingllm` directory of this repo into that `agent-skills` folder.
* Restart AnythingLLM, and toggle these skills **ON** in your Workspace Settings.

### 2. Configure Native MCP Server
AnythingLLM can also connect directly to the Autodesk Revit server.
* Navigate to `%APPDATA%\anythingllm-desktop\storage\plugins`.
* Copy the `anythingllm_mcp_servers.json` file from this repo into that directory.
* **Verify Path:** Ensure the path in that JSON file exactly matches where the Revit MCP Server is installed on your machine (Default: `C:\Program Files\Autodesk\Revit 2027 MCP Server Read-Tools Technical Preview\Autodesk.RevitMcpServer.Stdio.exe`).

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

## RULES

- Never skip Step 1. `revitInstanceId: 0`, `1`, or any hardcoded value will return empty results.
- Never omit `"searchScope": "AllViews"` from `query_model`.
- If a query returns an empty list, do NOT retry with different categories. Instead, verify the `revitInstanceId` is correct and that `"searchScope": "AllViews"` is present.
- For Levels use category `"OST_Levels"`, for Walls use `"OST_Walls"`, for Rooms use `"OST_Rooms"`.
- Always present results in a readable, structured format — never dump raw JSON to the user.
```

**Why this is necessary:** The Autodesk Revit MCP Server defaults `searchScope` to the **active view** when the parameter is omitted. Since elements like Levels exist across all views, omitting `"AllViews"` causes the server to return an empty array even when the model is fully loaded. This is not a bug — it is expected API behavior that the agent must be explicitly instructed to handle.

---

### 4. Why AnythingLLM?
*   **Local RAG:** Instantly query BIM data against localized PDF building codes and zoning ordinances.
*   **Agentic Workflows:** Multi-step reasoning for complex compliance checks (e.g., "Find all walls, check their fire rating against the IBC documents in the vector store, and report failures").
*   **Privacy:** Keep all BIM data and firm-specific documents on your local network.

---

## ⚖️ Legal Disclaimer & Liability

**Important Notice:** The Axoworks Logic Engine is an experimental, AI-powered QA/QC reference tool. 

* **Not Professional Advice:** This software does not constitute professional architectural, engineering, or legal advice. It is not a substitute for the judgment of a licensed Architect or Engineer of Record.
* **No Guarantee of Compliance:** While the deterministic engine is designed to accurately apply strict mathematical formulas to Revit data, building codes are subject to localized interpretations, exceptions, and updates. The results of any `/audit` (e.g., septic setbacks, energy envelopes, WWR) must be independently verified by a qualified professional.
* **Use at Your Own Risk:** By using this software, you agree that the authors, Axoworks, and contributors are not liable for any code violations, construction defects, damages, or financial losses resulting from the use of (or reliance upon) this software.

See the `LICENSE` file for full warranty and liability details.
