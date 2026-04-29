"""
Revit MCP Pipe Probe — NDJSON protocol (newline-delimited JSON).
Auto-discovers the active Revit MCP pipe via PowerShell enumeration.
"""
import win32file
import pywintypes
import json
import subprocess
import sys


def get_pipe_name() -> str:
    """Discover the first active revit-mcp* named pipe."""
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
                print(f"DISCOVERED: {chosen}", flush=True)
                return chosen
    except Exception:
        pass
    return r"\\.\pipe\revit-mcp"


def probe():
    pipe_name = get_pipe_name()
    print(f"CONNECTING: {pipe_name}", flush=True)

    try:
        handle = win32file.CreateFile(
            pipe_name,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None,
            win32file.OPEN_EXISTING,
            0, None,
        )
    except Exception as exc:
        print(f"FAIL: Could not open pipe. {exc}", flush=True)
        sys.exit(1)

    try:
        # --- Send: NDJSON (JSON + newline, no binary length header) ---
        request = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            "id": 1,
        }
        message = json.dumps(request) + "\n"
        win32file.WriteFile(handle, message.encode("utf-8"))
        print(f"SENT: {len(message)} bytes", flush=True)

        # --- Receive: read chunks until we get a complete JSON response ---
        buffer = ""
        while True:
            try:
                _hr, data = win32file.ReadFile(handle, 65536)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="replace")

                # Check each complete line for our response
                for line in buffer.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict) and parsed.get("id") == request["id"]:
                            print("SUCCESS:", flush=True)
                            print(json.dumps(parsed, indent=2), flush=True)
                            return parsed
                    except json.JSONDecodeError:
                        continue

            except pywintypes.error:
                break

        # Final sweep of remaining buffer
        for line in buffer.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict) and parsed.get("id") == request["id"]:
                    print("SUCCESS:", flush=True)
                    print(json.dumps(parsed, indent=2), flush=True)
                    return parsed
            except json.JSONDecodeError:
                continue

        print(f"FAIL: No matching JSON-RPC response found in buffer ({len(buffer)} bytes)", flush=True)
        if buffer:
            print(f"RAW BUFFER:\n{buffer[:500]}", flush=True)
        sys.exit(1)

    except Exception as exc:
        print(f"FAIL: Communication error. {exc}", flush=True)
        sys.exit(1)
    finally:
        win32file.CloseHandle(handle)


if __name__ == "__main__":
    probe()
