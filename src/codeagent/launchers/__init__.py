"""Tmux launcher — spawn and manage agent panes in tmux sessions.

This is a thin wrapper around tmux commands for launching worker agents
in dedicated panes. The actual agent protocol is handled by the
coordination subsystem (mailbox).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class PaneConfig:
    """Configuration for a tmux pane."""
    session: str = "agents"
    window: str = "main"
    shell: str = "zsh"
    cwd: str = ""
    env: Optional[dict[str, str]] = None


def send_keys(pane_target: str, text: str, enter: bool = True) -> bool:
    """Send keystrokes to a tmux pane."""
    cmd = ["tmux", "send-keys", "-t", pane_target, text]
    if enter:
        cmd.append("Enter")
    return subprocess.run(cmd, capture_output=True).returncode == 0


def capture_pane(pane_target: str, lines: int = 50) -> str:
    """Capture output from a tmux pane."""
    cmd = ["tmux", "capture-pane", "-t", pane_target, "-p", "-S", f"-{lines}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def create_pane(config: PaneConfig) -> Optional[str]:
    """Create a new tmux pane and return its target."""
    cmd = ["tmux", "split-window", "-t", f"{config.session}:{config.window}"]
    if config.cwd:
        cmd += ["-c", config.cwd]
    if config.shell:
        cmd += ["-P", "-F", "#{pane_id}", config.shell]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def kill_pane(pane_target: str) -> bool:
    """Kill a tmux pane."""
    return subprocess.run(
        ["tmux", "kill-pane", "-t", pane_target],
        capture_output=True,
    ).returncode == 0
