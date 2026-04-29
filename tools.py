import json
import math
import re
from typing import Any, List, Optional

from langchain_core.tools import tool

from bridge import RevitBridge, RevitBridgeError
from schemas import RevitElement, RevitPoint
from vector_store import query_code_db

bridge = RevitBridge()

# -----------------------------------------------------------------------------
# Parameter Normalization
# -----------------------------------------------------------------------------

def _coerce_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(val))
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None

def get_parameter_value(element: dict, aliases: List[str], default: Any = None) -> Any:
    if not isinstance(element, dict):
        return default
    for alias in aliases:
        if alias in element:
            return element[alias]
    params = element.get("parameters", {})
    if isinstance(params, dict):
        for alias in aliases:
            if alias in params:
                return params[alias]
    root_keys = {k.lower(): k for k in element.keys() if isinstance(k, str)}
    for alias in aliases:
        if alias.lower() in root_keys:
            return element[root_keys[alias.lower()]]
    if isinstance(params, dict):
        param_keys = {k.lower(): k for k in params.keys() if isinstance(k, str)}
        for alias in aliases:
            if alias.lower() in param_keys:
                return params[param_keys[alias.lower()]]
    return default

def get_parameter_float(element: dict, aliases: List[str], default: Optional[float] = None) -> Optional[float]:
    raw = get_parameter_value(element, aliases, None)
    if raw is None:
        return default
    return _coerce_float(raw)

def normalize_mcp_result(result: Any) -> List[dict]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if "elements" in result and isinstance(result["elements"], list):
            return result["elements"]
        if "result" in result:
            return normalize_mcp_result(result["result"])
        return [result]
    return []

def normalize_elements(raw_list: List[Any]) -> List[RevitElement]:
    out = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        geo = item.get("geometry", [])
        points = []
        if isinstance(geo, list):
            for pt in geo:
                if isinstance(pt, dict):
                    points.append(RevitPoint(
                        x=_coerce_float(pt.get("x", 0)) or 0.0,
                        y=_coerce_float(pt.get("y", 0)) or 0.0,
                        z=_coerce_float(pt.get("z")),
                    ))
        out.append(RevitElement(
            element_id=item.get("id", item.get("element_id", -1)),
            category=item.get("category"),
            parameters=item.get("parameters", {}),
            geometry=points if points else None,
        ))
    return out

# -----------------------------------------------------------------------------
# Revit Tools
# -----------------------------------------------------------------------------

@tool
async def revit_list_tools() -> str:
    """List all available tools from the Revit MCP server."""
    try:
        result = await bridge.list_mcp_tools()
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error: {e}"

@tool
async def revit_run_tool(tool_name: str, arguments_json: str = "{}") -> str:
    """Run a specific Revit MCP tool by name."""
    try:
        args = json.loads(arguments_json) if arguments_json else {}
        result = await bridge.run_mcp_tool(tool_name, args)
        return json.dumps(result, indent=2)
    except RevitBridgeError as e:
        return f"Revit Bridge Error: {e}"
    except Exception as e:
        return f"Unexpected error: {e}"

@tool
async def revit_get_elements_by_category(category: str, include_geometry: bool = False) -> str:
    """Get Revit elements by category."""
    try:
        result = await bridge.run_mcp_tool(
            "get_elements_by_category",
            {"category": category, "include_geometry": include_geometry},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error extracting {category}: {e}"

@tool
async def revit_get_project_info() -> str:
    """Get project info from the active Revit document."""
    try:
        result = await bridge.run_mcp_tool("get_project_info", {})
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error: {e}"

# -----------------------------------------------------------------------------
# RAG / Code Tools
# -----------------------------------------------------------------------------

@tool
def query_building_code(query: str, jurisdiction: str = "default") -> str:
    """Query building code vector database for code snippets."""
    snippets = query_code_db(query, jurisdiction)
    return json.dumps(snippets, indent=2)

# -----------------------------------------------------------------------------
# Deterministic Math (LLM Never Calculates)
# -----------------------------------------------------------------------------

@tool
def calculate_septic_setback(
    tank_locations_json: str,
    property_lines_json: str,
    required_distance_ft: float = 50.0,
) -> str:
    """Check if septic tanks comply with property line setback requirements."""
    tanks = normalize_elements(json.loads(tank_locations_json))
    lines = normalize_elements(json.loads(property_lines_json))

    def _pt_seg_dist(px, py, x1, y1, x2, y2):
        l2 = (x1 - x2) ** 2 + (y1 - y2) ** 2
        if l2 == 0.0:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)) / l2))
        return math.hypot(px - (x1 + t * (x2 - x1)), py - (y1 + t * (y2 - y1)))

    results = []
    all_pass = True
    for tank in tanks:
        if not tank.geometry:
            continue
        tx, ty = tank.geometry[0].x, tank.geometry[0].y
        best = float("inf")
        line_id = -1
        for line in lines:
            if not line.geometry:
                continue
            verts = line.geometry
            for i in range(len(verts) - 1):
                d = _pt_seg_dist(tx, ty, verts[i].x, verts[i].y, verts[i + 1].x, verts[i + 1].y)
                if d < best:
                    best = d
                    line_id = line.element_id
        compliant = best >= required_distance_ft
        if not compliant:
            all_pass = False
        results.append({
            "tank_id": tank.element_id,
            "closest_line_id": line_id,
            "distance_ft": round(best, 2),
            "required_ft": required_distance_ft,
            "compliant": compliant,
        })
    return json.dumps({"pass": all_pass, "checks": results}, indent=2)

@tool
def calculate_window_to_wall_ratio(
    wall_areas_json: str, window_areas_json: str, max_wwr_percent: float = 40.0
) -> str:
    """Calculate the window-to-wall ratio and check compliance."""
    walls = normalize_elements(json.loads(wall_areas_json))
    windows = normalize_elements(json.loads(window_areas_json))

    wall_area = {w.element_id: (get_parameter_float(w.model_dump(), ["Area", "area_sf", "area"], 0.0) or 0.0) for w in walls}
    win_area_by_wall: dict[int, float] = {}
    for win in windows:
        host = get_parameter_value(win.model_dump(), ["host_wall_id", "Host Wall Id", "wall_id"], -1)
        area = get_parameter_float(win.model_dump(), ["Area", "area_sf", "area"], 0.0) or 0.0
        if host != -1:
            win_area_by_wall[host] = win_area_by_wall.get(host, 0.0) + area

    results = []
    all_pass = True
    for wid, w_area in wall_area.items():
        if w_area <= 0:
            continue
        wa = win_area_by_wall.get(wid, 0.0)
        wwr = (wa / w_area) * 100.0
        compliant = wwr <= max_wwr_percent
        if not compliant:
            all_pass = False
        results.append({
            "wall_id": wid,
            "wall_area_sf": w_area,
            "window_area_sf": wa,
            "wwr_percent": round(wwr, 2),
            "max_allowed_percent": max_wwr_percent,
            "compliant": compliant,
        })
    return json.dumps({"pass": all_pass, "checks": results}, indent=2)

@tool
def calculate_energy_envelope_compliance(
    wall_u_factor: float,
    roof_u_factor: float,
    window_u_factor: float,
    window_shgc: float,
    climate_zone: str = "5B",
) -> str:
    """Check if envelope parameters comply with energy code limits."""
    limits = {
        "wall_u": 0.060,
        "roof_u": 0.032,
        "window_u": 0.30,
        "window_shgc": 0.25,
    }
    violations = []
    if wall_u_factor > limits["wall_u"]:
        violations.append({"param": "wall_u_factor", "value": wall_u_factor, "limit": limits["wall_u"]})
    if roof_u_factor > limits["roof_u"]:
        violations.append({"param": "roof_u_factor", "value": roof_u_factor, "limit": limits["roof_u"]})
    if window_u_factor > limits["window_u"]:
        violations.append({"param": "window_u_factor", "value": window_u_factor, "limit": limits["window_u"]})
    if window_shgc > limits["window_shgc"]:
        violations.append({"param": "window_shgc", "value": window_shgc, "limit": limits["window_shgc"]})
    return json.dumps({
        "climate_zone": climate_zone,
        "pass": len(violations) == 0,
        "violations": violations,
        "inputs": {
            "wall_u_factor": wall_u_factor,
            "roof_u_factor": roof_u_factor,
            "window_u_factor": window_u_factor,
            "window_shgc": window_shgc,
        },
    }, indent=2)

ALL_TOOLS = [
    revit_list_tools,
    revit_run_tool,
    revit_get_elements_by_category,
    revit_get_project_info,
    query_building_code,
    calculate_septic_setback,
    calculate_window_to_wall_ratio,
    calculate_energy_envelope_compliance,
]
