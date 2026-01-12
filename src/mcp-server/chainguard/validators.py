"""
CHAINGUARD MCP Server - Validators Module

Contains: SyntaxValidator with full async I/O

Copyright (c) 2026 Provimedia GmbH
Licensed under the Polyform Noncommercial License 1.0.0
See LICENSE file in the project root for full license information.
"""

import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List

from .config import SYNTAX_CHECK_TIMEOUT_SECONDS, PHPSTAN_ENABLED, PHPSTAN_LEVEL, logger

# Async file I/O
try:
    import aiofiles
    HAS_AIOFILES = True
except ImportError:
    HAS_AIOFILES = False


# =============================================================================
# Syntax Validation (PHP, JS, JSON, Python, TypeScript)
# =============================================================================
class SyntaxValidator:
    """
    Validates file syntax before runtime - catches errors early.

    Supported languages:
        - PHP: Uses `php -l` (lint mode)
        - JavaScript: Uses `node --check`
        - JSON: Uses Python's json.load()
        - Python: Uses `python3 -m py_compile`
        - TypeScript/TSX: Uses `npx tsc --noEmit`
    """

    @staticmethod
    async def validate_file(file_path: str, project_path: str) -> Dict[str, Any]:
        """
        Validate a file based on its extension.
        Returns: {"valid": bool, "errors": [...], "checked": str}
        """
        full_path = Path(project_path) / file_path if not Path(file_path).is_absolute() else Path(file_path)

        if not full_path.exists():
            return {"valid": True, "errors": [], "checked": "file not found"}

        ext = full_path.suffix.lower()
        errors = []

        try:
            # === PHP Validation ===
            phpstan_available = None  # None = not checked, True/False = checked
            if ext == ".php" and ".blade.php" not in str(full_path):
                # Step 1: Basic syntax check (php -l)
                result = await SyntaxValidator._run_command(
                    ["php", "-l", str(full_path)]
                )
                if result["returncode"] != 0:
                    error_msg = SyntaxValidator._extract_php_error(result["stderr"] or result["stdout"])
                    errors.append({
                        "type": "PHP Syntax",
                        "message": error_msg,
                        "file": str(file_path)
                    })
                # Step 2: Static analysis with PHPStan (if enabled and syntax OK)
                elif PHPSTAN_ENABLED:
                    phpstan_result = await SyntaxValidator._run_phpstan(str(full_path))
                    phpstan_available = phpstan_result["available"]
                    if phpstan_result["errors"]:
                        for err in phpstan_result["errors"][:3]:  # Max 3 errors
                            errors.append({
                                "type": "PHPStan",
                                "message": err,
                                "file": str(file_path)
                            })

            # === JavaScript/TypeScript Validation ===
            elif ext in [".js", ".mjs", ".cjs"]:
                result = await SyntaxValidator._run_command(
                    ["node", "--check", str(full_path)]
                )
                if result["returncode"] != 0:
                    error_msg = SyntaxValidator._extract_js_error(result["stderr"])
                    errors.append({
                        "type": "JS Syntax",
                        "message": error_msg,
                        "file": str(file_path)
                    })

            # === JSON Validation (async) ===
            elif ext == ".json":
                try:
                    if HAS_AIOFILES:
                        async with aiofiles.open(full_path, 'r', encoding='utf-8') as f:
                            content = await f.read()
                            json.loads(content)
                    else:
                        with open(full_path, 'r', encoding='utf-8') as f:
                            json.load(f)
                except json.JSONDecodeError as e:
                    errors.append({
                        "type": "JSON",
                        "message": f"Line {e.lineno}: {e.msg}",
                        "file": str(file_path)
                    })

            # === Python Validation ===
            elif ext == ".py":
                result = await SyntaxValidator._run_command(
                    ["python3", "-m", "py_compile", str(full_path)]
                )
                if result["returncode"] != 0:
                    error_msg = SyntaxValidator._extract_python_error(result["stderr"])
                    errors.append({
                        "type": "Python Syntax",
                        "message": error_msg,
                        "file": str(file_path)
                    })

            # === TypeScript/TSX Validation ===
            elif ext in [".ts", ".tsx"]:
                result = await SyntaxValidator._run_command(
                    ["npx", "--yes", "tsc", "--noEmit", "--skipLibCheck",
                     "--allowJs", "--target", "ES2020", str(full_path)]
                )
                if result["returncode"] != 0 and "error TS" in result["stdout"]:
                    error_msg = SyntaxValidator._extract_ts_error(result["stdout"])
                    errors.append({
                        "type": "TS Syntax",
                        "message": error_msg,
                        "file": str(file_path)
                    })

        except Exception as e:
            logger.error(f"Validation error for {file_path}: {e}")

        result = {
            "valid": len(errors) == 0,
            "errors": errors,
            "checked": ext or "unknown"
        }
        # v6.3: Include PHPStan availability for user hints
        if phpstan_available is not None:
            result["phpstan_available"] = phpstan_available
        return result

    @staticmethod
    async def _run_command(cmd: List[str]) -> Dict[str, Any]:
        """Run a command asynchronously."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=SYNTAX_CHECK_TIMEOUT_SECONDS
            )
            return {
                "returncode": proc.returncode,
                "stdout": stdout.decode() if stdout else "",
                "stderr": stderr.decode() if stderr else ""
            }
        except asyncio.TimeoutError:
            return {"returncode": -1, "stdout": "", "stderr": "Timeout"}
        except FileNotFoundError:
            return {"returncode": -1, "stdout": "", "stderr": "Command not found"}

    @staticmethod
    def _extract_php_error(output: str) -> str:
        """Extract meaningful error from PHP -l output."""
        for line in output.split('\n'):
            if 'Parse error' in line or 'Fatal error' in line or 'syntax error' in line:
                if ' in ' in line:
                    parts = line.split(' in ')
                    return parts[0].strip()
                return line.strip()[:100]
        return output.strip()[:100]

    @staticmethod
    def _extract_js_error(output: str) -> str:
        """Extract meaningful error from Node --check output."""
        for line in output.split('\n'):
            if 'SyntaxError' in line or 'Error' in line:
                return line.strip()[:100]
        return output.strip()[:100]

    @staticmethod
    def _extract_python_error(output: str) -> str:
        """Extract meaningful error from py_compile output."""
        lines = output.strip().split('\n')
        for i, line in enumerate(lines):
            if 'SyntaxError' in line or 'IndentationError' in line or 'TabError' in line:
                return line.strip()[:100]
            if 'line' in line.lower() and 'file' in line.lower():
                return line.strip()[:100]
        return output.strip()[:100] if output else "Syntax error"

    @staticmethod
    def _extract_ts_error(output: str) -> str:
        """Extract first TypeScript error from tsc output."""
        for line in output.split('\n'):
            if 'error TS' in line:
                if '): error' in line:
                    parts = line.split('): error')
                    if len(parts) > 1:
                        return f"error{parts[1][:80]}"
                return line.strip()[:100]
        return output.strip()[:100] if output else "TypeScript error"

    @staticmethod
    async def _run_phpstan(file_path: str) -> Dict[str, Any]:
        """
        Run PHPStan static analysis on a PHP file.
        Returns: {"available": bool, "errors": [...]}

        Smart Project Detection:
        1. Looks for existing phpstan.neon in project root
        2. If not found, generates a temporary config
        3. Runs PHPStan with proper project context
        """
        file_path = Path(file_path)

        # Find project root (where vendor/ or phpstan.neon is)
        project_root = SyntaxValidator._find_php_project_root(file_path)

        # Check if PHPStan is available
        phpstan_cmd = SyntaxValidator._find_phpstan(project_root)
        if not phpstan_cmd:
            return {"available": False, "errors": []}

        # Check for existing phpstan.neon
        config_file = None
        for config_name in ["phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon"]:
            if (project_root / config_name).exists():
                config_file = project_root / config_name
                break

        # Build PHPStan command
        cmd = [phpstan_cmd, "analyse"]

        if config_file:
            # Use existing config
            cmd.extend(["--configuration", str(config_file)])
        else:
            # No config: check for autoload
            autoload_file = project_root / "vendor" / "autoload.php"
            if autoload_file.exists():
                cmd.extend(["--autoload-file", str(autoload_file)])

        cmd.extend([
            "--level", str(PHPSTAN_LEVEL),
            "--no-progress",
            "--no-ansi",
            "--error-format", "raw",
            str(file_path)
        ])

        # Run PHPStan from project root
        result = await SyntaxValidator._run_phpstan_in_dir(cmd, project_root)

        if result["returncode"] == 0:
            return {"available": True, "errors": []}

        # Parse errors from output
        errors = []
        output = result["stdout"] + result["stderr"]
        for line in output.split('\n'):
            line = line.strip()
            if line and ':' in line and not line.startswith('Note:') and not line.startswith('['):
                # Format: "file.php:12: Error message"
                parts = line.split(':', 2)
                if len(parts) >= 3:
                    line_num = parts[1].strip()
                    message = parts[2].strip()
                    if message and not message.startswith('--'):
                        errors.append(f"Line {line_num}: {message[:80]}")

        return {"available": True, "errors": errors}

    @staticmethod
    def _find_php_project_root(file_path: Path) -> Path:
        """Find PHP project root by looking for composer.json or vendor/."""
        current = file_path.parent if file_path.is_file() else file_path

        for _ in range(10):  # Max 10 levels up
            # Check for project indicators
            if (current / "composer.json").exists():
                return current
            if (current / "vendor").is_dir():
                return current
            if (current / "phpstan.neon").exists():
                return current
            if (current / "index.php").exists() and (current / "includes").is_dir():
                return current  # Simple PHP project structure

            parent = current.parent
            if parent == current:  # Reached filesystem root
                break
            current = parent

        # Fallback: use file's directory
        return file_path.parent if file_path.is_file() else file_path

    @staticmethod
    def _find_phpstan(project_root: Path) -> str:
        """Find PHPStan executable."""
        import shutil

        # 1. Check vendor/bin/phpstan
        vendor_phpstan = project_root / "vendor" / "bin" / "phpstan"
        if vendor_phpstan.exists():
            return str(vendor_phpstan)

        # 2. Check global phpstan
        global_phpstan = shutil.which("phpstan")
        if global_phpstan:
            return global_phpstan

        # 3. Check composer global
        home = Path.home()
        for global_path in [
            home / ".composer" / "vendor" / "bin" / "phpstan",
            home / ".config" / "composer" / "vendor" / "bin" / "phpstan",
        ]:
            if global_path.exists():
                return str(global_path)

        return ""

    @staticmethod
    async def _run_phpstan_in_dir(cmd: List[str], cwd: Path) -> Dict[str, Any]:
        """Run PHPStan in a specific directory."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd)
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=SYNTAX_CHECK_TIMEOUT_SECONDS * 2  # PHPStan needs more time
            )
            return {
                "returncode": proc.returncode,
                "stdout": stdout.decode() if stdout else "",
                "stderr": stderr.decode() if stderr else ""
            }
        except asyncio.TimeoutError:
            return {"returncode": -1, "stdout": "", "stderr": "PHPStan timeout"}
        except FileNotFoundError:
            return {"returncode": -1, "stdout": "", "stderr": "PHPStan not found"}
