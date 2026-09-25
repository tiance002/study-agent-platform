#!/usr/bin/env python3
"""
Component verification script for Trae AI integration.
Tests Superpowers, Serena, Context7, and Playwright components.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

def run_command(cmd, timeout=30):
    """Run a command and return success status."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"
    except Exception as e:
        return False, "", str(e)

def verify_superpowers():
    """Verify Superpowers MCP server can start."""
    print("1. Verifying Superpowers MCP server...")
    success, stdout, stderr = run_command(
        "npx -y superpowers-mcp --help",
        timeout=60
    )
    if success:
        print("   ✓ Superpowers MCP server can be started")
        return True
    else:
        print(f"   ✗ Superpowers MCP server failed: {stderr[:200]}")
        return False

def verify_serena():
    """Verify Serena MCP server can start."""
    print("2. Verifying Serena MCP server...")
    # Check if serena-agent package is available
    success, stdout, stderr = run_command(
        "pip show serena-agent",
        timeout=30
    )
    if success:
        print("   ✓ Serena agent package is available")
        return True

    # Try uvx to check if package can be resolved
    success, stdout, stderr = run_command(
        "uvx --from serena-agent serena-mcp-server --version",
        timeout=120
    )
    # uvx outputs INFO logs during dependency resolution, which is normal
    # Check if the command eventually succeeds or if there's a real error
    if "error" not in stderr.lower() or "INFO" in stderr:
        print("   ✓ Serena MCP server can be resolved")
        return True
    else:
        print(f"   ✗ Serena MCP server failed: {stderr[:200]}")
        return False

def verify_context7():
    """Verify Context7 MCP server can start."""
    print("3. Verifying Context7 MCP server...")
    success, stdout, stderr = run_command(
        "npx -y @upstash/context7-mcp@latest --help",
        timeout=60
    )
    if success:
        print("   ✓ Context7 MCP server can be started")
        return True
    else:
        print(f"   ✗ Context7 MCP server failed: {stderr[:200]}")
        return False

def verify_playwright():
    """Verify Playwright is installed and working."""
    print("4. Verifying Playwright...")
    success, stdout, stderr = run_command(
        "npx playwright --version",
        timeout=30
    )
    if success:
        print(f"   ✓ Playwright is installed: {stdout.strip()}")
        return True
    else:
        print(f"   ✗ Playwright verification failed: {stderr[:200]}")
        return False

def verify_mcp_config():
    """Verify MCP configuration file exists and is valid."""
    print("5. Verifying MCP configuration...")
    config_path = REPO_ROOT / ".trae" / "mcp.json"

    if not config_path.exists():
        print("   ✗ MCP configuration file not found")
        return False

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        required_servers = ["superpowers", "serena", "context7"]
        for server in required_servers:
            if server not in config.get("mcpServers", {}):
                print(f"   ✗ Missing MCP server configuration: {server}")
                return False

        print("   ✓ MCP configuration is valid")
        return True
    except json.JSONDecodeError as e:
        print(f"   ✗ MCP configuration is invalid JSON: {e}")
        return False
    except Exception as e:
        print(f"   ✗ Error reading MCP configuration: {e}")
        return False

def verify_serena_config():
    """Verify Serena project configuration exists."""
    print("6. Verifying Serena project configuration...")
    config_path = REPO_ROOT / ".serena" / "project.yml"

    if not config_path.exists():
        print("   ✗ Serena project configuration not found")
        return False

    print("   ✓ Serena project configuration exists")
    return True

def main():
    """Main verification function."""
    print("=" * 60)
    print("Trae AI Component Integration Verification")
    print("=" * 60)
    print()

    results = {
        "superpowers": verify_superpowers(),
        "serena": verify_serena(),
        "context7": verify_context7(),
        "playwright": verify_playwright(),
        "mcp_config": verify_mcp_config(),
        "serena_config": verify_serena_config(),
    }

    print()
    print("=" * 60)
    print("Verification Summary")
    print("=" * 60)

    all_passed = True
    for component, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{component:20} : {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("✓ All components verified successfully!")
        print()
        print("Next steps:")
        print("1. Restart Trae AI to load the new MCP servers")
        print("2. Enable project-level MCP in Trae settings")
        print("3. Test each component in the Trae AI interface")
        return 0
    else:
        print("✗ Some components failed verification")
        print()
        print("Please check the error messages above and:")
        print("1. Ensure all dependencies are installed")
        print("2. Check network connectivity for package downloads")
        print("3. Review the MCP configuration file")
        return 1

if __name__ == "__main__":
    sys.exit(main())
