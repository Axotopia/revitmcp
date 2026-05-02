"""
Probe: Call query_model + get_element_data for OST_SiteProperty directly via the pipe.
Shows the raw JSON response so we can see exactly where the Area value lives.
"""
import win32file
import json
import subprocess
import sys


def get_pipe_name() -> str:
    try:
        result = subprocess.run(
            [
                "powershell", "-Command",
                "Get-ChildItem '\\\\.\\pipe\\' "
                "| Where-Object Name -Like 'revit-mcp*' "
                "| Select-Object -ExpandProperty Name",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            names = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            if names:
                chosen = rf"\\.\pipe\{names[0]}"
                print(f"PIPE: {chosen}", flush=True)
                return chosen
    except Exception:
        pass
    return r"\\.\pipe\revit-mcp"


def send_request(handle, method: str, params: dict, req_id: int) -> dict:
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": req_id,
    }
    message = json.dumps(request) + "\n"
    win32file.WriteFile(handle, message.encode("utf-8"))
    print(f"  >> SENT tools/call: {method} (id={req_id})", flush=True)

    # Read response
    import pywintypes
    buffer = ""
    while True:
        try:
            _hr, data = win32file.ReadFile(handle, 65536)
            if not data:
                break
            buffer += data.decode("utf-8", errors="replace")
            for line in buffer.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict) and parsed.get("id") == req_id:
                        return parsed
                except json.JSONDecodeError:
                    continue
        except pywintypes.error:
            break
    # Final sweep
    for line in buffer.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and parsed.get("id") == req_id:
                return parsed
        except json.JSONDecodeError:
            continue
    print(f"  !! No response for id={req_id}, buffer={buffer[:300]}", flush=True)
    return None


def probe():
    pipe_name = get_pipe_name()
    import win32file as wf
    import pywintypes

    handle = wf.CreateFile(
        pipe_name,
        wf.GENERIC_READ | wf.GENERIC_WRITE,
        0, None,
        wf.OPEN_EXISTING,
        0, None,
    )
    print("CONNECTED", flush=True)

    req_id = 1

    # === Step 1: query_model for OST_SiteProperty ===
    print("\n=== STEP 1: query_model(OST_SiteProperty) ===", flush=True)
    resp1 = send_request(handle, "tools/call", {
        "name": "query_model",
        "arguments": {
            "input": {
                "categories": ["OST_SiteProperty"],
                "searchScope": "AllViews",
                "maxResults": 10,
            }
        }
    }, req_id)
    req_id += 1

    if resp1:
        print(f"\nquery_model RAW RESPONSE:\n{json.dumps(resp1, indent=2)}", flush=True)

    # Extract element IDs
    element_ids = []
    if resp1:
        try:
            content_items = resp1.get("result", resp1).get("content", [])
            for item in content_items:
                if item.get("type") == "text":
                    text = item.get("text", "")
                    parsed = json.loads(text)
                    # Try outcome.elements
                    for el in parsed.get("outcome", {}).get("elements", []):
                        eid = el.get("elementId") or el.get("id")
                        if eid:
                            element_ids.append(eid)
                    # Try elements
                    if not element_ids:
                        for el in parsed.get("elements", []):
                            eid = el.get("elementId") or el.get("id")
                            if eid:
                                element_ids.append(eid)
        except Exception as e:
            print(f"  !! Parsing error: {e}", flush=True)

    if not element_ids:
        # Fallback: regex scan
        import re
        if resp1:
            text = json.dumps(resp1)
            element_ids = [int(m) for m in re.findall(r'\b(\d{6,8})\b', text)]
        print(f"\n  Element IDs (regex fallback): {element_ids}", flush=True)
    else:
        print(f"\n  Element IDs: {element_ids}", flush=True)

    if not element_ids:
        print("NO ELEMENT IDS FOUND - cannot proceed to step 2", flush=True)
        wf.CloseHandle(handle)
        return

    # === Step 2: get_element_data with AllParameters ===
    print("\n=== STEP 2: get_element_data(AllParameters) ===", flush=True)
    resp2 = send_request(handle, "tools/call", {
        "name": "get_element_data",
        "arguments": {
            "elementIds": [int(eid) for eid in element_ids],
            "outputOptions": {
                "basicElementInfo": True,
                "parametersOutputType": "AllParameters",
            },
        }
    }, req_id)
    req_id += 1

    if resp2:
        raw = json.dumps(resp2, indent=2)
        # Truncate to avoid massive output
        if len(raw) > 50000:
            raw = raw[:50000] + "\n  ... [TRUNCATED]"
        print(f"\nget_element_data RAW RESPONSE:\n{raw}", flush=True)

        # Also try to parse the elements and show keys
        try:
            content_items = resp2.get("result", resp2).get("content", [])
            for item in content_items:
                if item.get("type") == "text":
                    text = item.get("text", "")
                    parsed = json.loads(text)
                    # Walk ALL keys in elements
                    for el_key in ("elements",):
                        elems = parsed.get(el_key, [])
                        if not elems:
                            elems = parsed.get("outcome", {}).get("elements", [])
                        for i, elem in enumerate(elems):
                            print(f"\n  Element {i}:")
                            print(f"    Top-level keys ({len(elem)}): {list(elem.keys())}")
                            if "parameters" in elem and isinstance(elem["parameters"], dict):
                                p = elem["parameters"]
                                print(f"    Parameter keys ({len(p)}): {list(p.keys())[:30]}")
                                # Check if Area is one of them
                                for ak in ["Area", "area", "PROPERTY_LINE_AREA", "SITE_PROPERTY_LINE_AREA"]:
                                    if ak in p:
                                        print(f"    >>> FOUND '{ak}' in parameters: {p[ak]}")
                                # Also show a few sample values
                                for k in list(p.keys())[:5]:
                                    v = p[k]
                                    if isinstance(v, dict):
                                        print(f"      {k}: {json.dumps(v, default=str)[:200]}")
                                    else:
                                        print(f"      {k}: {v}")
                            # Also check if elem has "area" directly
                            if "area" in elem:
                                print(f"    >>> FOUND 'area' at top level: {elem['area']}")
        except Exception as e:
            print(f"  !! Parse error: {e}", flush=True)

    wf.CloseHandle(handle)
    print("\nDONE", flush=True)


if __name__ == "__main__":
    probe()
