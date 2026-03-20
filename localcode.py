#!/usr/bin/env python3
from __future__ import annotations

VERSION: Final[int] = 3
APP_NAME: Final[str] = "localcode"
SUMMARY_TOKEN_THRESHOLD: Final[int] = 90_000  # Trigger summary at 90k tokens (limit is 95k)

import ast
import datetime
import difflib
import fnmatch
import glob
import json
import math
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
import socketserver
from pathlib import Path
from re import Match
from typing import Any, Dict, Final, List, Optional, Set, Tuple, Union

# Configuration
LLAMA_HOST: str = os.getenv("LLAMA_HOST", "http://localhost:8080")
MODEL: str = os.getenv("LLAMA_MODEL", "local-model")  # Model name (for display, llama.cpp uses loaded model)
MAX_FILE_SIZE: Final[int] = 100 * 1024  # 100KB
MAX_LINE_LENGTH: Final[int] = 500
MAX_TOOL_LOOPS: Final[int] = 50
DEFAULT_BRIDGE_PORT: Final[int] = 9876
# Optional: temperature and other generation params
TEMPERATURE: float = float(os.getenv("LLAMA_TEMPERATURE", "0.7"))
MAX_TOKENS: int = int(os.getenv("LLAMA_MAX_TOKENS", "4096"))

TOOLS: Final[List[Dict[str, Any]]] = [
    {
        "type": "function",
        "name": "get_repo_map",
        "description": "Get a repository map showing all files and their structure. For Python files, includes function/class locations with line numbers. Call once without a pattern to see the complete repository. Use pattern parameter only to filter for specific files when needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Optional glob pattern to filter files (e.g., '*.py', 'src/*', 'tests/*'). Omit this parameter or leave empty to show ALL files in the repository.",
                },
                "include_details": {
                    "type": "boolean",
                    "description": "If true, include line numbers for Python functions/classes. Default: true.",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file. Creates new files or overwrites existing ones (with confirmation).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path"},
                "content": {"type": "string", "description": "Full file contents"},
                "overwrite": {"type": "boolean", "description": "If true, overwrite existing file. Default: false (will fail if file exists)."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": "Apply one exact find/replace edit to an existing text file. The find text must match exactly, including whitespace. Use shortest unique non-regex snippet.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path"},
                "find": {"type": "string", "description": "Exact text to replace"},
                "replace": {"type": "string", "description": "Replacement text; empty string deletes the match"},
            },
            "required": ["path", "find", "replace"],
        },
    },
    {
        "type": "function",
        "name": "run_shell_command",
        "description": "Run one shell command locally. The user will be asked to approve it first. Output is truncated to 500 lines with 500 characters per line.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "A single shell command"},
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "commit_changes",
        "description": "Create a git commit for all current changes with a concise commit message.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Git commit message"},
            },
            "required": ["message"],
        },
    },
    {
        "type": "function",
        "name": "browser_execute",
        "description": "Execute JavaScript in the currently active browser tab. Returns captured console logs and result. Use for debugging, inspection, or navigation (window.location = '...'). Always targets the active tab.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "JavaScript code to execute"}
            },
            "required": ["code"],
        },
    },
]


SYSTEM_PROMPT: Final[str] = (
    "You are a coding expert working inside a local repository tool called 'localcode'.\n\n"
    "Use tools via function calls. Never output XML, markdown code blocks, or any other format for tool calls.\n\n"
    "Behavior rules:\n"
    "- Think step by step before deciding to use tools.\n"
    "- Answer normally when no tool is needed.\n"
    "- Use get_repo_map once at the start to get the complete repository overview (all files, with Python line numbers). Only use pattern filter if you need to focus on specific files.\n"
    "- Use shell commands (cat, grep, sed, head, tail, wc) to read file contents or search for patterns. Use edit_file for modifications. Note: shell command output is truncated to 500 lines with 500 characters per line.\n"
    "- Use write_file to create new files or overwrite existing ones (requires confirmation for overwrite).\n"
    "- Use edit_file for precise find/replace edits. Make the 'find' string as short and unique as possible.\n"
    "- Preserve original formatting, whitespace, and surrounding code style exactly.\n"
    "- If an edit's exact find text is not found, use cat/grep to find the correct text first.\n"
    "- Preserve original formatting, whitespace, and surrounding code style exactly.\n"
    "- If an edit's exact find text is not found, read the file again and use a more precise match.\n"
    "- Only run shell commands when genuinely necessary (use cat/grep/sed for reading files).\n"
    "- After changes, call commit_changes with a short message if and only if files were actually modified.\n"
    "- Keep all user-facing answers concise.\n\n"
    "File operations:\n"
    "- get_repo_map: Shows ALL files in the repository. Python files include function/class line numbers. Non-Python files are listed without details. Excludes venv/, node_modules/, .git/, __pycache__/, data/. Call once without pattern for complete overview.\n"
    "- cat file.py: Read full file content. Use grep/sed to search within files. Use edit_file for modifications.\n"
    "- write_file: Create new files or overwrite (with confirmation). Use for full file rewrites.\n"
    "- edit_file: Precise find/replace edits. Best for small changes.\n"
    "- Files in excluded directories (venv/, node_modules/, .git/, __pycache__/, data/, etc.) are not shown in repo map.\n\n"
    "Important: Use the provided tools via function calling. Do not output tool calls as raw text, JSON, or XML in your messages.\n\n"
    "When copying code from tool responses for edit_file, always use the exact raw text from inside the ``` blocks. "
    "Never use the escaped JSON version (with \\n or \\\"). Copy the literal file content only."
)


def ansi(code: str) -> str:
    """Return ANSI escape sequence for terminal styling.

    Args:
        code: ANSI escape code (e.g., '1m' for bold, '31m' for red).

    Returns:
        Formatted ANSI escape string.
    """
    return f"\033[{code}"


def styled(text: str, style: str) -> str:
    """Wrap text with ANSI style codes for terminal output.

    Args:
        text: The text to style.
        style: ANSI style code (e.g., '1m' for bold, '32m' for green).

    Returns:
        Text wrapped with style codes and reset code.
    """
    return f"{ansi(style)}{text}{ansi('0m')}"


def run(shell_cmd: str) -> Optional[str]:
    """Run shell command and return stripped output or None on error.

    Args:
        shell_cmd: Shell command string to execute.

    Returns:
        Command output stripped of whitespace, or None if command fails.
    """
    try:
        return subprocess.check_output(
            shell_cmd, shell=True, text=True, stderr=subprocess.STDOUT
        ).strip()
    except Exception:
        return None


_TMUX_WIN: Optional[str] = run("tmux display-message -p '#{window_id}' 2>/dev/null")


def title(t: str) -> None:
    """Set terminal title and tmux window name if running in tmux.

    Args:
        t: Title string to set.
    """
    print(f"\033]0;{t}\007", end="", flush=True)
    if _TMUX_WIN:
        run(f"tmux rename-window -t {_TMUX_WIN} {t!r} 2>/dev/null")


def render_md(text: str) -> str:
    """Render markdown-like text with ANSI styling for terminal output.

    Supports:
    - Code blocks (```) with dark background
    - Inline code (`) with dark background
    - Links [text](url) as clickable terminal links
    - Headers (#, ##, ###) with yellow styling
    - Bold (**text**) and italic (*text* or _text_)

    Args:
        text: Markdown-formatted text to render.

    Returns:
        ANSI-styled string for terminal display.
    """
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    result: List[str] = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            inner = part[3:-3]
            if inner.startswith("\n"):
                inner = inner[1:]
            elif "\n" in inner:
                inner = inner.split("\n", 1)[1]
            inner_lines = inner.split("\n")
            inner = "\n".join(f"{line}{ansi('K')}" for line in inner_lines) + ansi("K")
            result.append(f"\n{ansi('48;5;236;37m')}{inner}{ansi('0m')}")
        elif part.startswith("`") and part.endswith("`"):
            result.append(f"{ansi('48;5;236m')}{part[1:-1]}{ansi('0m')}")
        else:
            part = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                lambda m: f"\033]8;;{m.group(2)}\033\\{ansi('4;34m')}{m.group(1)}{ansi('0m')}\033]8;;\033\\",
                part,
            )
            part = re.sub(r"\*\*(.+?)\*\*", lambda m: f"{ansi('1m')}{m.group(1)}{ansi('22m')}", part)
            part = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", lambda m: f"{ansi('3m')}{m.group(1)}{ansi('23m')}", part)
            part = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", lambda m: f"{ansi('3m')}{m.group(1)}{ansi('23m')}", part)

            def format_header(m: Match[str]) -> str:
                level, text_ = len(m.group(1)), m.group(2)
                if level == 1:
                    return f"{ansi('1;4;33m')}{text_}{ansi('0m')}"
                if level == 2:
                    return f"{ansi('1;33m')}{text_}{ansi('0m')}"
                return f"{ansi('33m')}{text_}{ansi('0m')}"

            part = re.sub(r"^(#{1,3}) (.+)$", format_header, part, flags=re.MULTILINE)
            result.append(part)
    return "".join(result)


def format_tool_call_display(name: str, args: Dict[str, Any]) -> str:
    """Format tool call arguments for user-friendly display.
    
    Instead of showing raw JSON, this returns a concise, meaningful representation
    of what the tool call does, tailored to each tool type.
    
    Args:
        name: The tool/function name.
        args: The tool arguments dictionary.
        
    Returns:
        A formatted string describing the tool call.
    """
    if name == "run_shell_command":
        cmd = args.get("command", "")
        return cmd if cmd else ""
    
    elif name == "commit_changes":
        message = args.get("message", "")
        return f'"{message}"' if message else ""
    
    elif name == "edit_file":
        path = args.get("path", "")
        find = args.get("find", "")
        replace = args.get("replace", "")
        find_preview = (find[:50] + "...") if len(find) > 50 else find
        return f"path={path}, find={find_preview!r}"
    
    elif name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        lines = len(content.splitlines()) if content else 0
        return f"path={path} ({lines} lines)"
    
    elif name == "get_repo_map":
        pattern = args.get("pattern", "")
        include_details = args.get("include_details", True)
        if pattern:
            return f"pattern={pattern!r}"
        return "" if include_details else "include_details=false"
    
    elif name == "browser_execute":
        code = args.get("code", "")
        code_preview = (code[:60] + "...") if len(code) > 60 else code
        return code_preview if code_preview else ""
    
    # Default: show minimal JSON for unknown tools
    return json.dumps(args, ensure_ascii=False)[:200]


def truncate(lines: List[str], n: int = 500, max_line_len: int = MAX_LINE_LENGTH) -> List[str]:
    """Truncate list of lines to fit within specified limits.

    Keeps first 20 and last 100 lines if truncation occurs, with [TRUNCATED] marker.
    Also truncates individual lines exceeding max_line_len.

    Args:
        lines: List of lines to truncate.
        n: Maximum number of lines to return (default 500).
        max_line_len: Maximum length of individual lines (default MAX_LINE_LENGTH).

    Returns:
        Truncated list of lines.
    """
    def trunc_line(line: str) -> str:
        return line if len(line) <= max_line_len else line[:max_line_len] + "..."

    lines = [trunc_line(line) for line in lines]
    return lines if len(lines) <= n else lines[:20] + ["[TRUNCATED]"] + lines[-100:]


def smart_truncate(lines: List[str], keep_first: int = 1, keep_last: int = 1, max_line_len: int = 80) -> List[str]:
    """Smart truncation for terminal display.
    
    Shows first and last lines, with line count summary in between.
    Great for quickly understanding output structure without clutter.

    Args:
        lines: List of lines to truncate.
        keep_first: Number of lines to keep from start.
        keep_last: Number of lines to keep from end.
        max_line_len: Maximum length of individual lines.

    Returns:
        Smart-truncated list of lines.
    """
    if not lines:
        return lines
    
    def trunc_line(line: str) -> str:
        return line if len(line) <= max_line_len else line[:max_line_len] + "..."
    
    lines = [trunc_line(line) for line in lines]
    
    if len(lines) <= keep_first + keep_last:
        return lines
    
    skipped = len(lines) - keep_first - keep_last
    return lines[:keep_first] + [f"... {skipped} lines skipped ..."] + lines[-keep_last:]


_CACHED_SYSTEM_INFO: Optional[Dict[str, Any]] = None


def system_summary() -> Dict[str, Any]:
    """Return cached system information dictionary.

    Gathers OS details, Python version, available tools, and tool versions.
    Results are cached to avoid repeated system calls.

    Returns:
        Dictionary containing:
        - os: Operating system name
        - release: OS release version
        - machine: Machine architecture
        - python: Python version
        - cwd: Current working directory
        - shell: Default shell
        - path: PATH environment variable
        - venv: Whether running in virtual environment
        - tools: List of available tools
        - versions: Dictionary of tool versions
    """
    global _CACHED_SYSTEM_INFO
    if _CACHED_SYSTEM_INFO is not None:
        return _CACHED_SYSTEM_INFO
    try:
        tools: List[str] = [
            "apt",
            "bash",
            "curl",
            "docker",
            "gcc",
            "git",
            "make",
            "node",
            "npm",
            "perl",
            "pip",
            "python3",
            "sh",
            "tar",
            "unzip",
            "wget",
            "zip",
        ]
        versions: Dict[str, str] = {
            tool: (run(f"{tool} --version") or "").split("\n")[0][:80]
            for tool in ["git", "python3", "pip", "node", "npm", "docker", "gcc"]
            if shutil.which(tool)
        }
        _CACHED_SYSTEM_INFO = {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "cwd": os.getcwd(),
            "shell": os.environ.get("SHELL") or os.environ.get("ComSpec") or "",
            "path": os.environ.get("PATH", ""),
            "venv": bool(os.environ.get("VIRTUAL_ENV") or sys.prefix != sys.base_prefix),
            "tools": [tool for tool in tools if shutil.which(tool)],
            "versions": {k: v for k, v in versions.items() if v},
        }
    except Exception:
        _CACHED_SYSTEM_INFO = {}
    return _CACHED_SYSTEM_INFO





def safe_repo_path(root: str, rel_path: str) -> Path:
    """Ensure path is within repository root to prevent path traversal.

    Args:
        root: Repository root directory.
        rel_path: Relative path to validate.

    Returns:
        Validated Path object within repo root.

    Raises:
        ValueError: If path escapes repository root.
    """
    p = Path(root, rel_path)
    resolved = p.resolve(strict=False)
    root_resolved = Path(root).resolve()
    if not str(resolved).startswith(str(root_resolved)):
        raise ValueError(f"path escapes repo: {rel_path}")
    return p


def safe_read_file(
    path: str, root: Optional[str] = None, confirm_large: bool = False
) -> Tuple[Optional[str], Optional[str]]:
    """Safely read file with security and size checks.

    Validates file exists, checks for symlinks pointing outside repo,
    verifies file is regular (not special), and enforces size limits.

    Args:
        path: File path to read (absolute or relative).
        root: Repository root for path validation (None for absolute paths).
        confirm_large: If True, prompt user to load files exceeding MAX_FILE_SIZE.

    Returns:
        Tuple of (content, error):
        - content: File contents or "[empty]" for empty files, None on error.
        - error: Error message string, None on success.
    """
    p = Path(path) if root is None else safe_repo_path(root, path)
    if not p.exists():
        return None, "not found"
    if p.is_symlink():
        try:
            target = p.resolve()
            root_path = Path(root).resolve() if root else Path.cwd().resolve()
            if not str(target).startswith(str(root_path)):
                return None, f"symlink points outside repo: {target}"
        except (OSError, ValueError) as e:
            return None, f"symlink error: {e}"
    try:
        mode = p.stat().st_mode
        if not stat.S_ISREG(mode):
            return None, "special file (not regular)"
    except OSError as e:
        return None, f"cannot stat: {e}"
    try:
        size = p.stat().st_size
        if size > MAX_FILE_SIZE:
            size_kb = size / 1024
            size_str = f"{size_kb:.1f}KB" if size_kb < 1024 else f"{size_kb / 1024:.1f}MB"
            if confirm_large:
                print(styled(f"Warning: {path} is {size_str} (>{MAX_FILE_SIZE // 1024}KB)", "93m"))
                try:
                    answer = input("Load anyway? (y/n): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    answer = "n"
                if answer != "y":
                    return None, f"skipped (too large: {size_str})"
            else:
                return None, f"file too large: {size_str}"
    except OSError as e:
        return None, f"cannot check size: {e}"
    try:
        content = p.read_text()
        return content if content else "[empty]", None
    except PermissionError:
        return None, "permission denied"
    except UnicodeDecodeError:
        return None, "binary/not UTF-8"
    except OSError as e:
        return None, f"read error: {e}"


def get_map(root: str, pattern: Optional[str] = None, include_details: bool = True) -> str:
    """Generate repository file map showing ALL files and Python definitions with line numbers.

    Uses smart filesystem scanning (no git dependency), excludes common clutter directories,
    and extracts Python def/class/method locations with line numbers. By default shows all files.

    Args:
        root: Repository root directory.
        pattern: Optional glob pattern to filter files (e.g., '*.py', 'src/*'). Omit to show all files.
        include_details: If true, include line numbers for Python elements.

    Returns:
        Formatted markdown string with all files and Python definitions (when include_details is True).
    """
    BINARY_EXT: Set[str] = {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp",
        ".mp3", ".mp4", ".wav", ".avi", ".mov",
        ".zip", ".tar", ".gz", ".rar", ".7z", ".pdf",
        ".exe", ".dll", ".so", ".dylib", ".pyc", ".whl", ".egg",
        ".woff", ".woff2", ".ttf", ".eot",
    }
    
    EXCLUDE_DIRS: Set[str] = {
        ".git", "node_modules", "__pycache__", "venv", ".venv",
        ".tox", "dist", "build", ".eggs", ".mypy_cache",
        ".pytest_cache", ".ruff_cache", "htmlcov", ".coverage",
        "env", ".env", "data", "datasets", "models", "cache",
        "*.egg-info", ".ipynb_checkpoints"
    }
    
    output = []
    file_count = 0
    excluded_found: Set[str] = set()
    root_path = Path(root)
    
    # Walk filesystem with early pruning
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune excluded directories IN PLACE (prevents descent)
        original_dirnames = dirnames.copy()
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        
        # Track excluded directories we found
        for d in original_dirnames:
            if d in EXCLUDE_DIRS:
                rel_dir = Path(dirpath) / d
                try:
                    excluded_found.add(str(rel_dir.relative_to(root_path)))
                except ValueError:
                    excluded_found.add(d)
        
        for filename in filenames:
            filepath = Path(dirpath) / filename
            try:
                rel_path = filepath.relative_to(root_path)
                rel_path_str = str(rel_path)
            except ValueError:
                continue
            
            # Apply pattern filter if provided
            if pattern and not fnmatch.fnmatch(rel_path_str, pattern):
                continue
            
            # Check binary by extension first (fast)
            if filepath.suffix.lower() in BINARY_EXT:
                output.append(f"{rel_path_str} [binary]")
                file_count += 1
                continue
            
            # Check for binary content (sample first bytes)
            try:
                with open(filepath, "rb") as f:
                    chunk = f.read(512)
                    if b"\x00" in chunk:
                        output.append(f"{rel_path_str} [binary]")
                        file_count += 1
                        continue
            except Exception:
                output.append(f"{rel_path_str} [unreadable]")
                file_count += 1
                continue
            
            # Process Python files for element details
            if include_details and filepath.suffix == ".py":
                output.append(f"{rel_path_str}:")
                elements = _extract_python_elements(filepath)
                for elem in elements:
                    output.append(f"  {elem['type']} {elem['name']} ({elem['start_line']}-{elem['end_line']})")
            else:
                output.append(rel_path_str)
            
            file_count += 1
    
    # Add excluded directories to output
    if excluded_found:
        output.append("")
        output.append("# Excluded directories:")
        for exc_dir in sorted(excluded_found):
            output.append(f"  {exc_dir}/")
    
    return "\n".join(output)


def _extract_python_elements(filepath: Path) -> List[Dict[str, Any]]:
    """Extract Python functions, classes, and methods with line numbers.
    
    Args:
        filepath: Path to Python file.
        
    Returns:
        List of dicts with name, type ('def' or 'class'), and line ranges.
    """
    try:
        source = filepath.read_text()
        tree = ast.parse(source)
        elements: List[Dict[str, Any]] = []
        
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                elements.append({
                    "name": node.name,
                    "type": "def",
                    "start_line": node.lineno,
                    "end_line": getattr(node, 'end_lineno', node.lineno)
                })
            elif isinstance(node, ast.ClassDef):
                # Add the class itself
                elements.append({
                    "name": node.name,
                    "type": "class",
                    "start_line": node.lineno,
                    "end_line": getattr(node, 'end_lineno', node.lineno)
                })
                # Add methods within the class
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        elements.append({
                            "name": f"{node.name}.{item.name}",
                            "type": "def",
                            "start_line": item.lineno,
                            "end_line": getattr(item, 'end_lineno', item.lineno)
                        })
        
        # Sort by line number
        return sorted(elements, key=lambda e: e["start_line"])
    except Exception:
        return []


def validate_path_for_shell(path: str) -> str:
    """Validate and sanitize a path for use in shell commands.
    
    Ensures the path is absolute, exists, and contains no shell metacharacters.
    
    Args:
        path: Path string to validate.
        
    Returns:
        Validated absolute path string.
        
    Raises:
        ValueError: If path is invalid or contains dangerous characters.
    """
    # Check for shell metacharacters
    dangerous_chars = set(';&|`$\\(){}[]<>!#\n\r')
    if any(c in path for c in dangerous_chars):
        raise ValueError(f"Path contains invalid characters: {path!r}")
    
    # Resolve to absolute path
    p = Path(path).resolve()
    
    # Verify it exists and is a directory
    if not p.exists():
        raise ValueError(f"Path does not exist: {path!r}")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {path!r}")
    
    return str(p)


def is_safe_read_command(cmd: str) -> bool:
    """Check if a shell command is a safe read-only operation.
    
    Whitelists: cat, sed, head, tail, wc, grep, find, ls, pwd, echo, date
    with safe patterns (no pipes to dangerous commands, no redirection to files).
    
    Args:
        cmd: Shell command to check.
        
    Returns:
        True if command appears safe for auto-approval.
    """
    safe_commands = {"cat", "sed", "head", "tail", "wc", "grep", "find", "ls", "pwd", "echo", "date", "file", "which"}
    
    # Remove leading whitespace and get first word
    cmd_stripped = cmd.strip()
    first_word = cmd_stripped.split()[0] if cmd_stripped.split() else ""
    
    # Check if it's a safe command
    if first_word not in safe_commands:
        return False
    
    # Check for command substitution patterns (backticks and $())
    # These can chain commands even if the base command is safe
    if "`" in cmd_stripped or "$(" in cmd_stripped:
        return False
    
    # Check for dangerous patterns
    dangerous_patterns = [
        "| rm", "| xargs rm", "| sh", "| bash", "| eval",
        ">", ">>", "2>", "&>",
        "; rm", "; mv", "; cp", "; chmod", "; chown",
        "&& rm", "|| rm",
    ]
    
    for pattern in dangerous_patterns:
        if pattern in cmd_stripped:
            return False
    
    # For sed, check it's only using safe operations (no in-place editing)
    if first_word == "sed":
        if "-i" in cmd_stripped or ">>" in cmd_stripped or ">" in cmd_stripped:
            return False
    
    # For find, check it's not executing commands
    if first_word == "find":
        if "-exec" in cmd_stripped or "-delete" in cmd_stripped:
            return False
    
    return True


def run_shell_interactive(cmd: str, stream_output: bool = True) -> Tuple[List[str], int]:
    """Run shell command interactively with live output.

    Streams command output line by line, handles Ctrl+C gracefully.

    Args:
        cmd: Shell command to execute.
        stream_output: Whether to print output as it's generated (default: True).

    Returns:
        Tuple of (output_lines, exit_code):
        - output_lines: List of output lines.
        - exit_code: Command exit code.
    """
    output_lines: List[str] = []
    process = subprocess.Popen(
        cmd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if stream_output:
                print(line, end="", flush=True)
            output_lines.append(line.rstrip("\n"))
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=2)
        except Exception:
            pass
        output_lines.append("[INTERRUPTED]")
        if stream_output:
            print("\n[INTERRUPTED]")
    return output_lines, process.returncode


def lint_py(path: str, content: str) -> Tuple[bool, Optional[str]]:
    """Check Python file for syntax errors.

    Args:
        path: File path (must end with .py to be checked).
        content: File contents to parse.

    Returns:
        Tuple of (is_valid, error_message):
        - is_valid: True if syntax is valid or not a Python file.
        - error_message: Syntax error string if invalid, None otherwise.
    """
    if not path.endswith(".py"):
        return True, None
    try:
        ast.parse(content)
        return True, None
    except SyntaxError as e:
        return False, str(e)


class Spinner:
    """Animated terminal spinner for showing progress.

    Displays a rotating spinner with pulsing color effect in a background thread.
    """

    def __init__(self, label: str = "localcode") -> None:
        """Initialize spinner.

        Args:
            label: Label text (currently unused, kept for compatibility).
        """
        self.label: str = label
        self.stop_event: threading.Event = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the spinner animation in a background thread."""
        def spin() -> None:
            print()
            chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i = 0
            while not self.stop_event.is_set():
                wave = (math.sin(time.time() * 4) + 1) / 2
                rgb_val = int(100 + wave * 155)
                color_code = f"\033[1m\033[38;2;{rgb_val};{rgb_val};255m"
                reset_code = "\033[0m"
                print(f"\r{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} {color_code}{chars[i % len(chars)]}{reset_code} ", end="", flush=True)
                i += 1
                time.sleep(0.08)

        self.thread = threading.Thread(target=spin, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the spinner animation and clean up."""
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        print("\r", end="", flush=True)


class LocalCode:
    """Core agent orchestrating the interactive coding session.

    Handles repo context, tool execution (read/edit/run/commit),
    llama.cpp server communication, and the REPL loop while following
    strict safety and precision rules.
    """

    def __init__(self) -> None:
        """Initialize LocalCode agent.

        Sets up repository context, checks llama.cpp server connectivity,
        and starts the browser bridge server.
        """
        self.repo_root: str = run("git rev-parse --show-toplevel") or os.getcwd()
        self.pending_notes: List[str] = []
        self.messages: List[Dict[str, Any]] = []  # Conversation history for llama.cpp
        self.last_usage: Optional[Dict[str, Any]] = None
        self.total_tokens: int = 0
        self._tokens_estimated: bool = False  # True if tokens are estimated (not from API)
        self.bridge_port: int = self._get_bridge_port()
        self._map_cache: Dict[Tuple[Optional[str], bool], str] = {}
        self._map_mtime: Dict[Tuple[Optional[str], bool], float] = {}
        self._initial_context_sent: bool = False  # Track if initial context was sent
        self.auto_approve: bool = False  # Auto-approve all commands when True
        self._is_summarizing: bool = False  # Prevent recursive summarization

        self._check_llama_server()
        self._start_bridge_if_needed()

    def _check_llama_server(self) -> None:
        """Check if llama.cpp server is reachable.

        Prints connection status and helpful instructions if server is unavailable.
        """
        try:
            req = urllib.request.Request(
                f"{LLAMA_HOST}/health",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print(styled(f"✓ llama.cpp server connected at {LLAMA_HOST}", "32m"))
                    return
        except Exception:
            pass
        print(styled(f"⚠ Warning: llama.cpp server not reachable at {LLAMA_HOST}", "93m"))
        print(styled("  Start it with: llama-server -m <model.gguf> --port 8080", "90m"))
        print(styled("  Or set LLAMA_HOST environment variable", "90m"))

    def get_repo_map(self, pattern: Optional[str] = None, include_details: bool = True) -> str:
        """Get repository map with optional pattern filtering.

        Args:
            pattern: Optional glob pattern to filter files.
            include_details: If true, include line numbers for Python elements.

        Returns:
            Formatted repository map string.
        """
        # Cache key based on parameters
        cache_key = (pattern, include_details)
        if not hasattr(self, '_map_cache'):
            self._map_cache: Dict[Tuple[Optional[str], bool], str] = {}
        if not hasattr(self, '_map_mtime'):
            self._map_mtime: Dict[Tuple[Optional[str], bool], float] = {}
        
        # Check for any file system changes
        root_path = Path(self.repo_root)
        current_mtime = root_path.stat().st_mtime if root_path.exists() else 0.0
        
        cached_mtime = self._map_mtime.get(cache_key, -1)
        if cache_key in self._map_cache and abs(current_mtime - cached_mtime) < 0.1:
            return self._map_cache[cache_key]

        self._map_cache[cache_key] = get_map(self.repo_root, pattern, include_details)
        self._map_mtime[cache_key] = current_mtime
        return self._map_cache[cache_key]

    def _get_bridge_port(self) -> int:
        """Find an available port for the browser bridge.

        Starts from DEFAULT_BRIDGE_PORT (or LOCALCODE_BRIDGE_PORT env var)
        and searches for the first unused port.

        Returns:
            Available port number.
        """
        port = int(os.getenv("LOCALCODE_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT)))
        for p in range(port, port + 20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", p)) != 0:
                    return p
        return port

    def _start_bridge_if_needed(self) -> None:
        """Start integrated bridge server in a daemon thread."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", self.bridge_port)) == 0:
                    print(styled(f"✓ Bridge already running on {self.bridge_port}", "32m"))
                    return
        except Exception:
            pass

        def run_bridge():
            server_address = ("localhost", self.bridge_port)
            # Allow reuse to avoid "Address already in use" errors (TIME_WAIT etc.)
            class ReuseTCPServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
            httpd = ReuseTCPServer(server_address, BridgeHandler)
            httpd.daemon_threads = True
            print(styled(f"✓ Bridge listening on {self.bridge_port} (threaded)", "32m"))
            try:
                httpd.serve_forever()
            except:
                pass

        thread = threading.Thread(target=run_bridge, daemon=True)
        thread.start()
        time.sleep(0.5)  # allow startup

    def llama_request(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """Send a chat completion request to llama.cpp server."""
        # Check if we need to summarize history in auto mode
        if self.auto_approve and self.total_tokens > SUMMARY_TOKEN_THRESHOLD and not self._is_summarizing:
            self.summarize_history()
        
        # Convert tools to OpenAI format expected by llama.cpp
        openai_tools = None
        if tools:
            openai_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.get("name"),
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {}),
                        }
                    })

        payload: Dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "stream": False,
        }
        
 
        if openai_tools:
            payload["tools"] = openai_tools
            payload["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{LLAMA_HOST}/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"localcode/{VERSION}",
            },
            data=json.dumps(payload).encode(),
        )

        spinner = Spinner()
        spinner.start()
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                self.last_usage = body.get("usage")
                if self.last_usage:
                    prompt_tokens = self.last_usage.get("prompt_tokens", 0)
                    completion_tokens = self.last_usage.get("completion_tokens", 0)
                    # API prompt_tokens is the full conversation size, always replace
                    self.total_tokens = prompt_tokens
                    self._tokens_estimated = False  # Reset when we get real API data
                spinner.stop()
                print(f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} {styled('✓', '32m')} {styled(f'input tokens: {prompt_tokens:,}', '90m')}\n")
                return body
        except urllib.error.HTTPError as e:
            spinner.stop()
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:1200]
            except Exception:
                pass
            print(styled(f"HTTP {e.code}: {e.reason}", "31m"))
            if error_body:
                print(styled(error_body, "31m"))
            return None
        except urllib.error.URLError as e:
            spinner.stop()
            print(styled(f"Connection error: {e.reason}", "31m"))
            print(styled(f"Is llama-server running at {LLAMA_HOST}?", "93m"))
            return None
        except KeyboardInterrupt:
            spinner.stop()
            print(styled("[user interrupted]", "93m"))
            return None
        except Exception as e:
            spinner.stop()
            print(styled(f"Err: {e}", "31m"))
            return None

    def build_user_message(self, request: str) -> str:
        """Build the user message content with context.
        
        Minimal context: system summary sent once, time always included.
        Agent uses get_repo_map tool and shell commands (cat/grep) to read files.
        """
        now = datetime.datetime.now().astimezone()
        day = now.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        current_time = now.strftime(f"%A {day}{suffix} of %B %Y, %H:%M %Z")

        parts = []
        
        # Send system summary only once
        if not self._initial_context_sent:
            parts.append(f"### System Summary\n{json.dumps(system_summary(), separators=(',', ':'))}")
            self._initial_context_sent = True

        if self.pending_notes:
            parts.append("### Extra Context\n" + "\n\n".join(self.pending_notes))
            self.pending_notes.clear()

        global _bridge_state
        if _bridge_state.get("url"):
            parts.append(f"### Browser State\nURL: {_bridge_state.get('url')}\nTitle: {_bridge_state.get('title', '')}")

        parts.append(f"### Current Time\n{current_time}")
        parts.append(f"### Request\n{request}")

        return "\n\n".join(parts)

    def get_messages_with_system(self) -> List[Dict[str, Any]]:
        """Return messages list with system prompt prepended."""
        return [{"role": "system", "content": SYSTEM_PROMPT}] + self.messages

    def summarize_history(self) -> None:
        """Summarize conversation history to reduce token usage.
        
        Keeps last 3 messages intact, summarizes everything before that,
        and replaces the old messages with the summary.
        """
        if len(self.messages) <= 3:
            return  # Not enough history to summarize
        
        # Keep last 3 messages intact
        recent_messages = self.messages[-3:]
        history_to_summarize = self.messages[:-3]
        
        # Build summary prompt
        history_text = "\n\n".join([
            f"## {'User' if m['role'] == 'user' else 'Assistant'}\n{m.get('content', '')}"
            for m in history_to_summarize
        ])
        
        summary_prompt = f"""Summarize the following conversation history for a coding assistant. Extract only the essential information needed to continue the work:

**CRITICAL - PRESERVE THE LONG-TERM GOAL:**
The most important thing to capture is the OVERARCHING OBJECTIVE - what is the main project/task we're working towards? This may span multiple sessions. Include:
- The primary goal and scope of the project
- What has been accomplished so far (milestones reached)
- What remains to be done (remaining scope)
- Any constraints or requirements that must be maintained

**PRESERVE:**
- Current project/repository being worked on
- Active task or goal (what we're building/fixing right now)
- Key files being modified (paths and what changes were made)
- Important decisions made (architecture, design choices)
- Any code patterns or snippets that are still relevant
- Pending tasks or unresolved issues
- Recent shell commands and their important outputs

**DISCARD:**
- Completed tasks that are done
- Old file reads that aren't referenced anymore
- Explained concepts that were already understood
- Previous iterations of the same code
- General chitchat or greetings

Format the summary as:

### Long-Term Goal
[The overarching objective - what are we ultimately trying to accomplish?]

### Project Context
[Brief description of what we're working on]

### Progress So Far
[What milestones have been completed]

### Current Goal
[What we need to do next]

### Remaining Work
[What still needs to be done to complete the overall goal]

### Key Files
- [file path]: [what was changed/why it matters]

### Important Decisions
- [decision 1]
- [decision 2]

### Pending Items
- [item 1]
- [item 2]

### Relevant Code/Commands
[Any critical code snippets or command outputs]

---
Conversation History to Summarize:
{history_text}
"""
        
        # Send summary request
        summary_messages = [
            {"role": "system", "content": "You are a concise summarizer. Extract only essential information."},
            {"role": "user", "content": summary_prompt}
        ]
        
        # Prevent recursive summarization
        self._is_summarizing = True
        try:
            response = self.llama_request(summary_messages)
        finally:
            self._is_summarizing = False
        if not response:
            print(styled("⚠ Failed to summarize history, keeping original", "93m"))
            return
        
        summary_text = self.extract_text(response)
        
        # Replace history with summary
        self.messages = [
            {"role": "user", "content": f"### Conversation Summary\n\n{summary_text}\n\n---\n*History summarized to reduce token usage*"}
        ] + recent_messages
        
        # Save old token count before resetting
        old_tokens = self.total_tokens
        
        # Calculate actual token count after summarization
        self.total_tokens = self._estimate_tokens_from_messages()
        self._tokens_estimated = True
        
        new_tokens = self.total_tokens
        
        # Display summary in distinct color
        print()
        print(styled("=" * 60, "35m"))
        print(styled("📝 CONVERSATION SUMMARY", "1;35m"))
        print(styled("=" * 60, "35m"))
        print(styled(summary_text, "35m"))
        print(styled("=" * 60, "35m"))
        print(styled(f"✓ Summarized history (was ~{old_tokens:,} tokens, now ~{new_tokens:,})", "1;32m"))
        
        # Save summary to log file
        try:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = f"conversation_summary_{timestamp}.txt"
            with open(log_file, "w", encoding="utf-8") as f:
                f.write(f"Conversation Summary - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Tokens before: ~{old_tokens:,}\n")
                f.write(f"Tokens after: ~{new_tokens:,}\n")
                f.write("=" * 60 + "\n\n")
                f.write(summary_text)
            print(styled(f"💾 Summary saved to: {log_file}", "93m"))
        except Exception as e:
            print(styled(f"⚠ Could not save summary log: {e}", "93m"))


    def extract_text(self, response: Dict[str, Any]) -> str:
        """Extract text content from OpenAI-compatible response."""
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return message.get("content", "") or ""

    def extract_reasoning_content(self, response: Dict[str, Any]) -> str:
        """Extract reasoning_content from OpenAI-compatible response."""
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return message.get("reasoning_content", "") or ""

    def extract_tool_calls(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract tool calls from OpenAI-compatible response."""
        choices = response.get("choices", [])
        if not choices:
            return []
        message = choices[0].get("message", {})
        return message.get("tool_calls", []) or []

    def get_finish_reason(self, response: Dict[str, Any]) -> str:
        """Get finish reason from response."""
        choices = response.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("finish_reason", "") or ""

    def print_assistant_text(self, text: str) -> None:
        if not text:
            return
        print(render_md(text))
        print()

    def tool_get_repo_map(self, args: Dict[str, Any]) -> Dict[str, Any]:
        pattern = args.get("pattern", "")
        include_details = args.get("include_details", True)
        
        result = self.get_repo_map(pattern if pattern else None, include_details)
        
        # Count files in output (lines that are file paths, not comments or indented)
        file_count = len([
            line for line in result.split("\n") 
            if line and not line.startswith("#") and not line.startswith("  ") and not line.startswith("Excluded:")
        ])
        
        print(styled(f"Repository map ({file_count} files)", "36m"))
        print(result)
        
        return {"ok": True, "file_count": file_count}

 

    def tool_write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args["path"]
        content = args["content"]
        overwrite = args.get("overwrite", False)
        
        try:
            p = safe_repo_path(self.repo_root, path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        
        # Check if file exists
        if p.exists():
            if not overwrite:
                return {"ok": False, "error": "file already exists (set overwrite=true to replace)"}
            
            # Confirm overwrite (unless auto_approve is enabled)
            if self.auto_approve:
                answer = "y"
            else:
                print(styled(f"⚠ {path} already exists. Overwrite? (y/n): ", "93m"), end="")
                sys.stdout.flush()
                try:
                    answer = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    answer = "n"
            
            if answer != "y":
                return {"ok": False, "error": "user cancelled overwrite"}

        ok, lint_error = lint_py(path, content)
        if not ok:
            print(styled(f"Lint Fail {path}: {lint_error}", "31m"))
            return {"ok": False, "error": f"python syntax error: {lint_error}"}

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            for ln in content.splitlines():
                print(styled(f"+{ln}", "32m"))
            print(styled(f"Created {path}", "32m"))
            return {"ok": True, "path": path}
        except (PermissionError, OSError) as e:
            return {"ok": False, "error": str(e)}

    def tool_edit_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        path = args["path"]
        find = args["find"]
        replace = args["replace"]

        try:
            p = safe_repo_path(self.repo_root, path)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if not p.exists():
            return {"ok": False, "error": "file not found"}

        content, error = safe_read_file(path, self.repo_root, confirm_large=False)
        if error:
            return {"ok": False, "error": error}
        if content == "[empty]":
            content = ""

        if find not in content:
            return {"ok": False, "error": "exact find text not found"}

        new_content = content.replace(find, replace, 1)
        ok, lint_error = lint_py(path, new_content)
        if not ok:
            print(styled(f"Lint Fail {path}: {lint_error}", "31m"))
            return {"ok": False, "error": f"python syntax error: {lint_error}"}

        if new_content == content:
            return {"ok": False, "error": "no-op edit"}

        diff_lines = list(
            difflib.unified_diff(
                content.splitlines(),
                new_content.splitlines(),
                fromfile=path,
                tofile=path,
                lineterm="",
            )
        )
        for d in diff_lines:
            if d.startswith(("---", "+++")):
                continue
            color = "32m" if d.startswith("+") else "31m" if d.startswith("-") else "0m"
            print(styled(d, color))

        try:
            p.write_text(new_content)
            
            # Count lines changed
            added = sum(1 for d in diff_lines if d.startswith("+") and not d.startswith("+++") )
            removed = sum(1 for d in diff_lines if d.startswith("-") and not d.startswith("---"))
            
            print(styled(f"Applied {path} (+{added} -{removed})", "32m"))
            return {"ok": True, "path": path, "lines_added": added, "lines_removed": removed}
        except (PermissionError, OSError) as e:
            return {"ok": False, "error": str(e)}

    def tool_run_shell_command(self, args: Dict[str, Any]) -> Dict[str, Any]:
        cmd = args["command"].strip()
        if not cmd:
            return {"ok": False, "error": "empty command"}

        # Auto-approve safe read-only commands or when auto_approve is enabled
        if is_safe_read_command(cmd) or self.auto_approve:
            print(f"{styled(f'$ {cmd}', '90m')}")
            answer = "y"
        else:
            print(f"{styled('[APPROVE] $ ' + cmd, '48;5;236;37m')}")
            title(f"⏳ {APP_NAME}")
            print(styled("(y/n): ", "1m"), end="")
            sys.stdout.flush()
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                answer = "n"

        if answer != "y":
            return {"ok": False, "denied": True, "error": "user denied"}

        try:
            # For auto-approved commands, don't stream output live - only show smart-truncated version
            stream_output = not is_safe_read_command(cmd)
            output_lines, exit_code = run_shell_interactive(cmd, stream_output=stream_output)
            
            # For auto-approved commands, print smart-truncated output to terminal
            # but send full output to LLM
            if is_safe_read_command(cmd):
                # Smart truncate: first line, line count, last line
                terminal_output = "\n".join(smart_truncate(output_lines, keep_first=1, keep_last=1, max_line_len=80))
                print(styled(terminal_output, "90m"))
            else:
                print(f"{styled(f'$ {cmd}', '90m')}")
            
            # Always send full (but reasonably truncated) output to LLM
            return {
                "ok": True,
                "command": cmd,
                "exit_code": exit_code,
                "output": "\n".join(truncate(output_lines)),
            }
        except Exception as e:
            return {"ok": False, "command": cmd, "error": str(e)}

    def tool_commit_changes(self, args: Dict[str, Any]) -> Dict[str, Any]:
        message = args["message"].strip()
        if not message:
            return {"ok": False, "error": "empty commit message"}

        # Validate repo_root to prevent command injection
        try:
            safe_repo_root = validate_path_for_shell(self.repo_root)
        except ValueError as e:
            return {"ok": False, "error": f"Invalid repo path: {e}"}

        # Use subprocess with list arguments instead of shell commands
        try:
            # Check git status
            status_result = subprocess.run(
                ["git", "-C", safe_repo_root, "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if not status_result.stdout.strip():
                return {"ok": False, "error": "nothing to commit"}

            # Stage all changes
            add_result = subprocess.run(
                ["git", "-C", safe_repo_root, "add", "-A"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if add_result.returncode != 0:
                return {"ok": False, "error": f"git add failed: {add_result.stderr.strip()}"}

            # Commit changes
            commit_result = subprocess.run(
                ["git", "-C", safe_repo_root, "commit", "-m", message],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if commit_result.returncode != 0:
                return {"ok": False, "error": f"git commit failed: {commit_result.stderr.strip()}"}
            
            output = commit_result.stdout.strip()
            print(styled(output, "32m"))
            
            # Automatically compress after successful commit (unit of work is complete)
            self.cmd_compress()
            
            return {"ok": True, "message": message, "git": output}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git command timed out"}
        except Exception as e:
            return {"ok": False, "error": f"git error: {e}"}

    def tool_browser_execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        code = args.get("code", "").strip()
        if not code:
            return {"ok": False, "error": "empty code"}

        print(f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} wants to execute in browser:")
        print(f"  {styled(code, '48;5;236;37m')}")
        title(f"⏳ {APP_NAME} (browser)")

        try:
            req = urllib.request.Request(
                f"http://localhost:{self.bridge_port}/execute",
                headers={"Content-Type": "application/json"},
                data=json.dumps({"code": code}).encode(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode())
            print(styled("✓ Sent to browser extension", "32m"))
            return {"ok": True, "code": code[:80] + "...", "status": "executed", "result": result}
        except Exception as e:
            return {"ok": False, "error": f"bridge not reachable (port {self.bridge_port}): {e}. Make sure Chrome extension is loaded + popup port matches."}

    def execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "get_repo_map":
            return self.tool_get_repo_map(args)
        if name == "write_file":
            return self.tool_write_file(args)
        if name == "edit_file":
            return self.tool_edit_file(args)
        if name == "run_shell_command":
            return self.tool_run_shell_command(args)
        if name == "commit_changes":
            return self.tool_commit_changes(args)
        if name == "browser_execute":
            return self.tool_browser_execute(args)
        return {"ok": False, "error": f"unknown tool: {name}"}

    def run_agent_turn(self, request: str) -> None:
        # Add user message to conversation history
        user_content = self.build_user_message(request)
        self.messages.append({"role": "user", "content": user_content})

        response = self.llama_request(self.get_messages_with_system(), TOOLS)
        if not response:
            return

        while True:

            text = self.extract_text(response)
            reasoning = self.extract_reasoning_content(response)
            tool_calls = self.extract_tool_calls(response)
            finish_reason = self.get_finish_reason(response)

            # Add assistant message to history
            assistant_msg: Dict[str, Any] = {"role": "assistant"}
            if text:
                assistant_msg["content"] = text
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            if text or tool_calls:
                self.messages.append(assistant_msg)

            # Print reasoning content first if present (thinking output)
            if reasoning and reasoning.strip():
                print(styled("Thinking:", "36m"))
                print(styled(reasoning, "90m"))
                print()

            if text:
                self.print_assistant_text(text)

            # If no tool calls or finish_reason is "stop", we're done
            if not tool_calls or finish_reason == "stop":
                return

            # Process tool calls
            for call in tool_calls:
                call_id = call.get("id", "")
                function = call.get("function", {})
                name = function.get("name", "")
                raw_args = function.get("arguments", "{}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception as e:
                    result = {"ok": False, "error": f"invalid JSON arguments: {e}"}
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result),
                    })
                    continue

                # Format tool call display in a user-friendly way
                display_args = format_tool_call_display(name, args)
                print(
                    f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')} "
                    f"{styled(name, '1;36m')} "
                    f"{styled(display_args, '90m')}"
                )
                result = self.execute_tool(name, args)
                
                # Add tool result to messages
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            # Continue conversation with tool results
            response = self.llama_request(self.get_messages_with_system(), TOOLS)
            if not response:
                return

    def cmd_add(self, pattern: str) -> None:
        """Show files matching pattern (for reference, agent should use shell commands)."""
        found = glob.glob(pattern, root_dir=self.repo_root, recursive=True)
        files = [f for f in found if Path(self.repo_root, f).is_file()]
        print(styled(f"Files matching '{pattern}':", "36m"))
        for f in sorted(files)[:50]:
            print(f"  {f}")
        if len(files) > 50:
            print(f"  ... and {len(files) - 50} more")
        print(f"\nTotal: {len(files)} files")
        print("Tip: Use 'cat file.py', 'grep pattern file.py', or 'sed' to read files. Use edit_file for modifications.")

    def cmd_ctx(self) -> None:
        """Show current context status for the AI."""
        print(styled("=== Context Status ===", "1;36m"))
        print(styled(f"Initial context sent: {'Yes' if self._initial_context_sent else 'No'}", "90m"))
        print(styled(f"Repo map cache entries: {len(self._map_cache)}", "90m"))
        print(styled(f"Conversation messages: {len(self.messages)}", "90m"))
        print()

    def cmd_compress(self) -> None:
        """Compress conversation by truncating large tool outputs.
        
        Replaces large outputs with summaries while preserving tool metadata.
        - Repo maps: Replace with file count note
        - Shell commands: Keep exit code, truncate output to last 10 lines
        - File reads: Keep first/last 5 lines, compress middle
        - Other tools: Keep metadata, truncate large outputs
        """
        compressed_count = 0
        bytes_saved = 0
        
        for msg in self.messages:
            if msg.get("role") != "tool":
                continue
            
            content = msg.get("content", "")
            if not content:
                continue
            
            old_size = len(content)
            
            try:
                result = json.loads(content)
                
                # Check if this was a repo map tool call
                if result.get("ok") and "file_count" in result and "output" not in result:
                    # This is likely a repo map result - compress it
                    file_count = result.get("file_count", 0)
                    result = {
                        "ok": True,
                        "compressed": True,
                        "note": f"Repository map compressed (showed {file_count} files)"
                    }
                    msg["content"] = json.dumps(result)
                    compressed_count += 1
                    bytes_saved += old_size - len(msg["content"])
                    continue
                
                # Handle shell command outputs
                if result.get("ok") and "output" in result:
                    output = result["output"]
                    if isinstance(output, str) and len(output) > 500:
                        lines = output.split("\n")
                        if len(lines) > 20:
                            # Keep first 5 and last 5 lines
                            kept_lines = lines[:5] + [f"  ... ({len(lines) - 10} lines compressed) ..."] + lines[-5:]
                            result["output"] = "\n".join(kept_lines)
                            result["compressed"] = True
                            msg["content"] = json.dumps(result)
                            if old_size > len(msg["content"]):
                                compressed_count += 1
                                bytes_saved += old_size - len(msg["content"])
                
                # Handle file read/write operations with large content
                elif "content" in result and isinstance(result["content"], str):
                    if len(result["content"]) > 1000:
                        lines = result["content"].split("\n")
                        if len(lines) > 20:
                            kept_lines = lines[:5] + [f"  ... ({len(lines) - 10} lines compressed) ..."] + lines[-5:]
                            result["content"] = "\n".join(kept_lines)
                            result["compressed"] = True
                            msg["content"] = json.dumps(result)
                            if old_size > len(msg["content"]):
                                compressed_count += 1
                                bytes_saved += old_size - len(msg["content"])
                
                # Generic compression for any large output field
                elif old_size > 2000:
                    # Truncate the entire JSON if it's very large
                    result["compressed"] = True
                    result["note"] = f"Tool result compressed ({old_size:,} bytes)"
                    msg["content"] = json.dumps(result)
                    compressed_count += 1
                    bytes_saved += old_size - len(msg["content"])
                    
            except (json.JSONDecodeError, KeyError, TypeError):
                # If we can't parse it, leave it alone
                pass
        
        if compressed_count == 0:
            print(styled("No tool outputs large enough to compress.", "93m"))
        else:
            print(styled(f"Compressed {compressed_count} tool result(s)", "32m"))
            print(styled(f"Saved ~{bytes_saved:,} bytes", "90m"))
            # Recalculate token estimate based on compressed content
            self.total_tokens = self._estimate_tokens_from_messages()
            self._tokens_estimated = True
            print(styled(f"Estimated tokens after compression: ~{self.total_tokens:,}", "90m"))

    def _estimate_tokens_from_messages(self) -> int:
        """Estimate total tokens from current messages using bytes/4 heuristic.
        
        Returns:
            Estimated token count based on message content size.
        """
        total_bytes = 0
        for msg in self.messages:
            content = msg.get("content", "")
            if content:
                total_bytes += len(content)
        # Rough estimate: ~4 bytes per token for English text
        return max(0, total_bytes // 4)

    def cmd_status(self) -> None:
        print(styled(f"Repository: {self.repo_root}", "36m"))
        print(styled(f"Bridge: integrated (port {self.bridge_port})", "36m"))
        print(styled(f"Server: {LLAMA_HOST}", "36m"))
        global _bridge_state
        if _bridge_state.get("url"):
            print(styled(f"Browser: {_bridge_state.get('title', 'No title')} @ {_bridge_state.get('url')}", "36m"))
        if self.messages:
            print(styled(f"Conversation: {len(self.messages)} messages", "32m"))
        print(styled(f"Model: {MODEL}", "90m"))
        print(styled(f"Total tokens: ~{self.total_tokens:,}", "90m"))

    def shell_user_command(self, shell_cmd: str) -> None:
        output_lines, exit_code = run_shell_interactive(shell_cmd)
        title(f"❓ {APP_NAME}")
        try:
            answer = input("\aAdd to context? [t]runcated/[f]ull/[n]o: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"

        if answer in ("t", "f"):
            body = "\n".join(truncate(output_lines) if answer == "t" else output_lines)
            self.pending_notes.append(f"$ {shell_cmd}\n{body}")
            print(styled("Added to context", "93m"))

    def repl(self) -> None:
        print(
            f"{styled('local', '48;2;80;80;200;37m')}{styled('code', '48;2;60;60;180;97m')}"
            f" {styled(' ' + MODEL + ' ', '48;5;236;37m')}"
            f" {styled(' ' + LLAMA_HOST + ' ', '48;5;236;90m')}"
            f" {styled(' ctrl+d to send ', '48;5;236;37m')}"
        )

        while True:
            title(f"❓ {APP_NAME}")
            last = self.last_usage or {}
            
            # Use estimated tokens after compression, otherwise use actual API tokens
            if self._tokens_estimated:
                prompt_tokens = self.total_tokens
                completion_tokens = 0
                token_label = "est"
            else:
                prompt_tokens = last.get("prompt_tokens", 0)
                completion_tokens = last.get("completion_tokens", 0)
                token_label = "actual"

            # Status line for local models (no cost, just tokens)
            print(
                styled(f"input: {prompt_tokens:,}" + (" [est]" if self._tokens_estimated else ""), "90m")
                + styled(" • ", "2;90m")
                + styled(f"msgs: {len(self.messages)}", "2;90m")
            )
            print(f"\a{styled('❯ ', '48;2;60;60;180;37m')}", end="", flush=True)
            input_lines = []
            try:
                while True:
                    input_lines.append(input())
            except EOFError:
                if not input_lines:
                    print("\nGoodbye!")
                    title("")
                    break
            except KeyboardInterrupt:
                print()
                continue

            user_input = "\n".join(input_lines).strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                command, _, arg = user_input.partition(" ")
                if command == "/exit":
                    print("Bye!")
                    title("")
                    break
                elif command == "/add":
                    self.cmd_add(arg)
                elif command == "/clear":
                    self.messages.clear()
                    self.pending_notes.clear()
                    self.total_tokens = 0
                    self._tokens_estimated = False
                    self._initial_context_sent = False  # Reset context tracking
                    self._map_cache.clear()
                    self._map_mtime.clear()
                    print("Conversation cleared (context tracking reset).")
                elif command == "/undo":
                    out = run(f"git -C {self.repo_root} reset --hard HEAD~1")
                    if out:
                        print(out)
                elif command == "/ctx":
                    self.cmd_ctx()
                elif command == "/status":
                    self.cmd_status()
                elif command == "/compress":
                    self.cmd_compress()
                elif command == "/auto":
                    self.auto_approve = not self.auto_approve
                    print(f"Auto-approve {'enabled' if self.auto_approve else 'disabled'} (use /auto to toggle)")
                elif command == "/summary":
                    self.summarize_history()
                elif command == "/help":
                    print("/add <glob> - List files matching pattern")
                    print("/ctx - Show context status")
                    print("/status - Show repo info")
                    print("/compress - Compress large tool outputs (repo maps, shell outputs, etc.)")
                    print("/clear - Clear conversation")
                    print("/undo - Undo commit")
                    print("/auto - Toggle auto-approve (skip y/n prompts)")
                    print("/summary - Summarize conversation history (auto at 75k tokens in auto mode)")
                    print("/exit - Exit")
                    print("!<cmd> - Shell command")
                    print()
                    print("Tools:")
                    print("  get_repo_map - Show repo structure with line numbers")
                    print("  write_file - Create/overwrite files")
                    print("  edit_file - Find/replace edits")
                    print("  run_shell_command - Run shell commands (cat, grep, etc.)")
                    print("  commit_changes - Git commit")
                    print("  browser_execute - Run JS in browser")
                continue

            if user_input.startswith("!"):
                shell_cmd = user_input[1:].strip()
                if shell_cmd:
                    self.shell_user_command(shell_cmd)
                continue

            self.run_agent_turn(user_input)


# === Integrated Browser Bridge ===
_bridge_pending: str | None = None
_bridge_result: dict | None = None
_bridge_state: Dict[str, Any] = {"url": "", "title": "", "timestamp": 0}
_bridge_pending_time: float = 0.0


class BridgeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/command'):
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            global _bridge_pending, _bridge_pending_time
            if _bridge_pending and (time.time() - _bridge_pending_time < 30):
                resp = {'command': 'execute', 'code': _bridge_pending}
                _bridge_pending = None
                _bridge_pending_time = 0.0
                self.wfile.write(json.dumps(resp).encode('utf-8'))
            else:
                self.wfile.write(json.dumps({}).encode('utf-8'))
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Accept')
        self.end_headers()

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8')) if content_length > 0 else {}

            global _bridge_pending, _bridge_result, _bridge_state, _bridge_pending_time

            if self.path.startswith('/execute'):
                _bridge_pending = data.get('code', '')
                _bridge_pending_time = time.time()
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                start_time = time.time()
                while time.time() - start_time < 12:
                    if _bridge_result is not None:
                        result = _bridge_result
                        _bridge_result = None
                        _bridge_pending = None
                        _bridge_pending_time = 0.0
                        self.wfile.write(json.dumps(result).encode('utf-8'))
                        return
                    time.sleep(0.3)
                self.wfile.write(json.dumps({
                    'ok': False,
                    'error': 'timeout waiting for browser response'
                }).encode('utf-8'))
                _bridge_pending = None
                _bridge_pending_time = 0.0

            elif self.path.startswith('/result'):
     
                _bridge_result = data
                _bridge_pending = None
                _bridge_pending_time = 0.0
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'ok'}).encode('utf-8'))

            elif self.path.startswith('/update'):
                _bridge_state.clear()
                _bridge_state.update(data)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'status': 'updated'}).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

    def log_message(self, format: str, *args: str) -> None:
        pass  # quiet


def main() -> None:
    LocalCode().repl()


def commit_changes(message: str) -> dict:
    """Standalone commit function for tool usage."""
    repo_root_raw = run("git rev-parse --show-toplevel") or os.getcwd()
    
    if not message or not message.strip():
        return {"ok": False, "error": "empty commit message"}
    
    # Validate repo_root to prevent command injection
    try:
        safe_repo_root = validate_path_for_shell(repo_root_raw)
    except ValueError as e:
        return {"ok": False, "error": f"Invalid repo path: {e}"}
    
    # Use subprocess with list arguments instead of shell commands
    try:
        # Check git status
        status_result = subprocess.run(
            ["git", "-C", safe_repo_root, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not status_result.stdout.strip():
            return {"ok": False, "error": "nothing to commit"}
        
        # Stage all changes
        add_result = subprocess.run(
            ["git", "-C", safe_repo_root, "add", "-A"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if add_result.returncode != 0:
            return {"ok": False, "error": f"git add failed: {add_result.stderr.strip()}"}
        
        # Commit changes
        commit_result = subprocess.run(
            ["git", "-C", safe_repo_root, "commit", "-m", message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if commit_result.returncode != 0:
            return {"ok": False, "error": f"git commit failed: {commit_result.stderr.strip()}"}
        
        output = commit_result.stdout.strip()
        print(styled(output, "32m"))
        return {"ok": True, "message": message, "git": output}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git command timed out"}
    except Exception as e:
        return {"ok": False, "error": f"git error: {e}"}


if __name__ == "__main__":
    main()
