"""
MCP Direct Protocol Test
========================
Simulates what AnythingLLM sends to the MCP server over stdio.

Usage:
    python test_mcp_direct.py [python_exe]

Examples:
    python test_mcp_direct.py                          # uses current python
    python test_mcp_direct.py venv/Scripts/python.exe  # uses venv python
"""

import json
import subprocess
import sys
import time
import threading
import os

# Determine which Python executable to use
if len(sys.argv) > 1:
    python_exe = sys.argv[1]
else:
    python_exe = sys.executable

MCP_SERVER_SCRIPT = os.path.join(os.path.dirname(__file__) or ".", "main_mcp.py")
STDERR_LOG = os.path.join(os.path.dirname(__file__) or ".", "mcp_stderr.log")

print("=" * 70)
print("MCP DIRECT PROTOCOL TEST")
print("=" * 70)
print(f"Python: {python_exe}")
print(f"Script: {MCP_SERVER_SCRIPT}")
print()

# Start the MCP server process
proc = subprocess.Popen(
    [python_exe, MCP_SERVER_SCRIPT],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

# Thread to capture stderr continuously
stderr_lines = []
stderr_lock = threading.Lock()

def capture_stderr():
    try:
        for line in proc.stderr:
            with stderr_lock:
                stderr_lines.append(line.rstrip())
    except:
        pass

stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
stderr_thread.start()

request_id = 1


def send_request(request: dict, timeout_s: float = 10.0) -> dict | None:
    """Send a JSON-RPC request and read the response with timeout."""
    global request_id
    
    if "id" not in request:
        request["id"] = str(request_id)
        request_id += 1
    
    payload = json.dumps(request) + "\n"
    print(f"\n{'─'*70}")
    print(f">>> METHOD: {request.get('method', '?')}  ID: {request.get('id')}")
    print(f"{'─'*70}")
    print(f"Request: {json.dumps(request, indent=2)}")
    
    # Write to stdin
    proc.stdin.write(payload)
    proc.stdin.flush()
    
    # Read response from stdout with timeout
    # (use a thread so we can timeout on Windows where select doesn't work on pipes)
    result = [None]
    exception = [None]
    
    def reader():
        try:
            line = proc.stdout.readline()
            result[0] = line
        except Exception as e:
            exception[0] = e
    
    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    reader_thread.join(timeout=timeout_s)
    
    if reader_thread.is_alive():
        print(f"!!! TIMEOUT: No response after {timeout_s}s")
        return None
    
    if exception[0]:
        print(f"!!! READ ERROR: {exception[0]}")
        return None
    
    response_line = result[0]
    if not response_line:
        print("!!! EOF on stdout - server may have crashed!")
        return None
    
    response_data = response_line.strip()
    if not response_data:
        print("!!! Empty response line")
        return None
    
    try:
        response = json.loads(response_data)
        print(f"\nResponse: {json.dumps(response, indent=2)}")
        return response
    except json.JSONDecodeError as e:
        print(f"!!! INVALID JSON: {response_data[:200]!r}")
        print(f"!!! Error: {e}")
        return None


def show_stderr():
    """Display captured stderr output."""
    with stderr_lock:
        if stderr_lines:
            print(f"\n--- STDERR ({len(stderr_lines)} lines) ---")
            for line in stderr_lines[-20:]:  # Show last 20 lines
                print(f"  [stderr] {line}")
            print("--- END STDERR ---")


try:
    # Give the server a moment to start
    time.sleep(2)
    show_stderr()
    
    # ===== STEP 1: Initialize =====
    print("\n\n" + "=" * 50)
    print("STEP 1: initialize")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    })
    
    if resp is None:
        print("\n!!! FAILED at initialize. Server may have crashed.")
        show_stderr()
        proc.terminate()
        sys.exit(1)
    
    if resp.get("error"):
        print(f"\n!!! INITIALIZE ERROR: {resp['error']}")
    
    # ===== STEP 2: notifications/initialized =====
    print("\n\n" + "=" * 50)
    print("STEP 2: notifications/initialized")
    print("=" * 50)
    proc.stdin.write(json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }) + "\n")
    proc.stdin.flush()
    print("Sent (no response expected)")
    time.sleep(0.5)
    
    # ===== STEP 3: tools/list =====
    print("\n\n" + "=" * 50)
    print("STEP 3: tools/list")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "params": {},
    })
    
    if resp and resp.get("result"):
        tools = resp["result"].get("tools", [])
        print(f"\n{'='*50}")
        print("TOOLS ANALYSIS")
        print(f"{'='*50}")
        print(f"Total tools returned: {len(tools)}")
        
        native = [t for t in tools if not t.get("name", "").startswith("axo_")]
        custom = [t for t in tools if t.get("name", "").startswith("axo_")]
        
        print(f"Native Revit tools: {len(native)}")
        if native:
            for t in native:
                desc = t.get('description', '')[:120]
                print(f"  ✓ {t['name']} - {desc}")
        else:
            print("  ⚠ NO NATIVE REVIT TOOLS!")
            print("  → Bridge could not connect to Revit pipe")
            print("  → Check: Is Revit running with MCP plugin active?")
        
        print(f"\nCustom proxy tools: {len(custom)}")
        for t in custom:
            print(f"  ✓ {t['name']}")
    
    show_stderr()
    
    # ===== STEP 4: tools/call - get_elements_by_category for Levels =====
    print("\n\n" + "=" * 50)
    print("STEP 4: tools/call - get_elements_by_category (Levels)")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "get_elements_by_category",
            "arguments": {"category": "Levels", "include_geometry": False},
        },
    })
    
    if resp:
        if resp.get("error"):
            print(f"\n⚠ ERROR RESPONSE:")
            print(f"  Code: {resp['error'].get('code')}")
            print(f"  Message: {resp['error'].get('message')}")
        elif resp.get("result"):
            content = resp["result"].get("content", [])
            print(f"\n✓ Got result with {len(content)} content item(s)")
            for item in content[:3]:
                text = item.get("text", "")[:300]
                print(f"  Content preview: {text}...")
    
    show_stderr()
    
    # ===== STEP 5: tools/call - query_model for Levels =====
    print("\n\n" + "=" * 50)
    print("STEP 5: tools/call - query_model (Levels)")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "query_model",
            "arguments": {
                "input": {
                    "categories": ["OST_Levels"],
                    "searchScope": "AllViews",
                    "maxResults": 50,
                }
            },
        },
    })
    
    if resp:
        if resp.get("error"):
            print(f"\n⚠ ERROR RESPONSE:")
            print(f"  Code: {resp['error'].get('code')}")
            print(f"  Message: {resp['error'].get('message')}")
        elif resp.get("result"):
            content = resp["result"].get("content", [])
            print(f"\n✓ Got result with {len(content)} content item(s)")
            for item in content[:3]:
                text = item.get("text", "")[:300]
                print(f"  Content preview: {text}...")
    
    show_stderr()
    
    # ===== STEP 6: Error test =====
    print("\n\n" + "=" * 50)
    print("STEP 6: tools/call - non-existent tool")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "nonexistent_tool",
            "arguments": {},
        },
    })
    
    if resp and resp.get("error"):
        print(f"\n✓ Expected error:")
        print(f"  Code: {resp['error'].get('code')}")
        print(f"  Message: {resp['error'].get('message')}")

    # ===== STEP 7: tools/call - axo_audit_floor_area (all levels) =====
    print("\n\n" + "=" * 50)
    print("STEP 7: tools/call - axo_audit_floor_area (all levels)")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "axo_audit_floor_area",
            "arguments": {},
        },
    })

    if resp:
        if resp.get("error"):
            print(f"\n⚠ ERROR RESPONSE:")
            print(f"  Code: {resp['error'].get('code')}")
            print(f"  Message: {resp['error'].get('message')}")
        elif resp.get("result"):
            content = resp["result"].get("content", [])
            print(f"\n✓ Got result with {len(content)} content item(s)")
            for item in content[:3]:
                text = item.get("text", "")[:500]
                print(f"  Content preview: {text}...")

    show_stderr()

    # ===== STEP 8: tools/call - axo_audit_floor_area (filtered by level) =====
    print("\n\n" + "=" * 50)
    print("STEP 8: tools/call - axo_audit_floor_area (filtered by level)")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "axo_audit_floor_area",
            "arguments": {
                "level_names": ["FP1.GARAGE", "FP2.ADU"],
                "include_room_details": True,
            },
        },
    })

    if resp:
        if resp.get("error"):
            print(f"\n⚠ ERROR RESPONSE:")
            print(f"  Code: {resp['error'].get('code')}")
            print(f"  Message: {resp['error'].get('message')}")
        elif resp.get("result"):
            content = resp["result"].get("content", [])
            print(f"\n✓ Got result with {len(content)} content item(s)")
            for item in content[:3]:
                text = item.get("text", "")[:500]
                print(f"  Content preview: {text}...")

    show_stderr()

    # ===== STEP 9: tools/call - axo_audit_lot_area =====
    print("\n\n" + "=" * 50)
    print("STEP 9: tools/call - axo_audit_lot_area")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "axo_audit_lot_area",
            "arguments": {"area_unit": "both"},
        },
    })

    if resp:
        if resp.get("error"):
            print(f"\n⚠ ERROR RESPONSE:")
            print(f"  Code: {resp['error'].get('code')}")
            print(f"  Message: {resp['error'].get('message')}")
        elif resp.get("result"):
            content = resp["result"].get("content", [])
            print(f"\n✓ Got result with {len(content)} content item(s)")
            for item in content[:3]:
                text = item.get("text", "")[:500]
                print(f"  Content preview: {text}...")

    # ===== STEP 10: tools/call - axo_audit_setback =====
    print("\n\n" + "=" * 50)
    print("STEP 10: tools/call - axo_audit_setback")
    print("=" * 50)
    resp = send_request({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "axo_audit_setback",
            "arguments": {"output_unit": "ft_in"},
        },
    })

    if resp:
        if resp.get("error"):
            print(f"\n⚠ ERROR RESPONSE:")
            print(f"  Code: {resp['error'].get('code')}")
            print(f"  Message: {resp['error'].get('message')}")
        elif resp.get("result"):
            content = resp["result"].get("content", [])
            print(f"\n✓ Got result with {len(content)} content item(s)")
            for item in content[:3]:
                text = item.get("text", "")[:800]
                print(f"  Content preview: {text}...")

    show_stderr()

except KeyboardInterrupt:
    print("\n\nInterrupted by user.")
except Exception as e:
    print(f"\n\n!!! Test error: {e}")
    import traceback
    traceback.print_exc()
finally:
    print("\n\n" + "=" * 50)
    print("CLEANUP")
    print("=" * 50)
    show_stderr()
    
    # Save stderr to file for reference
    with stderr_lock:
        if stderr_lines:
            try:
                with open(STDERR_LOG, "w") as f:
                    f.write("\n".join(stderr_lines))
                print(f"Full stderr saved to: {STDERR_LOG}")
            except:
                pass
    
    try:
        proc.terminate()
        proc.wait(timeout=3)
        print("Server terminated.")
    except:
        try:
            proc.kill()
            print("Server killed.")
        except:
            pass
