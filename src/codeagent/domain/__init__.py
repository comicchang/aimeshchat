"""Domain contracts — data models shared across all layers."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codeagent.constants import DEFAULT_EXEC_TIMEOUT
from codeagent.domain.runtime import RunContext

LOCAL_HOST_MARKER = "__local__"

__all__ = [
    "ExecutionSpec",
    "ModelContextUnavailable",
    "HostSpec",
    "RepoEntry",
    "TopicSpec",
    "RepoMap",
    "Target",
    "RunRequest",
    "RunResult",
    "RunContext",
    "SessionRecord",
    "current_hostname",
    "resolve_is_local",
]


class ModelContextUnavailable(RuntimeError):
    """Q5b: gateway 内调用但主 agent 无 runtime.context 时的明确失败。

    与 do_not_use（config.yml modelRoles.default）不同：default 模型必须
    来自当前调用者的真实运行上下文（runtime.context 机制），缺失时显式
    失败（MODEL_CONTEXT_UNAVAILABLE），不静默回落任何配置默认（mimo）。
    """


@dataclass(frozen=True)
class ExecutionSpec:
    """Q5: 不可变执行规格——去 role 化的核心数据结构。

    模型/提示词策略归 skill，aimeshchat 只保留执行/路由/会话/mailbox。
    ExecutionSpec 封装一次 oracle 启动所需的全部执行参数，构造后不可变。

    ``from_args(args)`` 从 CLI argparse.Namespace 构造，模型优先级
    （Q5b default 继承主 agent 模型）：
    1. 显式 --model/--variant/--system → 直接使用，不走任何解析。
    2. 否则 resolve_runtime_context(agent) → (model, variant[, provider])：
       继承当前调用者的 runtime.context（gateway 机制）；解析器抛
       ModelContextUnavailable 时原样上抛（明确报错，不静默回落）；
       返回 None 表示不适用（非 gateway 调用）。
    3. 仍无 → resolve_agent_model(agent) 回退 agent profile（向后兼容）。
    provider 优先取 runtime.context 显式上报值，否则从 model 前缀提取。
    """
    provider: str          # 模型供应商（从 model 前缀提取，如 openai/claude/ollama）
    model: str             # 完整模型标识（不含 variant）
    variant: str           # 模型变体（如 reasoning/thinking；空串=默认）
    system_prompt: str     # 系统提示词（空串=不设置）
    full_prompt: str       # 组合后的完整提示词（system_prompt + prompt）

    @classmethod
    def from_args(cls, args, *, resolve_agent_model=None,
                  resolve_runtime_context=None) -> "ExecutionSpec":
        """从 CLI args 构造 ExecutionSpec。

        模型优先级（Q5b default 继承主 agent 模型）：
        1. 显式 --model/--variant → 直接使用，不走任何解析。
        2. 否则调用 resolve_runtime_context(agent) 继承调用者当前模型：
           - 返回 (model, variant[, provider]) → 使用（--variant 未显式时
             继承 variant；provider 未显式上报时从 model 前缀提取）；
           - 抛 ModelContextUnavailable → 原样上抛（无上下文明确报错，
             不静默回落 mimo）；
           - 返回 None → 不适用，继续下一步。
        3. 仍无 model → 调用 resolve_agent_model(agent) 获取 profile
           model（向后兼容：非 gateway 内调用回退当前行为）。
        4. --system 为空时使用默认值（空串）。
        """
        model = getattr(args, "model", "") or ""
        variant = getattr(args, "variant", "") or ""
        system_prompt = getattr(args, "system", "") or ""
        prompt = getattr(args, "prompt", "") or ""
        agent = getattr(args, "agent", "") or ""

        # 1) 显式 --model 优先。
        # 2) Q5b: 未显式 --model 时继承调用者 runtime.context
        #    （resolve_runtime_context 由 CLI 层注入，负责 gateway 查询）。
        ctx_provider = ""
        if not model and resolve_runtime_context is not None:
            ctx = resolve_runtime_context(agent)
            if ctx:
                model = ctx[0] or ""
                ctx_variant = ctx[1] if len(ctx) > 1 else ""
                if len(ctx) > 2:
                    ctx_provider = ctx[2] or ""
                if not variant:
                    variant = ctx_variant or ""

        # 3) 仍无 model → agent profile（非 gateway 调用向后兼容路径）。
        if not model and resolve_agent_model is not None:
            model = resolve_agent_model(agent) or ""

        # provider：runtime.context 显式上报的优先；否则从 model 前缀提取。
        provider = ctx_provider or cls._extract_provider(model)

        # 组合 full_prompt：system_prompt 非空时前置
        if system_prompt and prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"
        elif system_prompt:
            full_prompt = system_prompt
        else:
            full_prompt = prompt

        return cls(
            provider=provider,
            model=model,
            variant=variant,
            system_prompt=system_prompt,
            full_prompt=full_prompt,
        )

    @staticmethod
    def _extract_provider(model: str) -> str:
        """从模型标识提取供应商前缀（openai/gpt-4 → openai）。"""
        if "/" in model:
            return model.split("/", 1)[0]
        return ""


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
    session_key: Optional[str] = None  # namespace for registry lookup (NOT backend session ID)
    new_session: bool = False
    no_auto_resume: bool = False
    topic: Optional[str] = None
    repo_index: int = 0
    host: Optional[str] = None
    raw: bool = False
    timeout: int = DEFAULT_EXEC_TIMEOUT
    resume_session_id: Optional[str] = None  # actual backend session ID for resume (passed by CLI from registry lookup)
    # v2 wire round-trip fields (Manager → remote_exec, preserved verbatim)
    request_id: str = ""
    run_id: str = ""
    review_key: str = ""
    require_ack: bool = False
    capabilities: tuple[str, ...] = ()


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
    """判断是否本机。exact match on short hostname or FQDN。"""
    actual = (hostname or current_hostname()).lower()
    actual_short = actual.split(".")[0]
    for candidate in host.hostnames:
        c = candidate.lower()
        if not c:
            continue
        c_short = c.split(".")[0]
        if c == actual or c_short == actual_short:
            return True
    return False
