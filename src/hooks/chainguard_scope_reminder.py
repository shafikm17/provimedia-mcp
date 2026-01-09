#!/usr/bin/env python3
"""
CHAINGUARD Scope Reminder Hook

UserPromptSubmit Hook - reminds Claude to set scope BEFORE starting work.
This hook ensures that chainguard_set_scope() is called at the beginning of each task.

Copyright (c) 2026 Provimedia GmbH
Licensed under the Polyform Noncommercial License 1.0.0
See LICENSE file in the project root for full license information.

v1.0: Initial implementation
v1.1: Added skip patterns for conversational prompts
"""

import sys
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

# CHAINGUARD Home
CHAINGUARD_HOME = Path.home() / ".chainguard"
ENFORCEMENT_STATE_FILE = "enforcement-state.json"

# Reminder settings
REMINDER_COOLDOWN_MINUTES = 30  # Don't remind again within this window
REMINDER_CACHE_FILE = CHAINGUARD_HOME / "scope_reminder_cache.json"

# Prompts that should NOT trigger a scope reminder (conversational, short, or meta)
SKIP_PATTERNS = [
    # Short affirmations
    "ja", "yes", "ok", "okay", "nein", "no", "gut", "good", "danke", "thanks",
    "weiter", "continue", "stop", "halt", "abbrechen", "cancel",
    # Questions about chainguard itself
    "was ist chainguard", "what is chainguard", "hilfe", "help",
    # Status checks (these will use chainguard tools anyway)
    "status", "show status", "zeig status",
    # Git/commit related (separate workflow)
    "commit", "push", "pull", "merge",
]

# Minimum prompt length to trigger reminder
MIN_PROMPT_LENGTH = 15


def get_project_id(working_dir: str) -> str:
    """
    Berechnet die Project ID wie der MCP Server.

    Reihenfolge (identisch mit project_manager.py):
    1. Git Remote URL Hash
    2. Git Root Path Hash
    3. Working Dir Path Hash (Fallback)
    """
    import subprocess

    # 1. Try git remote
    try:
        result = subprocess.run(
            ["git", "-C", working_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return hashlib.sha256(result.stdout.strip().encode()).hexdigest()[:16]
    except Exception:
        pass

    # 2. Try git root
    try:
        result = subprocess.run(
            ["git", "-C", working_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return hashlib.sha256(result.stdout.strip().encode()).hexdigest()[:16]
    except Exception:
        pass

    # 3. Fallback: path hash
    resolved = str(Path(working_dir).resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def load_enforcement_state(working_dir: str) -> Optional[Dict[str, Any]]:
    """Lädt den Enforcement-State für ein Projekt."""
    project_id = get_project_id(working_dir)
    state_file = CHAINGUARD_HOME / "projects" / project_id / ENFORCEMENT_STATE_FILE

    if not state_file.exists():
        return None

    try:
        with open(state_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def has_active_scope(state: Optional[Dict[str, Any]]) -> bool:
    """Prüft ob ein aktiver Scope existiert."""
    if state is None:
        return False

    # Check if scope exists and has description
    scope = state.get("scope", {})
    if not scope:
        return False

    description = scope.get("description", "")
    return bool(description and description.strip())


def load_reminder_cache() -> Dict[str, Any]:
    """Lädt den Reminder-Cache."""
    if not REMINDER_CACHE_FILE.exists():
        return {}
    try:
        with open(REMINDER_CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_reminder_cache(cache: Dict[str, Any]):
    """Speichert den Reminder-Cache."""
    try:
        REMINDER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REMINDER_CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except IOError:
        pass


def should_skip_reminder(project_id: str) -> bool:
    """Prüft ob wir innerhalb des Cooldown-Fensters sind."""
    cache = load_reminder_cache()
    last_reminder = cache.get(project_id)

    if not last_reminder:
        return False

    try:
        last_time = datetime.fromisoformat(last_reminder)
        cooldown_end = last_time + timedelta(minutes=REMINDER_COOLDOWN_MINUTES)
        return datetime.now() < cooldown_end
    except (ValueError, TypeError):
        return False


def mark_reminder_sent(project_id: str):
    """Markiert dass eine Reminder gesendet wurde."""
    cache = load_reminder_cache()
    cache[project_id] = datetime.now().isoformat()

    # Cleanup old entries (keep last 50)
    if len(cache) > 50:
        sorted_entries = sorted(cache.items(), key=lambda x: x[1], reverse=True)
        cache = dict(sorted_entries[:50])

    save_reminder_cache(cache)


def should_skip_prompt(prompt: str) -> bool:
    """Prüft ob dieser Prompt übersprungen werden soll."""
    prompt_lower = prompt.lower().strip()

    # Too short
    if len(prompt_lower) < MIN_PROMPT_LENGTH:
        return True

    # Matches skip pattern
    for pattern in SKIP_PATTERNS:
        if prompt_lower == pattern or prompt_lower.startswith(pattern + " "):
            return True

    # Starts with slash command (Claude Code internal)
    if prompt_lower.startswith("/"):
        return True

    return False


def generate_reminder_message() -> str:
    """Generiert die Scope-Reminder-Nachricht."""
    return """
**CHAINGUARD Scope Reminder**

Kein aktiver Scope gefunden! Bitte zuerst aufrufen:

```python
chainguard_set_scope(
    description="Was du bauen willst",
    mode="programming",  # oder: content, devops, research
    working_dir="/pfad/zum/projekt"
)
```

**Warum?**
- Tracking von Dateiänderungen
- Automatische Syntax-Validierung
- Akzeptanzkriterien-Prüfung
- Projekt-Kontext für bessere Hilfe

**Modi:**
- `programming` (Default): Code, Bugs, Features
- `content`: Bücher, Artikel, Dokumentation
- `devops`: Server, CLI, WordPress
- `research`: Recherche, Analyse
""".strip()


def main():
    """Hauptfunktion - Scope Reminder Hook."""
    # Hook-Input von stdin lesen
    hook_input = {}
    if not sys.stdin.isatty():
        try:
            raw_input = sys.stdin.read()
            if raw_input.strip():
                hook_input = json.loads(raw_input)
        except json.JSONDecodeError:
            pass

    # Extrahiere relevante Daten
    prompt = hook_input.get("prompt", "")
    cwd = hook_input.get("cwd", str(Path.cwd()))

    # Skip conversational/short prompts
    if should_skip_prompt(prompt):
        sys.exit(0)

    # Projekt ID berechnen
    try:
        project_id = get_project_id(cwd)
    except Exception:
        sys.exit(0)

    # Enforcement-State laden
    state = load_enforcement_state(cwd)

    # Scope bereits aktiv?
    if has_active_scope(state):
        sys.exit(0)

    # Bereits kürzlich erinnert?
    if should_skip_reminder(project_id):
        sys.exit(0)

    # Reminder ausgeben
    print(generate_reminder_message())

    # Cooldown markieren
    mark_reminder_sent(project_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
