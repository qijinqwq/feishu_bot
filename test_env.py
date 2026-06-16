"""Quick environment check - no interaction needed."""
import sys
import os

ok = True

print(f"Python: {sys.version.split()[0]}")
print(f"Path  : {sys.executable}")
print()

# --- dependencies ---
deps = {
    "lark_oapi": "lark-oapi",
    "apscheduler": "APScheduler",
}
for module, pkg in deps.items():
    try:
        __import__(module)
        print(f"  [OK] {pkg}")
    except ImportError:
        print(f"  [MISSING] {pkg} -> pip install {pkg}")
        ok = False

# --- config ---
print()
try:
    from config import APP_ID, APP_SECRET, CLAUDE_CLI_PATH
    if APP_ID == "cli_xxxxxxxxxxxx":
        print("  [WARN] APP_ID not configured in config.py!")
        ok = False
    else:
        print(f"  APP_ID     : {APP_ID[:16]}...")
    if APP_SECRET == "xxxxxxxxxxxxxxxx":
        print("  [WARN] APP_SECRET not configured in config.py!")
        ok = False
    else:
        print(f"  APP_SECRET : ***configured***")
    print(f"  Claude CLI : {'EXISTS' if os.path.exists(CLAUDE_CLI_PATH) else 'MISSING'}")
except Exception as e:
    print(f"  [ERROR] config.py: {e}")
    ok = False

print()
if ok:
    print("=== ALL CHECKS PASSED ===")
else:
    print("=== SOME CHECKS FAILED - see above ===")

# Write result file for non-interactive use
with open(os.path.join(os.path.dirname(__file__), "test_result.txt"), "w") as f:
    f.write("OK" if ok else "FAIL")
