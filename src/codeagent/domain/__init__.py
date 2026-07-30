"""Domain contracts — data models shared across all layers."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOCAL_HOST_MARKER = "__local__"


@dataclass(frozen=True)
class HostSpec:
    """单台机器的配置。"""
    name: str
    ssh_alias: str
    hostnames: tuple[str, ...]
    description: str = ""
    shell_prefix: str = ""
    transport: str = "ssh"
    fallback_ssh_alias: str = ""


@dataclass(frozen=True)
class RepoEntry:
    """topic 下某一个代码仓。"""
    host: str
    path: str
    note: str = ""


@dataclass(frozen=True)
class TopicSpec:
    """单个主题的配置。"""
    name: str
    repos: tuple[RepoEntry, ...]
    description: str = ""

    def repo(self, index: int = 0) -> RepoEntry:
        if index < 0 or index >= len(self.repos):
            raise IndexError(
                f"topic '{self.name}' 只有 {len(self.repos)} 个 repo，index {index} 越界"
            )
        return self.repos[index]


@dataclass(frozen=True)
class RepoMap:
    """完整映射表。"""
    midocs_root: Path
    hosts: dict[str, HostSpec]
    topics: dict[str, TopicSpec]
    relay_zsh: str = ""

    def topic(self, name: str) -> TopicSpec:
        t = self.topics.get(name)
        if t is None:
            raise KeyError(f"未找到 topic：{name}")
        return t


@dataclass(frozen=True)
class Target:
    """路由解析结果。"""
    host: HostSpec
    repo: RepoEntry
    topic: Optional[TopicSpec] = None
    repo_index: int = 0
    is_local: bool = False

    @property
    def workdir(self) -> str:
        return self.repo.path

    @property
    def ssh_alias(self) -> str:
        return self.host.ssh_alias


@dataclass
class RunRequest:
    """一次执行请求。"""
    task: str
    workdir: str = ""
    backend: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    skills: Optional[str] = None
    skip_permissions: bool = True
    session_key: Optional[str] = None
    new_session: bool = False
    no_auto_resume: bool = False
    topic: Optional[str] = None
    repo_index: int = 0
    host: Optional[str] = None
    raw: bool = False


@dataclass
class RunResult:
    """执行结果。"""
    returncode: int
    stdout: str = ""
    stderr: str = ""
    session_id: Optional[str] = None
    backend: str = ""
    host: str = ""
    workdir: str = ""


@dataclass
class SessionRecord:
    """Session registry 中的一条记录。"""
    key: str
    session_id: str
    backend: str
    host: str
    workdir: str
    agent: str = ""
    model: str = ""
    topic: str = ""
    status: str = "active"  # active | failed | interrupted
    created_at: float = 0.0
    updated_at: float = 0.0


def current_hostname() -> str:
    """返回本机 hostname（不带域名）。"""
    name = socket.gethostname()
    return name.split(".", 1)[0]


def resolve_is_local(host: HostSpec, hostname: Optional[str] = None) -> bool:
    """判断是否本机。substring 匹配。"""
    actual = (hostname or current_hostname()).lower()
    for candidate in host.hostnames:
        c = candidate.lower()
        if c and c in actual:
            return True
    return False
