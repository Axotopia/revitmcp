import os
import json
import uuid
import re
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from bridge import RevitBridge, RevitBridgeError, get_governed_bridge
from governor import PayloadViolation
from tools import (
    calculate_septic_setback,
    calculate_window_to_wall_ratio,
    calculate_energy_envelope_compliance,
    query_building_code,
    get_parameter_float,
    normalize_mcp_result,
)
from schemas import EnergyExtraction
from agent import react_agent, llm

# Load .env file
load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:35b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
API_PORT = int(os.getenv("API_PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="Axoworks Companion", version="3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., description="User query or command")
    thread_id: str = Field(default="", description="Conversation ID.")

class ChatResponse(BaseModel):
    response: str
    thread_id: str
    tool_calls_made: list[dict]

class AuditRequest(BaseModel):
    audit_type: str = Field(..., pattern="^(septic|energy|wwr|model_health)$")
    jurisdiction: str = "default"
    thread_id: str = ""

class AuditResponse(BaseModel):
    audit_type: str
    thread_id: str
    compliant: bool
    calculations: dict
    narrative: str
    steps: list[dict]


# -----------------------------------------------------------------------------
# /health & /debug
# -----------------------------------------------------------------------------

@app.get("/health")
async def health():
    bridge = get_governed_bridge()
    found = False
    count = None
    try:
        tools = await bridge.list_mcp_tools()
        found = True
        count = len(tools) if isinstance(tools, list) else 0
    except Exception:
        pass
    return {"status": "ok" if found else "degraded", "revit_pipe_found": found, "revit_tools_count": count}

@app.post("/debug/revit_tools")
async def debug_revit_tools():
    bridge = get_governed_bridge()
    try:
        tools = await bridge.list_mcp_tools()
        return {"tools": tools}
    except RevitBridgeError as exc:
        return {"error": str(exc)}

@app.get("/governor/status")
async def governor_status():
    """Exposes governor state: active requests, cache entries, and stats."""
    bridge = get_governed_bridge()
    return bridge.get_status()


# -----------------------------------------------------------------------------
# /chat  -> Flexible ReAct Agent
# -----------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    thread_id = request.thread_id or str(uuid.uuid4())
    try:
        result = await react_agent.ainvoke(
            {"messages": [("user", request.message)]},
            {"configurable": {"thread_id": thread_id}},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    final_msg = result["messages"][-1]
    content = final_msg.content if hasattr(final_msg, "content") else str(final_msg)

    tool_calls = []
    for msg in result["messages"]:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "role": "assistant",
                    "tool": tc.get("name", tc.get("function", {}).get("name", "unknown")),
                    "args": tc.get("args", tc.get("function", {}).get("arguments", {})),
                })
        elif getattr(msg, "type", None) == "tool":
            tool_calls.append({
                "role": "tool",
                "tool": getattr(msg, "name", "unknown"),
                "result_preview": str(getattr(msg, "content", ""))[:300],
            })

    return ChatResponse(response=content, thread_id=thread_id, tool_calls_made=tool_calls)


# -----------------------------------------------------------------------------
# /audit -> Deterministic Compliance with Agentic Fallback
# -----------------------------------------------------------------------------

def _extract_json_from_agent_output(text: str) -> dict:
    if not text:
        raise ValueError("Empty text")
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No JSON found in: {text[:200]}")


async def _agentic_extract_energy(thread_id: str, w_samp: list, r_samp: list, win_samp: list) -> EnergyExtraction:
    audit_thread = f"audit_energy_{thread_id}_{uuid.uuid4()}"
    isolated = create_react_agent(model=llm, tools=[], checkpointer=MemorySaver())
    system = (
        "You are a data extraction parser. Given raw Revit JSON samples, "
        "identify the highest U-factors and SHGC. Return ONLY JSON with keys: "
        "wall_max_u, roof_max_u, window_max_u, window_max_shgc. Use null for missing values."
    )
    user = (
        f"Walls sample: {json.dumps(w_samp[:3])}\n"
        f"Roofs sample: {json.dumps(r_samp[:3])}\n"
        f"Windows sample: {json.dumps(win_samp[:3])}"
    )
    try:
        res = await isolated.ainvoke(
            {"messages": [("system", system), ("user", user)]},
            {"configurable": {"thread_id": audit_thread}},
        )
    except Exception:
        return EnergyExtraction(still_missing=["wall_max_u", "roof_max_u", "window_max_u", "window_max_shgc"])

    last = res["messages"][-1].content if hasattr(res["messages"][-1], "content") else str(res["messages"][-1])

    structured = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, temperature=0.0).with_structured_output(EnergyExtraction)
    chain = ChatPromptTemplate.from_messages([
        ("system", "Parse the following raw auditor text into the exact schema. Use null for missing values."),
        ("human", "{text}"),
    ]) | structured

    try:
        return await chain.ainvoke({"text": last})
    except Exception:
        try:
            data = _extract_json_from_agent_output(last)
            return EnergyExtraction(**data)
        except Exception:
            return EnergyExtraction(still_missing=["wall_max_u", "roof_max_u", "window_max_u", "window_max_shgc"])


@app.post("/audit", response_model=AuditResponse)
async def audit_endpoint(request: AuditRequest):
    thread_id = request.thread_id or str(uuid.uuid4())
    bridge = get_governed_bridge()
    steps = []

    try:
        if request.audit_type == "septic":
            t_raw = await bridge.run_mcp_tool("get_elements_by_category", {"category": "Plumbing Fixtures", "include_geometry": True})
            l_raw = await bridge.run_mcp_tool("get_elements_by_category", {"category": "Property Lines", "include_geometry": True})
            t_list, l_list = normalize_mcp_result(t_raw), normalize_mcp_result(l_raw)
            steps.append({"step": "extract", "tanks_count": len(t_list)})

            codes = query_building_code("septic setback gravity", request.jurisdiction)
            req = 50.0
            for c in codes:
                if "100" in c.get("text", ""):
                    req = 100.0
            steps.append({"step": "code", "sources": [c.get("source") for c in codes]})

            calc = json.loads(calculate_septic_setback(json.dumps(t_list), json.dumps(l_list), req))
            compl = calc.get("pass", False)
            nar = ChatOllama(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, temperature=0.0).invoke(
                f"Septic audit complete. Results: {json.dumps(calc)}. Jurisdiction: {request.jurisdiction}. One-paragraph pass/fail summary."
            ).content
            return AuditResponse(audit_type="septic", thread_id=thread_id, compliant=compl, calculations=calc, narrative=nar, steps=steps)

        elif request.audit_type == "energy":
            w_raw = await bridge.run_mcp_tool("get_elements_by_category", {"category": "Walls"})
            r_raw = await bridge.run_mcp_tool("get_elements_by_category", {"category": "Roofs"})
            win_raw = await bridge.run_mcp_tool("get_elements_by_category", {"category": "Windows"})
            w_list, r_list, win_list = normalize_mcp_result(w_raw), normalize_mcp_result(r_raw), normalize_mcp_result(win_raw)
            steps.append({"step": "initial_extract", "counts": {"walls": len(w_list), "roofs": len(r_list), "windows": len(win_list)}})

            w_u = max((v for v in (get_parameter_float(x, ["U-Factor", "u_factor", "Thermal Properties - U-Factor"]) for x in w_list) if v is not None), default=None)
            r_u = max((v for v in (get_parameter_float(x, ["U-Factor", "u_factor"]) for x in r_list) if v is not None), default=None)
            win_u = max((v for v in (get_parameter_float(x, ["U-Factor", "u_factor"]) for x in win_list) if v is not None), default=None)
            win_sh = max((v for v in (get_parameter_float(x, ["SHGC", "shgc", "Solar Heat Gain Coefficient"]) for x in win_list) if v is not None), default=None)

            missing = [k for k, v in {"wall_max_u": w_u, "roof_max_u": r_u, "window_max_u": win_u, "window_max_shgc": win_sh}.items() if v is None]

            if missing:
                extracted = await _agentic_extract_energy(thread_id, w_list, r_list, win_list)
                if w_u is None and extracted.wall_max_u is not None:
                    w_u = extracted.wall_max_u
                if r_u is None and extracted.roof_max_u is not None:
                    r_u = extracted.roof_max_u
                if win_u is None and extracted.window_max_u is not None:
                    win_u = extracted.window_max_u
                if win_sh is None and extracted.window_max_shgc is not None:
                    win_sh = extracted.window_max_shgc
                missing = [k for k, v in {"wall_max_u": w_u, "roof_max_u": r_u, "window_max_u": win_u, "window_max_shgc": win_sh}.items() if v is None]
                steps.append({"step": "agentic_fallback", "resolved": [k for k in ["wall_max_u", "roof_max_u", "window_max_u", "window_max_shgc"] if k not in missing], "still_missing": missing})

            if missing:
                raise HTTPException(status_code=422, detail=f"Energy audit incomplete. Missing parameters: {missing}")

            calc = json.loads(calculate_energy_envelope_compliance(wall_u_factor=w_u, roof_u_factor=r_u, window_u_factor=win_u, window_shgc=win_sh))
            compl = calc.get("pass", False)
            nar = f"Energy envelope audit complete. Pass={compl}."
            return AuditResponse(audit_type="energy", thread_id=thread_id, compliant=compl, calculations=calc, narrative=nar, steps=steps)

        elif request.audit_type == "wwr":
            w_raw = await bridge.run_mcp_tool("get_elements_by_category", {"category": "Walls", "include_geometry": True})
            win_raw = await bridge.run_mcp_tool("get_elements_by_category", {"category": "Windows", "include_geometry": True})
            w_list, win_list = normalize_mcp_result(w_raw), normalize_mcp_result(win_raw)
            steps.append({"step": "extract", "walls_count": len(w_list)})

            calc = json.loads(calculate_window_to_wall_ratio(json.dumps(w_list), json.dumps(win_list)))
            compl = calc.get("pass", False)
            nar = f"WWR audit complete. Pass={compl}."
            return AuditResponse(audit_type="wwr", thread_id=thread_id, compliant=compl, calculations=calc, narrative=nar, steps=steps)

        else:
            raise HTTPException(status_code=400, detail="Unsupported audit type")

    except PayloadViolation as pv:
        raise HTTPException(status_code=422, detail=pv.message)
    except RevitBridgeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audit failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=API_PORT)
