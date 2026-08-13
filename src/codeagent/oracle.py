"""Oracle CLI — persistent-context advisory sessions (hot/warm/cold).

``codeagent oracle start|ask|status|watch|release``

Native-first design (per operator direction):
  - OMP:     omp-config memory (memsearch/autoRecall/handoffSaveToDisk) +
             parked-revive; the runtime is a tmux-supervised interactive
             OMP process that can accept in-loop steer.
  - OpenCode: native session DB + ``--session`` continuation (the subagent
             ``task_id`` mechanism); oh-my-openagent adds NO extra session
             fields — it only passes the native backend session id through.

Methods: hot (in-loop send to a live runtime) → warm (resume native backend
session) → cold (snapshot reconstruction). ``status`` must report the ACTUAL
method used — never claim a hot revive that did not happen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

log = logging.getLogger(__name__)

from codeagent.constants import ISO_TIMESTAMP_FORMAT
from codeagent.gateway.client import GatewayClient
from codeagent.gateway.events import control_socket_path
from codeagent.gateway.model import GatewayError
from codeagent.mailbox.store import MailboxStore, RequestLedger, resolve_root
from codeagent.park.registry import ParkRegistry
from codeagent.park.inject import build_cold_context
from codeagent.park.snapshot import ReviewSnapshot, latest_snapshot, save_snapshot
from codeagent.domain import ExecutionSpec, ModelContextUnavailable
from codeagent.domain.park import Lifecycle, ParkManifest
from codeagent.runtime.base import CAP_WARM_RESUME
from codeagent.runtime.registry import RuntimeRegistry

_ORACLE_AGENT = "oracle"

# ── 卡死/停滞检测（操作层防御；只告警，不自动 recover）─────────────
# 强信号：同 generation 连续 ≥3 个 interrupt_skipped（advisory 跳过工具）
#   事件且期间无成功 TOOL_FINISHED/ASSISTANT_PROGRESS → 疑似卡死。
_STUCK_SKIP_MIN = 3
# 弱信号：非终态 + runtime alive + 排除 heartbeat 后 ≥15 分钟无 work 事件
#   （TURN_STARTED/ASSISTANT_PROGRESS/TOOL_*）+ 无 in-flight tool → 疑似停滞。
_STALL_IDLE_SECONDS = 15 * 60
# work 事件种类（gateway 事件不落库 heartbeat，天然排除心跳干扰）。
_WORK_EVENT_KINDS = frozenset({
    "TURN_STARTED", "ASSISTANT_PROGRESS", "TOOL_STARTED", "TOOL_FINISHED",
})
# 终态（turn 结束 / 会话退出）——oracle 等待下一条 ask 是正常态，不告警。
_TERMINAL_TASK_STATES = frozenset({
    "agent_end", "agent_stop", "session_shutdown", "process_exit",
})
# OMP createSkippedToolResult() 的固定文案（interrupt_skipped 特征）。
_SKIP_TEXT_MARKER = "Skipped due to pending system advisory"
# P2-C: 卡死检测只扫描 JSONL 尾部 N 行（避免大文件全量读取开销）。
_STUCK_SCAN_TAIL_LINES = 200
# P1: oracle result 输出截断上限（字节），可通过 ORACLE_RESULT_MAX_BYTES 覆盖。
_DEFAULT_RESULT_MAX_BYTES = 32768  # 32KB; was 8KB — oracle output is long
# P2-3: snapshot staleness threshold for cold revive quality warning (days).
_SNAPSHOT_STALE_DAYS = 7


def _snapshot_age_days(manifest) -> float:
    """P2-3: days since the latest snapshot for *manifest*'s review_key.

    Returns ``-1.0`` when no snapshot exists (callers treat negative as
    "unknown / no snapshot").
    """
    try:
        snap = latest_snapshot(manifest.review_key)
    except Exception:
        return -1.0
    if not snap:
        return -1.0
    if not snap.generated_at:
        return -1.0
    return (time.time() - snap.generated_at) / 86400.0


def _kernel_and_store():
    from codeagent.cli import _get_swarm_kernel

    return _get_swarm_kernel()


def _gateway() -> GatewayClient:
    return GatewayClient(timeout=10)


def _ensure_gateway_or_hint() -> bool:
    """P4: ensure a gateway is reachable; auto-start if not.

    Returns True when the gateway is (or just became) running.
    When auto-start fails, prints a hint to stderr and returns False.
    """
    # Fast path: already running.
    try:
        GatewayClient(timeout=2).call("capabilities.get")
        return True
    except Exception:
        pass

    # Attempt auto-start via subprocess (same entry-point as `aimeshchat gateway start`).
    log.info("oracle: gateway not running — attempting auto-start")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "codeagent.gateway.cli", "start"],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode == 0:
            # Brief pause for socket readiness after a successful start.
            time.sleep(1.5)
            try:
                GatewayClient(timeout=2).call("capabilities.get")
                log.info("oracle: gateway auto-start succeeded")
                return True
            except Exception:
                pass
        # start command failed or socket still not ready.
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        hint_detail = detail[-1] if detail else "unknown error"
        log.warning("oracle: gateway auto-start failed (%s)", hint_detail)
    except subprocess.TimeoutExpired:
        log.warning("oracle: gateway auto-start timed out (20s)")
    except FileNotFoundError:
        log.warning("oracle: python executable not found for gateway auto-start")
    except Exception as exc:
        log.warning("oracle: gateway auto-start error: %s", exc)

    print(
        "error: gateway not running — auto-start failed.\n"
        "hint: run 'aimeshchat gateway start' manually, then retry.",
        file=sys.stderr,
    )
    return False


def _review_sid(review_key: str) -> str:
    """Swarm session id for a review key (hash-derived ora-* scheme).

    I3: previously ``replace(':', '-')[-12:]`` truncated the review key —
    two keys sharing the same 12-char suffix collided on the same swarm
    session id. A sha256 prefix is collision-safe for the same entropy.

    P0-A: 确定性 id——去掉 uuid4 随机后缀，仅用 sha256(review_key) 前 16
    位；同一 review_key 每次得到相同 sid，冷路径不再产生 session 碎片。
    """
    digest = hashlib.sha256(review_key.encode("utf-8")).hexdigest()[:16]
    return f"postmesh-{digest}"


def _adopt_runtime(review_key: str, sid: str, handle, backend: str) -> bool | str:
    """Adopt a spawned runtime into the local gateway (presence/status).

    Needed for runtimes WITHOUT a plugin handshake (opencode/generic); OMP
    runtimes are adopted by the plugin's runtime.register instead.

    P1-4 OWNER_MISMATCH 修复：OMP backend 禁止在此 adopt。CLI 进程用
    os.getpid()/随机 nonce 抢先注册时，会把 gateway 的 owner 身份占成
    "CLI PID + 随机 nonce"；随后插件 handshake 以 supervisor PID + spec
    nonce 同 generation 重注册，被 gateway 判为 OWNER_MISMATCH 而拒绝，
    真实插件的注册因此丢失。OMP 的 presence 由插件 runtime.register 正常
    接管，这里直接跳过。

    I2: returns True on success / False on failure so callers can surface
    ``adopted: false`` in their success JSON instead of silently degrading.
    OMP backend 返回 "skipped"（注册归插件 handshake，CLI 不 adopt）。
    """
    if backend == "omp":
        # OMP：插件 handshake（supervisor PID + spec nonce）负责
        # runtime.register；CLI 侧任何 adopt 都会以错误 owner 身份同
        # generation 抢先注册，触发 gateway OWNER_MISMATCH——必须跳过。
        return "skipped"
    try:
        from codeagent.gateway.client import GatewayClient

        GatewayClient(timeout=10).call("runtime.register", {
            "session_id": sid,
            "agent_id": _ORACLE_AGENT,
            "runtime_id": handle.runtime_id,
            "review_key": review_key,
            "generation": handle.generation,
            "backend_session_id": handle.backend_session_id,
            "runtime": backend,
            "owner_pid": os.getpid(),
            "nonce": uuid4().hex[:12],
        })
        return True
    except Exception as exc:
        print(f"warning: gateway runtime adoption failed: {exc}", file=sys.stderr)
        return False


def _resolve_backend(agent: str, requested: str) -> str:
    """Oracle prefers OMP → OpenCode (warm_resume required)."""
    from codeagent.cli import _resolve_agent_backend

    return _resolve_agent_backend(agent, requested)


# ── OMP native memory config integration (B1) ─────────────────────────


def _omp_config_paths() -> list[Path]:
    """Candidate omp config locations (config.yml / common.yml)."""
    candidates = [
        Path(os.environ["OMP_CONFIG"]) if os.environ.get("OMP_CONFIG") else None,
        Path.home() / ".omp" / "agent" / "config.yml",
        Path.home() / ".omp" / "config" / "common.yml",
        Path.home() / ".config" / "omp" / "common.yml",
    ]
    return [p for p in candidates if p is not None and p.exists()]


def _parse_flat_yaml(path: Path) -> dict:
    """Minimal YAML-subset parser for the omp config format.

    Supports ``key: value`` and 2-space-indented children (this file's
    exact shape). Returns a dict of section → {key: value}. Never raises
    on malformed lines — verification is best-effort.
    """
    out: dict[str, dict[str, str]] = {}
    current = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if line.startswith("  ") or line.startswith("\t"):
                if current:
                    out.setdefault(current, {})[key] = val
            else:
                current = key
                out.setdefault(key, {})
    except OSError:
        pass
    return out


def _read_agent_model(agent_type: str) -> str:
    """Read an OMP agent profile's ``model:`` field (agents/<agent_type>.md).

    Returns "" when the profile or its model field is absent.
    """
    base = Path.home() / ".omp" / "agent" / "agents"
    profile = base / f"{agent_type}.md"
    if not profile.exists():
        return ""
    try:
        for line in profile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("model:") or line.startswith("model="):
                return line.split(":", 1)[1].strip() if ":" in line else line.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def _normalize_oracle_agent(agent_type: str) -> str:
    """未显式指定 agent 时给明确默认（oracle），不再静默回落 default/mimo。

    空值 / ``default`` 归一为 ``oracle``（与 CLI ``--agent`` 默认值一致），
    其余类型原样透传。

    P2-B: 降为 debug——本函数在 ``cmd_oracle_start`` 中无条件调用（即使
    用户显式传了 ``--model``），此时 agent 不参与模型解析，warning 是
    噪声。仅在 ``_resolve_oracle_model_chain`` 的实际解析路径中，agent
    归一才影响结果，但该路径已有显式 ``--model`` 的 early-return 保护。
    """
    if not agent_type or agent_type == "default":
        log.debug("oracle: no explicit agent specified — defaulting to %r "
                  "(agent profile model: is the single authority)", _ORACLE_AGENT)
        return _ORACLE_AGENT
    return agent_type


# ── P1-4: config fingerprint — 检测配置变更以即时生效 ─────────────────


def _config_fingerprint(agent_type: str = "") -> str:
    """P1-4: 读取 agent profile 文件内容的 SHA256 指纹。

    优先哈希 agents/<agent_type>.md 文件内容（与 _read_agent_model
    解析的源一致）；profile 不存在或为空时，回退哈希 config.yml 中
    modelRoles section。返回 hex[:16]；两者均缺失返回空串。
    用于 manifest 落盘——ask 时比较当前指纹与 manifest 指纹，
    不同则重新从配置推导 model chain（忽略 manifest 缓存）。
    """
    if agent_type:
        profile_path = Path.home() / ".omp" / "agent" / "agents" / f"{agent_type}.md"
        if profile_path.exists():
            try:
                content = profile_path.read_text(encoding="utf-8").strip()
                if content:
                    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            except OSError:
                pass
    # Fallback: hash config.yml modelRoles section.
    config_path = Path.home() / ".omp" / "agent" / "config.yml"
    if not config_path.exists():
        return ""
    try:
        parsed = _parse_flat_yaml(config_path)
    except OSError:
        return ""
    relevant: dict = {}
    for section in ("modelRoles", "fallbackChains", "retry"):
        if section in parsed:
            relevant[section] = parsed[section]
    if not relevant:
        return ""
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── Q5b: default 继承主 agent 模型（runtime.context 机制）─────────────


def _runtime_context_model(agent: str) -> Optional[tuple[str, str, str]]:
    """Q5b: 从 gateway runtime.context 继承主 agent 当前模型（来源 2）。

    仅当调用方在 gateway runtime 内（AIMESHCHAT_RUNTIME_ID 已设置）时
    启用：``runtime.context_get`` 命中返回 (model, variant, provider)；
    无上下文或查询失败 → 抛 ``ModelContextUnavailable``（明确报错，不
    静默回落 mimo）；非 gateway 调用（无环境变量）→ 返回 None，由调用
    方继续尝试来源 3（OMP execution-context 文件）或回退 agent profile。

    完整优先级（Q5 §9）：
    1. 显式 --model/--variant；
    2. AIMESHCHAT_RUNTIME_ID → gateway runtime.context（本函数）；
    3. OMP 0600 execution-context 文件（ExecutionSpec.from_args 内部）；
    4. agent profile（向后兼容）。

    ``agent`` 仅作签名对齐（from_args 调用约定）；继承与 agent 无关。
    """
    runtime_id = os.environ.get("AIMESHCHAT_RUNTIME_ID", "")
    if not runtime_id:
        return None
    try:
        resp = _gateway().call("runtime.context_get", {"runtime_id": runtime_id})
    except Exception as exc:
        raise ModelContextUnavailable(
            f"gateway runtime.context_get failed for {runtime_id}: {exc}"
        ) from exc
    ctx = (resp or {}).get("model_context") or {}
    model = ctx.get("model", "") or ""
    if not model:
        raise ModelContextUnavailable(
            f"runtime {runtime_id} has no model context "
            "(plugin model_change 未上报)；请显式 --model 或等待主 agent 上报"
        )
    return model, ctx.get("variant", "") or "", ctx.get("provider", "") or ""


def _resolve_oracle_model_chain(agent_type: str, explicit_model: str) -> list[str]:
    """Resolve the primary model chain for an oracle agent（M-model）。

    B2: 此函数已非主路径——主路径使用 ExecutionSpec 显式字段
    （spec.model / manifest.primary_model / manifest.variant / manifest.system_prompt）。
    保留仅供 ``--agent`` 便捷名 fallback：缺 --model 时读 agent profile
    的 model: 字段（向后兼容）。

    模型解析两条路径，不再依赖 config.yml 的 retry.fallbackChains：

    - ``explicit_model`` 非空 → 单元素 ``[explicit_model]``（用户显式
      --model，永远优先，不被 profile 覆盖）。
    - 否则读 agent profile 的 model:（oracle → gpt-5.6-sol、oracle-lite →
      v4-pro、oracle-opus → claude-opus；以 profile 实际值为准）。
    - 空/未知 agent → ``_normalize_oracle_agent`` 归一为明确默认 oracle。
    - 全部缺失 → ``[]``（调用方显式处理，不静默降级）。
    """
    if explicit_model:
        return [explicit_model]

    agent_type = _normalize_oracle_agent(agent_type)
    m = _read_agent_model(agent_type)
    if m:
        return [m]

    return []


def _model_chain_from_manifest(agent_type: str, manifest, explicit_override: str = "") -> list[str]:
    """M-model: 从 manifest 读已落盘模型，revive/ask 不再重推导。

    B2: 此函数已非主路径——主路径直接读 manifest Q5 字段
    （manifest.primary_model / manifest.model / manifest.variant /
    manifest.system_prompt）。保留仅供旧 manifest（无 primary_model 且无
    spec 字段）的迁移兼容 fallback。

    优先级：
    1. ``manifest.model``（start 时显式 --model 的持久化）或
       ``explicit_override``（调用方本次 --model）→ 单元素 ``[该模型]``
       （显式覆盖永远优先）。
    2. ``manifest.primary_model``（start 落盘的 chain[0]，ExecutionSpec
       解析结果）→ 单元素 ``[primary_model]``。
    3. 旧 manifest 无 primary_model（升级前创建）→ 现场解析一次
       （_resolve_oracle_model_chain），迁移兼容；不写回 manifest。
    """
    explicit = (manifest.model if manifest else "") or explicit_override
    if explicit:
        return [explicit]
    if manifest is not None and manifest.primary_model:
        return [manifest.primary_model]
    return _resolve_oracle_model_chain(agent_type, "")


def _ask_model_chain_realtime(agent_type: str, manifest, explicit_override: str = "") -> list[str]:
    """P1: ask 时实时修正 model_chain——runtime.context 优先，manifest 仅 fallback。

    B2: 此函数已非主路径——主路径直接读 manifest Q5 字段
    （manifest.primary_model / manifest.model）+ 显式 --model 覆盖。
    保留仅供旧 manifest（无 primary_model 且无 spec 字段）的迁移兼容 fallback。

    manifest 落盘的 primary_model 是 start 时刻的解析结果，主 agent 换模型后
    会过期。ask（warm/cold spawn）先用 runtime.context_get 继承当前主 agent
    模型作为实时修正源；查询失败（gateway 不可达）或非 gateway 调用 →
    回退 manifest（旧行为，向后兼容）。

    P1-4: 配置变更即时感知——比较当前 config.yml 指纹与 manifest 落盘指纹，
    不同则跳过 manifest 缓存，重新从配置推导 model chain。

    优先级：
    1. 显式 --model（manifest.model / explicit_override）→ 永远优先。
    2. AIMESHCHAT_RUNTIME_ID 下 runtime.context_get 命中 → [实时模型]。
    3. P1-4: config fingerprint 不匹配 → 重新从配置推导（忽略 manifest 缓存）。
    4. manifest 落盘模型（start 时解析的 primary_model / model）。
    5. 旧 manifest 无 primary_model → 现场解析 agent profile。
    """
    explicit = (manifest.model if manifest else "") or explicit_override
    if explicit:
        return [explicit]
    try:
        ctx = _runtime_context_model(agent_type)
    except ModelContextUnavailable:
        # 网关查询失败 → manifest 仅作 fallback（不 fatal）。
        ctx = None
    if ctx and ctx[0]:
        return [ctx[0]]
    # P1-4: config fingerprint 比较——配置变更时忽略 manifest 缓存。
    if manifest and manifest.config_fingerprint:
        current_fp = _config_fingerprint(agent_type)
        if current_fp and current_fp != manifest.config_fingerprint:
            log.warning(
                "oracle: config fingerprint changed (manifest=%s, current=%s) — "
                "re-deriving model chain from config (ignoring manifest cache)",
                manifest.config_fingerprint, current_fp,
            )
            return _resolve_oracle_model_chain(agent_type, "")
    return _model_chain_from_manifest(agent_type, manifest, "")


def _looks_like_quota(text: str) -> bool:
    """True when *text* carries a provider quota/rate-limit marker.

    Mirrors the supervisor's ``_scan_quota_error`` markers so CLI-side
    failures (spawn errors, warm/cold degrade messages) can be reported
    explicitly as insufficient_quota instead of generic timeouts.
    """
    import re

    return bool(re.search(
        r"insufficient[_ -]?quota|quota[ _-]?exceeded|quota exceeded|"
        r"rate[ _-]?limit|payment required|\b402\b|billing|quota",
        text,
        re.IGNORECASE,
    ))


def _merge_flat_yaml(path: Path, ensure: dict[str, dict[str, str]]) -> bool:
    """Insert missing keys under their correct YAML sections (with backup).

    Only appends keys that are entirely absent — never overwrites an
    existing value. Returns True when a merge happened.
    """
    backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    body = "\n".join(lines)
    merged = False

    # P3-9: index section boundaries so missing keys insert at the end of
    # their owning section rather than being appended at file end.
    section_end: dict[str, int] = {}
    current_section = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key_part, _, _ = stripped.partition(":")
        key_part = key_part.strip()
        if not (line.startswith("  ") or line.startswith("\t")):
            if current_section:
                section_end[current_section] = i
            current_section = key_part
    if current_section:
        section_end[current_section] = len(lines)

    # Build insertion plan: per-existing-section lines + trailing new sections.
    section_inserts: dict[str, list[str]] = {}
    new_section_lines: list[str] = []
    for section, kv in ensure.items():
        missing = {k: v for k, v in kv.items() if f"{k}:" not in body}
        if not missing:
            continue
        merged = True
        if section in section_end:
            for k, v in missing.items():
                section_inserts.setdefault(section, []).append(f"  {k}: {v}")
        else:
            if not new_section_lines:
                new_section_lines.append(f"{section}:")
            for k, v in missing.items():
                new_section_lines.append(f"  {k}: {v}")

    if not merged:
        return False

    # P3-9: apply inserts bottom-up so line indices remain valid.
    new_lines = list(lines)
    for section, insert_lines in sorted(
        section_inserts.items(), key=lambda x: section_end[x[0]], reverse=True,
    ):
        pos = section_end[section]
        for j, text in enumerate(insert_lines):
            new_lines.insert(pos + j, text)
    new_lines.extend(new_section_lines)

    if path.exists():
        path.rename(backup)
    with open(path, "w") as f:
        f.write("\n".join(new_lines) + "\n")
    return True


def ensure_omp_memory_config(apply: bool = False) -> dict:
    """Verify/merge OMP native memory config (memsearch/autoRecall/handoff).

    D4: 默认只检测缺失项，不自动修改用户配置。传 ``apply=True`` 时才
    写入缺失的配置项（带时间戳备份）；``apply=False`` 时 report 包含
    ``missing`` 列表供调用方提示用户。

    Per the native-first directive: codeagent actively ensures the OMP
    native persistence knobs are on. Missing keys are merged (with a
    timestamped backup) ONLY when apply=True; existing values are never
    overwritten.
    """
    paths = _omp_config_paths()
    report: dict = {
        "config_path": str(paths[0]) if paths else "",
        "backend": "", "auto_recall": False, "handoff_save_to_disk": False,
        "merged": False, "missing": [],
    }
    if not paths:
        report["missing"] = ["omp config file not found (autoRecall/handoff unverified)"]
        return report
    path = paths[0]
    parsed = _parse_flat_yaml(path)
    report["backend"] = parsed.get("memory", {}).get("backend", "")
    auto_recall = parsed.get("memsearch", {}).get("autoRecall", "") or parsed.get("memory", {}).get("autoRecall", "")
    report["auto_recall"] = auto_recall.lower() == "true"
    handoff = parsed.get("compaction", {}).get("handoffSaveToDisk", "")
    report["handoff_save_to_disk"] = handoff.lower() == "true"
    if not report["auto_recall"]:
        report["missing"].append("memsearch.autoRecall=true")
    if not report["handoff_save_to_disk"]:
        report["missing"].append("compaction.handoffSaveToDisk=true")
    if not report["backend"]:
        report["missing"].append("memory.backend=memsearch")

    # D4: 只有 apply=True 时才自动写入缺失配置。
    if report["missing"] and apply:
        try:
            ensure = {}
            if not report["auto_recall"]:
                ensure.setdefault("memsearch", {})["autoRecall"] = "true"
            if not report["handoff_save_to_disk"]:
                ensure.setdefault("compaction", {})["handoffSaveToDisk"] = "true"
            if not report["backend"]:
                ensure.setdefault("memory", {})["backend"] = "memsearch"
            report["merged"] = _merge_flat_yaml(path, ensure)
        except OSError as exc:
            report["merge_error"] = str(exc)
    return report


# ── A1: oracle overlay config（防日志爆炸）──────────────────────────────

_ORACLE_OVERLAY_CONTENT = """\
# oracle overlay — 自动生成，勿手动编辑。
# MFT：唯一落盘相关键 = advisor.enabled=false（杀 499MB __advisor.jsonl 被动二评）。
# display.hideToolActivity（仅 TUI）/ compaction.strategy（仅上下文）不落盘，是文档噪音。
# tool 结果溢出/截断走 OMP 默认（>50KB 溢出到 artifact，jsonl 保留 head20KB+tail20KB，审计够用且 jsonl 小）。
advisor:
  enabled: false
"""


def _ensure_oracle_overlay() -> Path:
    """A1: 确保 ~/.omp/oracle/oracle-overlay.yml 存在（幂等写入）。

    overlay 内容：advisor.enabled=false（杀 __advisor.jsonl）、
    display.hideToolActivity=true、tools.artifactSpillThreshold=50。
    已存在时跳过写入（避免无谓 I/O）。返回 overlay 路径。
    """
    overlay = Path.home() / ".omp" / "oracle" / "oracle-overlay.yml"
    if overlay.exists():
        return overlay
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(_ORACLE_OVERLAY_CONTENT, encoding="utf-8")
    log.debug("oracle: wrote overlay config → %s", overlay)
    return overlay


# ── start ──────────────────────────────────────────────────────────────


def _check_mailbox_plugin() -> Optional[str]:
    """A4: verify the installed omp-mailbox-plugin can report runtime events.

    Returns an error message when the plugin is missing or predates
    RuntimeEventReporter — otherwise None. Without the reporter, gateway
    heartbeat presence silently degrades (no runtime events), so oracle start
    must fail loudly instead of proceeding blind.
    """
    plugin_src = (
        Path.home() / ".omp" / "plugins" / "node_modules"
        / "omp-mailbox-plugin" / "src" / "index.ts"
    )
    try:
        text = plugin_src.read_text(encoding="utf-8")
    except OSError as exc:
        return (
            f"omp-mailbox-plugin not found at {plugin_src} ({exc}); oracle "
            "heartbeat presence requires it. Update: run `omp plugin update "
            "omp-mailbox-plugin` (or reinstall), then retry oracle start."
        )
    if "RuntimeEventReporter" not in text:
        return (
            f"installed omp-mailbox-plugin ({plugin_src}) predates "
            "RuntimeEventReporter — heartbeat presence is unavailable. Update "
            "the plugin (`omp plugin update omp-mailbox-plugin`), then retry "
            "oracle start."
        )
    return None


def _oracle_init_protocol(sid: str) -> str:
    """B3: self-describing protocol block embedded in the oracle-init TASK body.

    The plugin handshake picks the TASK body up as the initial user message
    (pi.sendUserMessage); embedding reply_format / required_fields / example
    makes the handshake self-describing without a separate doc fetch.
    """
    return (
        "\n\n--- ORACLE MAILBOX PROTOCOL (B3) ---\n"
        "reply_format: REPORT envelope via `mailbox send --kind REPORT` "
        "(JSON payload in --body)\n"
        "required_fields: reply_to=<original msg_id>, run_id, request_id\n"
        f"example: mailbox send --session {sid} --from oracle --to manager "
        "--kind REPORT --reply-to <msg_id> --run-id <run_id> "
        "--request-id <request_id> --subject result --body '<json>'\n"
    )


# ── A1: session_id 同步绑定 + meta.json ───────────────────────────────


def _oracle_meta_path(review_key: str) -> Path:
    """A1: meta.json path for a review key (~/.omp/oracle/<safe-key>/meta.json).

    review_key may carry ':' / '/' — sanitize into a safe directory name
    (same substitution ``_review_sid`` applies to ':').
    """
    safe = review_key.replace(":", "-").replace("/", "-").replace("\\", "-")
    return Path.home() / ".omp" / "oracle" / safe / "meta.json"


def _oracle_session_dir(review_key: str) -> str:
    """P-SI: dedicated OMP session directory for an oracle review.

    Returns ``~/.omp/agent/sessions/_oracle/<safe-key>/`` — each oracle
    review gets its own isolated session directory so OMP sessions don't
    bleed across reviews.  The directory is NOT created here; callers
    must ``mkdir -p`` before passing to ``omp --session-dir``.
    """
    safe = review_key.replace(":", "-").replace("/", "-").replace("\\", "-")
    return str(Path.home() / ".omp" / "agent" / "sessions" / "_oracle" / safe)


def _read_oracle_meta(review_key: str) -> dict:
    """A1: read the bound-session meta for a review key ({} when absent)."""
    try:
        path = _oracle_meta_path(review_key)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_oracle_meta(review_key: str, backend_session_id: str, status: str,
                       swarm_session_id: str = "") -> dict:
    """A1: persist bound-session meta (backend_session_id/bound_at/status).

    ``status`` is "bound" after a successful bind; the file is the warm
    resume point for revive/ask when the park manifest is missing or stale.
    """
    meta = {
        "review_key": review_key,
        "swarm_session_id": swarm_session_id,
        "backend_session_id": backend_session_id,
        "bound_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
        "status": status,
    }
    try:
        path = _oracle_meta_path(review_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".tmp-{uuid4().hex[:8]}.json"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except OSError as exc:
        print(f"warning: oracle meta write failed: {exc}", file=sys.stderr)
    return meta


def _runtime_log_path(handle) -> Optional[Path]:
    """A1: runtime log file for a spawned handle.

    OMP interactive handles carry the supervisor spec path — the log lives
    beside it (<runtime_dir>/<runtime_id>.log). Falls back to the
    supervisor's canonical runtime dir (``_runtime_dir``).
    """
    spec_path = getattr(handle, "extra", {}).get("spec_path") if hasattr(handle, "extra") else None
    if spec_path:
        p = Path(spec_path)
        if p.parent.is_dir():
            return p.parent / f"{handle.runtime_id}.log"
    try:
        from codeagent.runtime.supervisor import _runtime_dir

        return _runtime_dir(handle.runtime_id) / f"{handle.runtime_id}.log"
    except Exception:
        return None


def _scan_runtime_log_for_session_id(log_path: Optional[Path]) -> str:
    """A1: extract the native backend session id from a runtime log.

    Scans for ``session_id=`` / ``backend_session=`` markers (the OMP plugin
    / opencode runner print the native backend session id as it binds).
    ``backend_session*`` matches win over generic ``session_id`` (which can
    also carry the ora-* swarm sid); the LAST match wins (the bound id is
    printed once the session actually starts).
    """
    if log_path is None or not log_path.exists():
        return ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    backend_matches = re.findall(r"backend_session(?:_id)?\s*=\s*([^\s,;\"']+)", text)
    if backend_matches:
        return backend_matches[-1].strip()
    for g in reversed(re.findall(r"session_id\s*=\s*([^\s,;\"']+)", text)):
        g = g.strip().strip('"').strip("'")
        if g and not g.startswith(("postmesh-", "ora-")):
            return g
    return ""


def _poll_backend_session_id(handle, timeout: float = 60.0, interval: float = 0.5) -> str:
    """A1: synchronously bind the backend session id after spawn (≤60s).

    Fast path: the handle already carries a ``backend_session_id`` (adapters
    that resolve it synchronously). Otherwise poll the runtime log for up to
    *timeout* seconds, scanning for ``session_id=`` / ``backend_session=``.
    Returns "" when binding times out (caller must NOT report success).
    """
    direct = getattr(handle, "backend_session_id", "") or ""
    if direct:
        return direct
    log_path = _runtime_log_path(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = _scan_runtime_log_for_session_id(log_path)
        if sid:
            return sid
        time.sleep(interval)
    return ""


def _resolve_bound_session_id(review_key: str, manifest) -> str:
    """A1: backend session id for a review key — manifest → meta.json.

    The park manifest is authoritative (updated on start/ask/revive);
    meta.json is the A1 persistence that survives manifest loss and warms
    revive/ask before spawning.

    Root-cause fix (2026-08-12): plugin handshake runs BEFORE OMP binds its
    native session id, so runtime_register persisted an EMPTY sid into the
    manifest — manifest/meta stayed empty forever even though the gateway's
    in-memory runtime record later got the real sid.  When both manifest and
    meta are empty, lazily sync from the gateway (runtime.info) and backfill
    both, so every consumer self-heals regardless of registration timing.
    """
    if manifest is not None and getattr(manifest, "backend_session_id", ""):
        return manifest.backend_session_id
    meta = _read_oracle_meta(review_key)
    sid = (meta or {}).get("backend_session_id", "") or ""
    if sid:
        return sid
    # Lazy sync: query the gateway's authoritative binding, backfill both.
    sid = _sync_backend_session_from_gateway(review_key, manifest)
    return sid


def _sync_backend_session_from_gateway(review_key: str, manifest):
    """Lazy-backfill a bound backend_session_id from the live gateway.

    The gateway's in-memory runtime record holds the real OMP session id
    (bound after plugin handshake).  When park manifest and meta.json are
    both stale/empty, pull it here and persist to both so warm resume and
    transcript extraction work.  Idempotent; returns "" when not bound yet.
    """
    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        sid = info.get("backend_session_id", "")
        if not sid:
            return ""
    except (GatewayError, Exception):  # noqa: BLE001
        return ""
    # Backfill manifest + meta.json so downstream reads (warm revive,
    # _wait_final_text transcript extraction) use the real session.
    try:
        from codeagent.park.registry import ParkRegistry
        from dataclasses import replace

        reg = ParkRegistry()
        cur = reg.lookup(review_key) if manifest is None else manifest
        if cur is not None and not getattr(cur, "backend_session_id", ""):
            reg.update(review_key, replace(cur, backend_session_id=sid))
        _write_oracle_meta(review_key, sid, "bound",
                           swarm_session_id=(getattr(cur, "swarm_session_id", "") or "") if cur else "")
    except Exception as exc:  # noqa: BLE001
        log.debug("oracle: lazy backend-session sync failed (%s)", exc)
    return sid


def cmd_oracle_start(args: argparse.Namespace) -> int:
    """Create review/session/runtime and return the runtime id."""
    # P4: refuse to start when gateway is unreachable (auto-start attempted).
    if not _ensure_gateway_or_hint():
        return 1

    # A4: heartbeat defense — refuse to start when the mailbox plugin cannot
    # report runtime events (presence/heartbeat would silently degrade).
    plugin_err = _check_mailbox_plugin()
    if plugin_err is not None:
        # U3: error output is JSON to stderr (consistent with ask/revive).
        print(json.dumps({
            "error": "mailbox_plugin_unavailable",
            "review_key": args.review_key,
            "detail": plugin_err,
        }, indent=2), file=sys.stderr)
        return 1
    review_key = args.review_key
    # M-model: 未显式 --agent 时归一为明确默认（oracle），不再静默回落。
    agent = _normalize_oracle_agent(args.agent)
    backend = _resolve_backend(agent, args.backend)
    workdir = args.workdir or os.getcwd()

    # ── OMP native memory config (B1): verify/merge autoRecall + handoff ──
    # D4: 默认只检测不写入；--apply-memory-config 才自动修改用户配置。
    apply_mem_cfg = getattr(args, "apply_memory_config", False)
    memory_report = ensure_omp_memory_config(apply=apply_mem_cfg)
    if memory_report["merged"]:
        print(f"omp memory config merged: {memory_report['config_path']}", file=sys.stderr)
    elif memory_report["missing"] and not memory_report["config_path"]:
        print(f"warning: {memory_report['missing'][0]}", file=sys.stderr)
    elif memory_report["missing"] and memory_report["config_path"] and not apply_mem_cfg:
        # D4: 检测到缺失但未自动修改——提示用户加 --apply-memory-config。
        print(
            f"info: omp memory config has missing keys ({', '.join(memory_report['missing'])}). "
            f"Use --apply-memory-config to auto-merge, or fix manually in "
            f"{memory_report['config_path']}.",
            file=sys.stderr,
        )
    memory_env = {"OMP_MEMORY_CONFIG_PATH": memory_report["config_path"]} if memory_report["config_path"] else {}

    # ── Q5: ExecutionSpec — 不可变执行规格（去 role 化核心） ───────────
    # Q5b: 显式 --model/--variant 优先；无显式时若在 gateway runtime 内
    # （AIMESHCHAT_RUNTIME_ID）继承主 agent 当前模型（runtime.context）。
    # P0-B: 完全去 role——不再回落 agent profile（不传 resolve_agent_model）。
    # 无 --model 时仅走 runtime.context（来源 2）→ execution-context（来源 3），
    # 全部缺失则下方报错要求 --model。--model-strict 仍控制 runtime.context
    # 查询失败是否 fatal（MODEL_CONTEXT_UNAVAILABLE）。
    try:
        spec = ExecutionSpec.from_args(
            args,
            resolve_runtime_context=_runtime_context_model,
            runtime_context_strict=bool(getattr(args, "model_strict", False)),
        )
    except ModelContextUnavailable as exc:
        # 仅 --model-strict 可达：显式要求 runtime.context 缺失即失败。
        print(f"MODEL_CONTEXT_UNAVAILABLE: {exc}", file=sys.stderr)
        return 1
    if not spec.model:
        # P0-B/P1-B: 显式 --model / runtime.context / execution-context 全部
        # 缺失——报错要求 --model，不再回落 agent profile。
        print(
            "error: oracle start 未能解析模型——请显式 --model"
            "（runtime.context 与 execution-context 均无可用模型；已不再回落 agent profile）",
            file=sys.stderr,
        )
        return 1
    log.debug("oracle start: ExecutionSpec(provider=%s, model=%s, variant=%s, system=%r)",
              spec.provider, spec.model, spec.variant, spec.system_prompt[:40] if spec.system_prompt else "")

    # ── B2: ExecutionSpec 显式为主路径 ─────────────────────────────────
    # 主路径 = ExecutionSpec.from_args 解析出的 spec.model（显式 --model /
    # runtime.context / execution-context 已在 from_args 内部处理；source4
    # agent profile 回落已移除）。不再调用 _resolve_oracle_model_chain 重推导。
    primary_model = spec.model
    model_chain = [primary_model] if primary_model else []
    chain_env: dict[str, str] = {}
    if model_chain:
        chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(model_chain)
        log.debug("oracle start: model chain resolved: %s (primary=%s)", model_chain, primary_model)

    # ── Swarm session + agent registration ────────────────────────────
    kernel, store = _kernel_and_store()
    sid = _review_sid(review_key)
    try:
        kernel.create_session(sid, "manager", [_ORACLE_AGENT])
    except ValueError as exc:
        # 改进项4: only swallow the idempotent "already exists" case; a real
        # conflict (manager mismatch / corrupt session dir) must surface.
        if "already exists" not in str(exc):
            raise
        log.debug("oracle start: swarm session %s already exists (idempotent)", sid)
    from codeagent.swarm.model import AgentLocation, ExecutionMode, ReturnMode

    try:
        kernel.register(
            AgentLocation(
                agent_id=_ORACLE_AGENT,
                host_alias="__local__",
                backend=backend,
                execution_mode=ExecutionMode.MAILBOX_WORKER,
                return_mode=ReturnMode.BIDIRECTIONAL,
                mailbox_root=str(resolve_root()),
            ),
            session_id=sid,
        )
    except ValueError as exc:
        # P3-7: only swallow duplicate-registration; real errors must surface.
        if "already" not in str(exc).lower() and "exist" not in str(exc).lower():
            raise
        log.debug("oracle start: agent %s already registered (idempotent)", _ORACLE_AGENT)

    # ── Park (idempotent; backend session filled after spawn) ─────────
    registry = ParkRegistry()
    existing = registry.lookup(review_key)

    # ── Initial TASK into the oracle inbox (plugin handshake picks it up
    #    as the initial task via pi.sendUserMessage). ─────────────────
    # U2: prompt 只走单一通道，消除双重传递：
    #   - omp（默认 interactive_plugin）：mailbox TASK 是唯一到达 runtime
    #     的通道（gateway runtime_register handshake → pi.sendUserMessage；
    #     spawn 的 task 对 interactive 模式只写进 spec.json，不进 argv）。
    #     mailbox 保留 prompt+B3；spawn task 置空。
    #   - opencode/generic：spawn task 经 argv 位置参数到达 runtime；
    #     mailbox TASK 仅携带 B3 协议块（不含 prompt），避免潜在二次执行。
    # Q5: 使用 spec.full_prompt（已含 system_prompt 前置组合）
    prompt = spec.full_prompt
    if backend == "omp":
        init_task_body = prompt + _oracle_init_protocol(sid) if prompt else ""
    else:
        init_task_body = _oracle_init_protocol(sid)
    if init_task_body:
        try:
            from codeagent.mailbox.service import MailboxService

            MailboxService(store=store).send(
                session_id=sid,
                from_id="manager",
                to_id=_ORACLE_AGENT,
                subject="oracle-init",
                body=init_task_body,  # B3: self-describing handshake
                kind="TASK",
                run_id=f"run-{uuid4().hex[:10]}",
                request_id=f"req-{uuid4().hex[:10]}",
                require_ack=True,
            )
        except Exception as exc:
            print(f"warning: initial task dispatch failed: {exc}", file=sys.stderr)

    # ── Spawn runtime (native path) ───────────────────────────────────
    reg = RuntimeRegistry()
    spawn_env = {**memory_env, **chain_env}
    # Q6/A1: omp 后端注入防日志爆炸 overlay（advisor.enabled=false 杀
    # __advisor.jsonl；artifactSpillThreshold 溢出大 tool 结果；
    # hideToolActivity 减少显示噪声）。仅 omp 接受 --config。
    profile_args: list[str] = []
    if backend == "omp":
        overlay = _ensure_oracle_overlay()
        profile_args = ["--config", str(overlay)]
    # P-SI: compute isolated session directory for this oracle review.
    # CLI --session-dir overrides; otherwise use the default per-review path.
    session_dir = getattr(args, "session_dir", "") or ""
    if not session_dir and backend == "omp":
        session_dir = _oracle_session_dir(review_key)
    if session_dir:
        Path(session_dir).mkdir(parents=True, exist_ok=True)
    handle = reg.spawn(backend, {
        "session_id": sid,
        "agent_id": _ORACLE_AGENT,
        "review_key": review_key,
        "workdir": workdir,
        # U2: omp 走 mailbox 单通道（interactive 模式 task 不进 argv）；
        # 非 omp 后端（opencode argv 位置参数）经 task 传递 prompt。
        "task": prompt if backend != "omp" else "",
        "model": primary_model,
        "variant": spec.variant,  # Q5: ExecutionSpec 变体 → opencode --variant（omp 由 plugin 经 execution-context 消费）
        "gateway_socket": str(control_socket_path()),
        "owner_pid": os.getpid(),
        "nonce": uuid4().hex[:12],
        "short_task": False,
        "profile_args": profile_args,
        "env": spawn_env,
        "session_dir": session_dir,  # P-SI: 会话隔离目录
    })

    # ── A1: session_id 同步绑定 ──────────────────────────────────────
    # Poll the runtime log (≤60s) for the native backend session id so the
    # review's warm resume point is known BEFORE start returns, then persist
    # it to ~/.omp/oracle/<review_key>/meta.json for revive/ask reuse.
    # NOTE: slow-starting oracle (oracle-full/opus reads config, long first
    # turn) may exceed the window — binding MUST NOT kill the runtime (that
    # caused SIGTERM 143 on healthy advisors). On timeout we return success
    # with binding="pending"; the gateway syncs backend_session_id into the
    # park manifest later via runtime.register (existing A2/P2-11 path), so
    # warm resume still converges without killing the advisor.
    bound_session_id = _poll_backend_session_id(handle)
    binding_pending = not bound_session_id
    if binding_pending:
        # Do NOT reg.stop() — the oracle is alive and reasoning; only the
        # session-id binding hasn't surfaced yet. Report success with
        # binding=pending so callers know warm-resume isn't ready yet.
        log.debug(
            "oracle start: backend session binding pending for %s "
            "(runtime kept alive; will sync via runtime.register)", review_key,
        )

    # Persist backend session into the park manifest (authoritative).
    # M-model: manifest.model 仅存显式覆盖（args.model）；primary_model 存
    # start 时解析出的 chain[0]（agent profile 权威结果）。revive/ask 直接
    # 读 primary_model，不再重推导；显式覆盖优先于 primary_model。
    # Q5: ExecutionSpec 字段持久化到 manifest（provider/variant/system_prompt）
    if existing is not None:
        # D3: restart path — preserve every untouched field via replace()
        # instead of copying 15+ fields manually. created_at is kept from
        # the original row; stale release/session state is reset.
        manifest = replace(
            existing,
            swarm_session_id=sid,
            agent_type=agent,
            model=args.model or "",
            primary_model=primary_model,
            host="__local__",
            workdir=workdir,
            lifecycle=Lifecycle.HOT_PARKED,
            backend_session_id=bound_session_id,
            last_activity_at=time.time(),
            release_mode="",
            omp_session_path="",
            provider=spec.provider,
            variant=spec.variant,
            system_prompt=spec.system_prompt,
            config_fingerprint=_config_fingerprint(agent),  # P1-4: 配置变更即时感知
            omp_session_dir=session_dir,  # P-SI: 会话隔离目录
        )
        registry.update(review_key, manifest)
    else:
        manifest = ParkManifest(
            review_key=review_key,
            swarm_session_id=sid,
            agent_type=agent,
            model=args.model or "",
            primary_model=primary_model,
            host="__local__",
            workdir=workdir,
            lifecycle=Lifecycle.HOT_PARKED,
            backend_session_id=bound_session_id,
            created_at=time.time(),
            last_activity_at=time.time(),
            provider=spec.provider,
            variant=spec.variant,
            system_prompt=spec.system_prompt,
            config_fingerprint=_config_fingerprint(agent),  # P1-4: 配置变更即时感知
            omp_session_dir=session_dir,  # P-SI: 会话隔离目录
        )
        registry.acquire(review_key, manifest)

    # A1: persist the bound-session meta (backend_session_id/bound_at/status).
    # binding_pending → status "pending" (runtime kept alive; warm-resume point
    # syncs later via gateway runtime.register).
    _write_oracle_meta(review_key, bound_session_id,
                       "pending" if binding_pending else "bound", swarm_session_id=sid)

    # Adopt the runtime into the LOCAL gateway so presence/status work even
    # for runtimes without a plugin handshake (opencode/generic).
    # I2: surface adoption failure — a silent presence gap would otherwise
    # look like a healthy runtime.
    adopted = _adopt_runtime(review_key, sid, handle, backend)

    print(json.dumps({
        "review_key": review_key,
        "session_id": sid,
        "runtime_id": handle.runtime_id,
        "backend": backend,
        "backend_session_id": bound_session_id,
        "bound": not binding_pending,
        "binding": "pending" if binding_pending else "bound",
        "adopted": adopted,
        "meta_path": str(_oracle_meta_path(review_key)),
        "generation": handle.generation,
        "mode": handle.mode,
        "capabilities": sorted(handle.capabilities),
        "model_chain": model_chain,
        "primary_model": primary_model,  # M-model: manifest 落盘的 chain[0]
        "spec": {                        # Q5: ExecutionSpec 完整快照
            "provider": spec.provider,
            "model": spec.model,
            "variant": spec.variant,
            "model_source": spec.model_source,  # P0: runtime_context|execution_context|agent_profile|explicit
            "system_prompt": spec.system_prompt[:80] + "…" if len(spec.system_prompt) > 80 else spec.system_prompt,
        },
    }, indent=2))
    return 0


# ── ask (hot → warm → cold) ────────────────────────────────────────────


def _ask_retrieve_hint(review_key: str) -> str:
    """E1: ask 成功后的取回答提示——引导用户用 wait/result 子命令。"""
    return (
        f"use 'oracle wait {review_key}' or 'oracle result {review_key}' "
        "to retrieve the answer"
    )


def cmd_oracle_ask(args: argparse.Namespace) -> int:
    """Deliver a prompt to the review's runtime: hot in-loop, warm resume,
    or cold reconstruction. Reports the ACTUAL method used."""
    review_key = args.review_key
    # U1: prompt 必填（argparse 已移除 nargs='?'）——删除 sys.stdin.read()
    # fallback，避免非交互终端/忘记 prompt 时挂起。程序化调用方仍防御空值。
    prompt = (args.prompt or "").strip()
    if not prompt:
        # U3: error output is JSON to stderr.
        print(json.dumps({"error": "no_prompt", "review_key": review_key},
                         indent=2), file=sys.stderr)
        return 1

    # ── Hot: live runtime, in-loop send ───────────────────────────────
    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        health = info.get("runtime_health", {})
        if info.get("status") == "active" and health.get("alive"):
            # B: steer needs a bound backend session to inject into the
            # session JSONL — with sid empty the message reaches the mailbox
            # but is silently dropped (mailbox-delivery ≠ session-injection).
            # Fail fast (exit 1) instead of pretending success; --wait-binding
            # polls until binding completes so a single call still delivers.
            if not info.get("backend_session_id", ""):
                if not getattr(args, "wait_binding", False):
                    print(json.dumps({
                        "status": "binding_pending",
                        "method": "blocked",
                        "review_key": review_key,
                        "detail": "runtime alive but backend session binding pending — "
                                  "steer cannot locate the session; message would be "
                                  "silently dropped",
                        "suggestion": "retry shortly, or pass --wait-binding",
                    }, indent=2), file=sys.stderr)
                    return 1
                # B: --wait-binding — poll the gateway's authoritative binding
                # state (runtime.info.backend_session_id) up to 60s instead of
                # scanning log files (ask has no handle).
                deadline = time.monotonic() + 60.0
                sid = ""
                while time.monotonic() < deadline:
                    pinfo = _gateway().call("runtime.info", {"review_key": review_key})
                    sid = pinfo.get("backend_session_id", "")
                    if sid:
                        break
                    time.sleep(1.0)
                if not sid:
                    print(json.dumps({
                        "status": "binding_pending",
                        "method": "blocked",
                        "review_key": review_key,
                        "detail": "backend session did not bind within --wait-binding "
                                  "window; steer would be silently dropped",
                    }, indent=2), file=sys.stderr)
                    return 1
            result = _gateway().call("runtime.send", {
                "runtime_id": info["runtime_id"],
                "from": "manager",
                "body": prompt,
                "kind": "TASK",
                "require_ack": True,
                "request_id": f"ask-{uuid4().hex[:10]}",
                "run_id": f"run-{uuid4().hex[:10]}",
            })
            # P1: method 语义修正 —— 底层为持久命令状态机（QUEUED 等 ack），
            # 不承诺已注入 turn；status 取 runtime_send 返回的已确认阶段
            # （mailbox_persisted|claimed|session_live|turn_triggered|...）。
            hot_status = result.get("status", "mailbox_persisted")
            print(json.dumps({
                "method": "hot_pending_ack",
                "review_key": review_key,
                "runtime_id": info["runtime_id"],
                "backend_session_id": info.get("backend_session_id", ""),
                "msg_id": result.get("msg_id", ""),
                "status": hot_status,
                "note": "in-loop send to live runtime (plugin steer)",
                "hint": _ask_retrieve_hint(review_key),  # E1
            }, indent=2))
            # A15: --wait — 投递成功后阻塞等新产出内联返回
            if getattr(args, "wait", False):
                return _wait_for_new_output(
                    review_key, info["runtime_id"], info.get("session_id", ""),
                    session_dir=_get_session_dir(ParkRegistry().lookup(review_key)),
                )
            return 0
        # Runtime not alive: if it died of quota exhaustion, say so
        # EXPLICITLY before silently degrading to warm/cold.
        quota = health.get("quota_error", "")
        if quota:
            print(json.dumps({
                "warning": "insufficient_quota",
                "review_key": review_key,
                "detail": quota[:300],
                "degrade_hint": "primary model quota exhausted — retry with oracle-lite or a cheaper model",
            }, indent=2), file=sys.stderr)
    except GatewayError as exc:
        # 改进项3: the hot path always degrades to warm/cold — log WHY the
        # in-loop send was skipped (gateway down, runtime gone, etc.).
        log.debug("oracle ask: hot path skipped (%s: %s) — degrading to warm/cold",
                  exc.code, exc.message)
    except Exception as exc:
        log.debug("oracle ask: hot path failed unexpectedly (%s) — degrading to warm/cold", exc)

    # ── Warm: native backend session resume ───────────────────────────
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)
    session_dir = _get_session_dir(manifest)
    # A1: warm 前置 — meta.json 绑定的 backend session 复用（manifest 优先）。
    bound_sid = _resolve_bound_session_id(review_key, manifest)
    if manifest and bound_sid:
        # P2-12: registry is created up front so a failed warm spawn can be
        # stopped from the except handler before degrading to cold.
        reg = RuntimeRegistry()
        warm_handle = None
        try:
            # Enqueue the prompt as a TASK FIRST so the resumed runtime's
            # plugin picks it up as the initial task (not a stale one).
            sid = manifest.swarm_session_id or _review_sid(review_key)
            try:
                from codeagent.mailbox.service import MailboxService

                MailboxService().send(
                    session_id=sid,
                    from_id="manager",
                    to_id=_ORACLE_AGENT,
                    subject="oracle-ask",
                    body=prompt,
                    kind="TASK",
                    run_id=f"run-{uuid4().hex[:10]}",
                    request_id=f"req-{uuid4().hex[:10]}",
                    require_ack=True,
                )
            except Exception as exc:
                print(f"warning: warm task enqueue failed: {exc}", file=sys.stderr)
            # B2: 从 manifest 读 ExecutionSpec 显式字段，不再重推导。
            # 显式 --model 覆盖 > manifest.primary_model（start 落盘的
            # spec.model）> manifest.model（start 时的显式 --model）。
            # 旧 manifest 无 primary_model 时回退 _ask_model_chain_realtime
            # （迁移兼容）。
            explicit_model = getattr(args, "model", "") or ""
            if manifest and manifest.primary_model:
                # P1-4: config fingerprint 比较——配置变更时重新推导。
                if not explicit_model and manifest.config_fingerprint:
                    cur_fp = _config_fingerprint(ask_agent_type)
                    if cur_fp and cur_fp != manifest.config_fingerprint:
                        log.warning(
                            "oracle ask warm: config fingerprint changed "
                            "(manifest=%s, current=%s) — re-deriving from config",
                            manifest.config_fingerprint, cur_fp,
                        )
                        ask_agent_type = (manifest.agent_type or "") or args.agent
                        ask_model_chain = _resolve_oracle_model_chain(ask_agent_type, "")
                        if ask_model_chain:
                            ask_primary = ask_model_chain[0]
                        else:
                            ask_primary = manifest.primary_model
                            ask_model_chain = [ask_primary]
                    else:
                        ask_primary = manifest.primary_model
                        ask_model_chain = [ask_primary]
                else:
                    ask_primary = explicit_model or manifest.primary_model
                    ask_model_chain = [ask_primary]
            else:
                # 旧 manifest 迁移兼容：走原有解析链
                ask_agent_type = (manifest.agent_type if manifest and manifest.agent_type else "") or args.agent
                ask_model_chain = _ask_model_chain_realtime(
                    ask_agent_type, manifest, explicit_override=explicit_model)
                ask_primary = ask_model_chain[0] if ask_model_chain else (manifest.model or "")
            ask_chain_env: dict[str, str] = {}
            if ask_model_chain:
                ask_chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(ask_model_chain)
            warm_handle = reg.spawn(_resolve_backend(args.agent, args.backend), {
                "session_id": sid,
                "agent_id": _ORACLE_AGENT,
                "review_key": review_key,
                "workdir": manifest.workdir or os.getcwd(),
                # U2 对齐：prompt 已由上方 mailbox enqueue（body=prompt, TASK）
                # 单通道投递；spawn task 置空避免 runtime 收到两次同一 prompt。
                # omp interactive_plugin 模式下 spawn task 仅写 spec.json 不进
                # argv，plugin 通过 gateway handshake 的 pi.sendUserMessage 消费
                # mailbox TASK 作为首任务——与 start 一致。
                "task": "",
                "model": ask_primary,
                "variant": manifest.variant if manifest else "",  # Q5: start 时落盘的 variant，warm 恢复保持一致
                "backend_session_id": bound_sid,
                "gateway_socket": str(control_socket_path()),
                "owner_pid": os.getpid(),
                "nonce": uuid4().hex[:12],
                "short_task": False,
                "env": ask_chain_env,
            })
            # P2-11: never overwrite the manifest's known backend session id
            # with an empty one — the opencode extraction window may close
            # before the session id is known, and clobbering it would destroy
            # the resume point (next warm attempt degrades to cold).
            new_backend_session_id = warm_handle.backend_session_id or bound_sid
            if not warm_handle.backend_session_id:
                print(
                    f"warning: warm: backend session id extraction window failed — "
                    f"preserving previous id {bound_sid!r}",
                    file=sys.stderr,
                )
            # Update manifest backend id + lifecycle (in-place, no UNIQUE clash).
            # D3: replace() preserves every untouched field instead of the
            # 15-field manual copy.
            registry.update(review_key, replace(
                manifest,
                lifecycle=Lifecycle.HOT_PARKED,
                backend_session_id=new_backend_session_id,
                round=manifest.round + 1,
                last_activity_at=time.time(),
                config_fingerprint=_config_fingerprint(manifest.agent_type or ""),  # P1-4: 刷新指纹
            ))
            # P2-3: snapshot after successful ask — ensures cold revive
            # gets the latest context even after a crash (previously only
            # saved on eviction).
            try:
                save_snapshot(ReviewSnapshot(
                    review_key=review_key,
                    round=manifest.round + 1,
                    last_question=prompt,
                    generated_at=time.time(),
                ))
            except Exception as exc:
                log.debug("oracle ask warm: snapshot save failed (%s)", exc)
            # I2: surface adoption failure in the success JSON.
            adopted = _adopt_runtime(review_key, sid, warm_handle,
                                     _resolve_backend(args.agent, args.backend))
            print(json.dumps({
                "method": "warm",
                "review_key": review_key,
                "runtime_id": warm_handle.runtime_id,
                "old_backend_session_id": bound_sid,
                "new_backend_session_id": new_backend_session_id,
                "adopted": adopted,
                "model_chain": ask_model_chain,
                "note": (
                    "native backend session resumed (OMP --resume / opencode --session)"
                    if new_backend_session_id else
                    "warm runtime spawned; session id pending — previous id preserved"
                ),
                "hint": _ask_retrieve_hint(review_key),  # E1
            }, indent=2))
            # A15: --wait — 投递成功后阻塞等新产出内联返回
            if getattr(args, "wait", False):
                return _wait_for_new_output(
                    review_key, warm_handle.runtime_id, sid,
                    session_dir=session_dir,
                )
            return 0
        except Exception as exc:
            # P2-12: the warm runtime was already spawned — if a later step
            # (registry.update / _adopt_runtime) raised, stop it before the
            # cold path, or the resumed process leaks.
            if warm_handle is not None:
                try:
                    reg.stop(warm_handle.runtime_id, "warm-failed-degrade-cold")
                except Exception as stop_exc:
                    log.debug("oracle ask: warm runtime stop failed (%s): %s",
                              warm_handle.runtime_id, stop_exc)
            error_text = str(exc)
            warm_fail: dict = {
                "method": "warm_failed",
                "review_key": review_key,
                "error": error_text,
                "note": "backend resume failed — degrading to cold",
            }
            if _looks_like_quota(error_text):
                warm_fail["error_type"] = "insufficient_quota"
                warm_fail["degrade_hint"] = (
                    "model quota exhausted — retry with oracle-lite or a cheaper model"
                )
            print(json.dumps(warm_fail, indent=2), file=sys.stderr)

    # ── Cold: snapshot reconstruction ─────────────────────────────────
    # P1-7: cold 分支必须注入 snapshot 上下文（build_cold_context），不能
    # 信 park_revive 返回的 rv.context——stale HOT_PARKED manifest 会让
    # revive_or_spawn 返回 method="hot" 的路由提示（"use hub send to
    # peer_agent_id=..."），round3 起该提示被当成首轮 prompt 注入重建
    # 实例（回归）。这里直接生成 snapshot 上下文，忽略决策层的 context。
    cold_context = build_cold_context(review_key)
    # B2: 从 manifest 读 ExecutionSpec 显式字段，不再重推导。
    # 显式 --model 覆盖 > manifest.primary_model（start 落盘的
    # spec.model）> manifest.model（start 时的显式 --model）。
    # 旧 manifest 无 primary_model 时回退 _ask_model_chain_realtime
    # （迁移兼容）。
    explicit_model = getattr(args, "model", "") or ""
    if manifest and manifest.primary_model:
        # P1-4: config fingerprint 比较——配置变更时重新推导。
        if not explicit_model and manifest.config_fingerprint:
            cur_fp = _config_fingerprint(ask_agent_type)
            if cur_fp and cur_fp != manifest.config_fingerprint:
                log.warning(
                    "oracle ask cold: config fingerprint changed "
                    "(manifest=%s, current=%s) — re-deriving from config",
                    manifest.config_fingerprint, cur_fp,
                )
                ask_agent_type = (manifest.agent_type or "") or args.agent
                ask_model_chain = _resolve_oracle_model_chain(ask_agent_type, "")
                if ask_model_chain:
                    cold_primary = ask_model_chain[0]
                else:
                    cold_primary = manifest.primary_model
                    ask_model_chain = [cold_primary]
            else:
                cold_primary = manifest.primary_model
                ask_model_chain = [cold_primary]
        else:
            cold_primary = explicit_model or manifest.primary_model
            ask_model_chain = [cold_primary]
    else:
        # 旧 manifest 迁移兼容：走原有解析链
        ask_agent_type = (manifest.agent_type if manifest and manifest.agent_type else "") or args.agent
        ask_model_chain = _ask_model_chain_realtime(
            ask_agent_type, manifest, explicit_override=explicit_model)
        cold_primary = ask_model_chain[0] if ask_model_chain else (
            (manifest.model if manifest else "") or explicit_model)
    cold_chain_env: dict[str, str] = {}
    if ask_model_chain:
        cold_chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(ask_model_chain)
    # P0-A: 冷路径 sid 确定性——复用 manifest 已落盘的 swarm_session_id
    # （对齐 warm 路径），否则用确定性 _review_sid（无 uuid4 后缀）。
    cold_sid = (manifest.swarm_session_id if manifest else "") or _review_sid(review_key)
    try:
        reg = RuntimeRegistry()
        handle = reg.spawn(_resolve_backend(args.agent, args.backend), {
            "session_id": cold_sid,
            "agent_id": _ORACLE_AGENT,
            "review_key": review_key,
            "workdir": manifest.workdir if manifest else os.getcwd(),
            "task": cold_context + "\n\n" + prompt,
            "model": cold_primary,
            "variant": (manifest.variant if manifest else ""),  # Q5: 冷启动重建时沿用 manifest 落盘 variant
            "gateway_socket": str(control_socket_path()),
            "owner_pid": os.getpid(),
            "nonce": uuid4().hex[:12],
            "short_task": False,
            "env": cold_chain_env,
        })
    except Exception as exc:
        error_text = str(exc)
        cold_fail: dict = {
            "method": "cold_failed",
            "review_key": review_key,
            "error": error_text,
        }
        if _looks_like_quota(error_text):
            cold_fail["error_type"] = "insufficient_quota"
            cold_fail["degrade_hint"] = (
                "model quota exhausted — retry with oracle-lite or a cheaper model"
            )
        print(json.dumps(cold_fail, indent=2), file=sys.stderr)
        return 1
    # I2: surface adoption failure in the success JSON.
    adopted = _adopt_runtime(review_key, cold_sid, handle,
                             _resolve_backend(args.agent, args.backend))
    # P1-C: 冷路径持久化 manifest——spawn+adopt 后 update（对齐
    # revive_cold/_flip_to_hot），避免冷路径每次 ask 重建 session 却不落盘，
    # 导致后续 ask 仍走冷路径（session 碎片化）。
    if manifest is not None:
        try:
            registry.update(review_key, replace(
                manifest,
                swarm_session_id=cold_sid,
                lifecycle=Lifecycle.HOT_PARKED,
                backend_session_id=handle.backend_session_id or "",
                round=manifest.round + 1,
                last_activity_at=time.time(),
                release_mode="",
                config_fingerprint=_config_fingerprint((manifest.agent_type or "") if manifest else ""),  # P1-4: 刷新指纹
            ))
        except Exception as exc:
            log.warning("oracle ask: cold manifest persist failed (%s)", exc)
        # P2-3: snapshot after successful ask — ensures cold revive
        # gets the latest context even after a crash.
        try:
            save_snapshot(ReviewSnapshot(
                review_key=review_key,
                round=manifest.round + 1,
                last_question=prompt,
                generated_at=time.time(),
            ))
        except Exception as exc:
            log.debug("oracle ask cold: snapshot save failed (%s)", exc)
    print(json.dumps({
        "method": "cold",
        "review_key": review_key,
        "runtime_id": handle.runtime_id,
        "backend_session_id": handle.backend_session_id,
        "adopted": adopted,
        "model_chain": ask_model_chain,
        "note": "snapshot reconstruction (no live/hot session)",
        "hint": _ask_retrieve_hint(review_key),  # E1
    }, indent=2))
    # A15: --wait — 投递成功后阻塞等新产出内联返回
    if getattr(args, "wait", False):
        return _wait_for_new_output(
            review_key, handle.runtime_id, cold_sid,
            session_dir=session_dir,
        )
    return 0


# ── 卡死/停滞检测（操作层防御；只告警，不自动 recover）─────────────


def _parse_iso_ts(value: str) -> Optional[float]:
    """ISO-8601 Z 时间戳 → epoch 秒。

    gateway created_at 无毫秒（``...Z``），OMP 转录带毫秒
    （``...548Z``），统一解析为 epoch 便于跨源比较。
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _is_interrupt_skip(text: str) -> bool:
    """判断工具结果文本是否为 interrupt_skipped（advisory 跳过工具）。

    OMP harness 在 system advisory 抢占式打断工具批次时生成两种载荷：
      - ``createSkippedToolResult()`` 固定文案（尚未开始的串行工具被跳过）；
      - JSON 载荷 ``{__synthetic: true, source: "interrupt_skipped", ...}``
        或 ``{__interrupted: true, source: "interrupt_skipped", ...}``。
    要求文本自身就是载荷（以固定文案或 ``{`` 开头），避免把 grep/read
    输出中恰好包含该字符串的内容误判为 skip。
    """
    t = text.strip()
    if t.startswith(_SKIP_TEXT_MARKER):
        return True
    if t.startswith("{") and '"source": "interrupt_skipped"' in t:
        return "__synthetic" in t or "__interrupted" in t
    return False


def _read_tail_lines(path: Path, n: int) -> list[str]:
    """P2-C: 读取文件尾部 n 行（seek 到末尾反向扫描，避免全量加载大文件）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)  # SEEK_END
            size = f.tell()
            # 每行约 200-500 字节 JSONL；向上估算读取块大小
            chunk = min(size, n * 1024)  # 最多读 n*1KB
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            # 第一行可能被截断（从块中间开始），丢弃
            if len(lines) > n and size > chunk:
                lines = lines[1:]
            return lines[-n:]
    except OSError:
        return []


def _scan_session_stuck_events(path: Path, since_epoch: float,
                               tail_lines: int = _STUCK_SCAN_TAIL_LINES,
                               ) -> list[tuple[str, float]]:
    """扫描 OMP 会话转录，返回 generation 窗口内的 ``(kind, ts)`` 事件序列。

    kind: ``skip``（interrupt_skipped）| ``tool_ok``（成功工具结果）|
    ``output``（assistant 文本消息）。

    为什么不用 gateway 的 TOOL_FINISHED 判"成功"：插件在 ``tool_result``
    hook 上对 skip 结果同样上报 TOOL_FINISHED（空 payload，无法区分），
    因此"成功"进度只能以转录为准。

    P2-C: 仅读取尾部 ``tail_lines`` 行（默认 200），避免大 JSONL 全量
    扫描开销。卡死/停滞检测只关心最近 generation 的事件，尾部足够覆盖。
    """
    events: list[tuple[str, float]] = []
    try:
        lines = _read_tail_lines(path, tail_lines)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if obj.get("type") != "message":
                continue
            ts = _parse_iso_ts(obj.get("timestamp", ""))
            if ts is None or ts < since_epoch:
                continue
            msg = obj.get("message", {}) or {}
            role = msg.get("role")
            if role == "toolResult":
                text = "".join(
                    c.get("text", "") for c in (msg.get("content") or [])
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                events.append(("skip" if _is_interrupt_skip(text) else "tool_ok", ts))
            elif role == "assistant":
                content = msg.get("content") or []
                text = "".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                if text.strip():
                    events.append(("output", ts))
    except OSError:
        return []
    events.sort(key=lambda e: e[1])  # 时间序
    return events


def _detect_oracle_stuck(review_key: str, info: dict,
                         session_dir: str = "") -> Optional[dict]:
    """卡死/停滞检测（只告警，不自动 recover——避免探针制造更多 advisory）。

    强信号（卡死）：同 generation 连续 ≥3 个 interrupt_skipped 事件且期间
      无成功 TOOL_FINISHED/ASSISTANT_PROGRESS（转录级：成功工具结果 /
      assistant 文本），说明 oracle 正被 advisory 反复跳过工具。
      判定“当前仍在进行”：从最新事件向前延伸连续 skip 段，遇进度事件即断。
    弱信号（停滞）：非终态 + runtime alive + 无 in-flight tool +
      排除 heartbeat 后 ≥15 分钟无 work 事件（TURN_STARTED/ASSISTANT_PROGRESS/TOOL_*）。

    返回 ``{"detected": True, "signal": ..., "detail": ..., "hint": ...}``，
    无信号返回 None（向后兼容：status JSON 不输出 stuck 字段）。
    """
    runtime_id = info.get("runtime_id", "")
    generation = info.get("generation")
    if not runtime_id or generation is None:
        return None
    agg = info.get("last_event") or {}
    last_kind = agg.get("last_event_kind", "")
    last_payload = agg.get("last_event_payload") or {}

    # 非工作态不告警：hot-park 的 oracle 在 idle 时靠 heartbeat 保活、
    # 静默等待下一条 ask（无 work 事件是正常态）——只有 agent_running
    # 且事件流停滞才值得告警。ended/agent_end 等终态同理。
    working = info.get("agent_state") == "agent_running"
    terminal = (
        info.get("status") == "stopped"
        or not working
        or (last_kind == "TASK_STATE"
            and last_payload.get("state") in _TERMINAL_TASK_STATES)
    )
    alive = bool((info.get("runtime_health") or {}).get("alive"))

    # ── 强信号：转录中的 interrupt_skipped 连续段（generation 窗口内）──
    strong_detail = ""
    backend_sid = info.get("backend_session_id", "")
    if backend_sid:
        session_path = _find_session_file(backend_sid, session_dir=session_dir)
        since = _parse_iso_ts(agg.get("first_seen_at", ""))
        if session_path is not None and since is not None:
            events = _scan_session_stuck_events(session_path, since)
            run = 0  # 当前仍在进行的连续 skip 段（从最新事件向前数）
            for kind, _ts in reversed(events):
                if kind == "skip":
                    run += 1
                else:
                    break
            if run >= _STUCK_SKIP_MIN:
                strong_detail = (
                    f"同 generation 连续 {run} 个 interrupt_skipped"
                    "（advisory 跳过工具）事件，期间无成功"
                    " TOOL_FINISHED/ASSISTANT_PROGRESS"
                )

    # ── 弱信号：gateway 事件统计（heartbeat 不落库，天然排除）──────────
    weak_detail = ""
    if not terminal and alive and not strong_detail:
        stats: dict = {}
        try:
            stats = _gateway().call("runtime.event_stats", {
                "runtime_id": runtime_id,
                "generation": generation,
            })
        except Exception as exc:
            log.warning("oracle: runtime.event_stats failed: %s", exc)
        newest = stats.get("newest") or {}
        work_ts = []
        for kind, ts in newest.items():
            if kind in _WORK_EVENT_KINDS:
                epoch = _parse_iso_ts(ts)
                if epoch is not None:
                    work_ts.append(epoch)
        # in-flight tool：最新事件是 TOOL_STARTED（尚无更新 TOOL_FINISHED）。
        in_flight = last_kind == "TOOL_STARTED"
        if work_ts and not in_flight:
            idle_s = time.time() - max(work_ts)
            if idle_s >= _STALL_IDLE_SECONDS:
                weak_detail = (
                    f"runtime alive 但 {int(idle_s // 60)} 分钟无 work 事件"
                    "（TURN_STARTED/ASSISTANT_PROGRESS/TOOL_*，排除 heartbeat）"
                    "且无 in-flight tool"
                )

    if strong_detail:
        return {
            "detected": True,
            "signal": "strong",
            "detail": strong_detail,
            "hint": (
                f"疑似卡死（advisory 跳过工具）：建议 `aimeshchat oracle release"
                f" {review_key}` 后 `aimeshchat oracle revive {review_key}` 恢复；"
                "仅告警不自动 recover（避免探针制造更多 advisory）"
            ),
        }
    if weak_detail:
        return {
            "detected": True,
            "signal": "weak",
            "detail": weak_detail,
            "hint": (
                f"疑似停滞：建议 `aimeshchat oracle release {review_key}` 后"
                f" `aimeshchat oracle revive {review_key}` 恢复；仅告警不自动 recover"
            ),
        }
    return None


def cmd_oracle_status(args: argparse.Namespace) -> int:
    """Aggregate receipt/progress/park for a review key."""
    review_key = args.review_key
    out: dict = {"review_key": review_key}

    registry = ParkRegistry()
    manifest = registry.lookup(review_key)
    session_dir = _get_session_dir(manifest)
    out["park"] = (
        {
            "lifecycle": manifest.lifecycle.value,
            "round": manifest.round,
            "backend_session_id": manifest.backend_session_id,
            "agent_type": manifest.agent_type,
            "last_activity_at": manifest.last_activity_at,
        }
        if manifest else None
    )
    # P2-3: snapshot freshness — surface age so users can see how stale
    # the cold-revive context would be.
    if manifest:
        age = _snapshot_age_days(manifest)
        out["snapshot"] = {
            "age_days": round(age, 1) if age >= 0 else None,
            "stale": age > _SNAPSHOT_STALE_DAYS if age >= 0 else True,
            "threshold_days": _SNAPSHOT_STALE_DAYS,
        }

    # Runtime observability (EventStore aggregation).
    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        out["runtime"] = {
            "runtime_id": info.get("runtime_id"),
            "status": info.get("status"),
            "elapsed_s": info.get("elapsed"),
            "backend_session_id": info.get("backend_session_id"),
            "generation": info.get("generation"),
            "last_event": info.get("last_event", {}),
            "tool_stats": info.get("tool_stats", {}),
            "runtime_health": info.get("runtime_health", {}),
        }
        # Quota exhaustion must be surfaced EXPLICITLY (not as a generic
        # timeout/dead-runtime) — with a concrete degradation hint.
        quota = (info.get("runtime_health") or {}).get("quota_error", "")
        if quota:
            out["runtime"]["quota_error"] = quota
            out["runtime"]["degrade_hint"] = (
                "model quota exhausted — retry with oracle-lite or a cheaper model, "
                "or wait for quota reset"
            )
    except GatewayError as exc:
        # 改进项2: gateway 不在时给结构化降级 + 修复提示，而不是裸 error 字符串。
        if exc.code in ("GATEWAY_DOWN", "GATEWAY_CONNECT_FAILED"):
            out["runtime"] = {
                "status": "gateway_down",
                "hint": "run 'aimeshchat gateway start'",
            }
        else:
            out["runtime"] = {"status": "unavailable", "error": exc.message}
    except Exception as exc:
        out["runtime"] = {"status": "unavailable", "error": str(exc)}

    # 卡死/停滞检测（操作层防御；只告警，不自动 recover）。
    # 仅当 runtime.info 真正成功（非 gateway_down/unavailable 降级）才检测；
    # 检测失败不阻塞 status 输出（向后兼容，无卡死时不输出 stuck 字段）。
    if out["runtime"].get("status") not in ("gateway_down", "unavailable"):
        try:
            stuck = _detect_oracle_stuck(review_key, info, session_dir=session_dir)
            if stuck:
                out["stuck"] = stuck
                print(
                    f"[oracle] {stuck['detail']}\n"
                    f"  → {stuck['hint']}",
                    file=sys.stderr,
                )
        except Exception as exc:
            log.warning("oracle: stuck detection skipped: %s", exc)

    # Request ledger (terminal truth).
    store = MailboxStore()
    reqs: list[dict] = []
    if manifest and manifest.swarm_session_id:
        try:
            events_root = store.session_dir(manifest.swarm_session_id) / _ORACLE_AGENT / "events"
            if events_root.is_dir():
                for req_dir in sorted(events_root.iterdir()):
                    if not req_dir.is_dir():
                        continue
                    lg = RequestLedger(store.session_dir(manifest.swarm_session_id), _ORACLE_AGENT)
                    # I4: use the public API — never reach into the private
                    # _read_entries_all_runs implementation.
                    for run_id, evs in lg.get_entries_all_runs(req_dir.name).items():
                        reqs.append({
                            "request_id": req_dir.name,
                            "run_id": run_id,
                            "states": [e["event"] for e in evs],
                            "terminal": next((e["event"] for e in evs if e["event"] in {"DONE", "BLOCKED", "CANCELLED", "EXPIRED", "UNKNOWN_STALE"}), ""),
                            "_mtime": req_dir.stat().st_mtime,
                        })
        except Exception:
            reqs = []
    # P3-8: sort by filesystem mtime (creation/modification order), not
    # request_id lexicographic order — hex IDs don't sort chronologically.
    reqs.sort(key=lambda r: r.get("_mtime", 0))
    for r in reqs:
        r.pop("_mtime", None)
    out["requests"] = reqs[-5:]  # most recent by time

    # Latest READ receipt / progress from history.
    if manifest and manifest.swarm_session_id:
        try:
            store = MailboxStore()
            history = store.read_history(manifest.swarm_session_id, limit=50)
            receipts = [m for m in history if m.get("kind") == "RECEIPT"]
            out["receipts"] = [
                {"msg_id": m.get("msg_id"), "reply_to": m.get("reply_to"), "from": m.get("from")}
                for m in receipts[-3:]
            ]
        except Exception:
            out["receipts"] = []

    # B2: mailbox unread aggregation — unread count + latest REPORT preview +
    # recommendation.  Inbox files == delivered-but-not-read (two-phase
    # consumption: inbox → processing → archive).
    if manifest and manifest.swarm_session_id:
        try:
            inbox_dir = store.agent_subdir(manifest.swarm_session_id, _ORACLE_AGENT, "inbox")
            unread = 0
            latest_report: Optional[dict] = None
            for f in store.list_messages(inbox_dir):
                try:
                    msg = json.loads(f.read_bytes())
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
                unread += 1
                if msg.get("kind") == "REPORT":
                    created = msg.get("created_at", "")
                    if latest_report is None or created >= latest_report.get("created_at", ""):
                        latest_report = {
                            "msg_id": msg.get("msg_id"),
                            "from": msg.get("from"),
                            "created_at": created,
                            "subject": (msg.get("subject") or "")[:120],
                            "body_preview": (msg.get("body") or "")[:300],
                        }
            if unread == 0:
                recommendation = "none"
            elif latest_report is not None:
                recommendation = "read or ack REPORT before release"
            else:
                recommendation = "read inbox before release"
            out["mailbox"] = {
                "unread": unread,
                "latest_report": latest_report,
                "recommendation": recommendation,
            }
        except Exception:
            out["mailbox"] = None
    else:
        out["mailbox"] = None

    print(json.dumps(out, indent=2))
    return 0


# ── list（E2：列出所有 park review）───────────────────────────────────


def cmd_oracle_list(args: argparse.Namespace) -> int:
    """List every parked oracle review (ParkRegistry.list_active).

    E2: ``ParkRegistry.list_active()`` already existed but had no CLI
    entry — ``aimeshchat oracle list`` surfaces it. Each entry carries
    the manifest's lifecycle (HOT_PARKED for active instances) plus the
    fields needed to pick a next action (ask/status/result/release).
    """
    registry = ParkRegistry()
    reviews: list[dict] = []
    for m in registry.list_active():
        reviews.append({
            "review_key": m.review_key,
            "lifecycle": m.lifecycle.value,
            "agent_type": m.agent_type or "",
            "backend_session_id": m.backend_session_id or "",
            "swarm_session_id": m.swarm_session_id or "",
            "round": m.round,
            "model": m.model or "",
            "last_activity_at": m.last_activity_at,
        })
    print(json.dumps({"reviews": reviews}, indent=2))



    return 0


# ── result：从 OMP 会话转录提取最新回答 ───────────────────────────────


def _get_session_dir(manifest) -> str:
    """Extract the OMP session directory from a park manifest.

    Returns the parent directory of ``manifest.omp_session_path`` (the
    ``~/.omp/agent/sessions/<dir>/`` containing the session JSONL).
    Empty string means "use the default global root" — callers MUST
    treat a non-empty return as the SOLE search root to prevent stale
    matches from old sessions in the default directory.
    """
    raw = getattr(manifest, "omp_session_path", "") or ""
    if not raw:
        return ""
    return str(Path(raw).parent)


def _find_session_file(backend_session_id: str,
                       session_dir: str = "") -> Optional[Path]:
    """Locate the OMP session transcript file for a backend session id.

    Session files live under ~/.omp/agent/sessions/<dir>/*_<session_id>.jsonl
    where <dir> is the cwd-derived name (e.g. -src-codeagent-py). Falls back
    to scanning all session dirs when the derived dir misses.

    When *session_dir* is non-empty, ONLY that directory is searched — this
    prevents stale matches from old sessions in the default root.
    """
    if session_dir:
        search_root = Path(session_dir)
        if not search_root.is_dir():
            return None
    else:
        search_root = Path.home() / ".omp" / "agent" / "sessions"
        if not search_root.is_dir():
            return None

    def _candidate_dirs() -> list[Path]:
        dirs: list[Path] = []
        for d in search_root.iterdir():
            if not d.is_dir():
                continue
            if any(f.name.endswith(f"_{backend_session_id}.jsonl") for f in d.iterdir()):
                dirs.append(d)
        return dirs

    candidates = _candidate_dirs()
    if not candidates:
        return None
    best: Optional[Path] = None
    for d in candidates:
        for f in sorted(d.glob(f"*_{backend_session_id}.jsonl")):
            if best is None or f.stat().st_mtime > best.stat().st_mtime:
                best = f
    return best


def _extract_assistant_messages(path: Path, max_messages: int = 1) -> list[str]:
    """Extract the last *max_messages* assistant text messages from a session JSONL."""
    msgs: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") != "message":
                    continue
                m = obj.get("message", {})
                if m.get("role") != "assistant":
                    continue
                content = m.get("content", [])
                text = "".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
                if text.strip():
                    msgs.append(text)
    except OSError:
        return []
    return msgs[-max_messages:]


def _fallback_find_session_for_key(review_key: str, since: Optional[float] = None,
                                   session_dir: str = "") -> Optional[Path]:
    """A2-③ filesystem source: recursive scan, EXACT review_key match, start-time window.

    Recursively walks the session root for .jsonl files and scores
    candidates instead of first-hit matching — a short tail segment
    (e.g. ``blur`` from ``proj:oracle:gfx:blur``) previously matched
    unrelated sessions mentioning that word.  Scoring:
      +100  full *review_key* appears EXACTLY (word-boundary, not substring)
      +10   file lives under an oracle session dir (``ora-*`` / ``__advisor``)
      +1    specific tail segment (>= 5 chars) appears
    ``since`` (epoch mtime) filters out sessions written before the review
    started. A key-derived signal (full-key exact OR specific tail) is
    REQUIRED — the ``ora-*`` dir bonus alone never matches an unrelated key.
    Returns the best-scoring file (or ``None`` when nothing scores).

    When *session_dir* is non-empty, ONLY that directory is walked — this
    prevents stale matches from old sessions in the default root.
    """
    if session_dir:
        search_root = Path(session_dir)
        if not search_root.is_dir():
            return None
    else:
        search_root = Path.home() / ".omp" / "agent" / "sessions"
        if not search_root.is_dir():
            return None

    # A2: recursive walk — every session file anywhere under the root.
    all_files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(search_root):
        for name in filenames:
            if name.endswith(".jsonl"):
                all_files.append(Path(dirpath) / name)
    if not all_files:
        return None

    # Sort by mtime descending; drop files older than *since* (the review
    # start), keep the 5 most recent within the window.
    recent: list[Path] = []
    for f in all_files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if since is not None and mtime < since:
            continue
        recent.append((mtime, f))
    recent.sort(key=lambda t: t[0], reverse=True)
    recent = [f for _m, f in recent[:5]]

    # Derive tokens: full review_key first (most specific), then the tail
    # segment ONLY when it is specific enough (>= 5 chars) — short generic
    # segments ("blur", "v1") cause false positives against old sessions.
    tail = review_key.rsplit(":", 1)[-1].strip()
    full_key = review_key if len(review_key) >= 6 else ""
    tail_ok = tail and tail != review_key and len(tail) >= 5

    def _exact(lower: str, needle: str) -> bool:
        """A2: word-boundary containment — 'proj:oracle:gfx:blur' must NOT
        match 'proj:oracle:gfx:blur2' (substring false positive). Empty
        before/after (string start/end) counts as a boundary."""
        _alpha = "abcdefghijklmnopqrstuvwxyz_0123456789"
        idx = 0
        while True:
            idx = lower.find(needle, idx)
            if idx < 0:
                return False
            before = lower[idx - 1] if idx > 0 else ""
            after = lower[idx + len(needle)] if idx + len(needle) < len(lower) else ""
            if (not before or before not in _alpha) and (not after or after not in _alpha):
                return True
            idx += len(needle)
        return False

    best: Optional[Path] = None
    best_score = 0
    best_key_score = 0  # A2: key-derived signal (full-key exact / tail) —
                        # the ora-* dir bonus alone must NOT match an
                        # unrelated key (精确 review_key 匹配要求).
    for f in recent:
        # Root-cause fix (2026-08-12): __advisor*.jsonl is a SEPARATE monitor
        # session (advisor meta-comments), not the main agent's answer. It
        # must never be selected — previously it got a +10 dir bonus AND its
        # text often contains the review_key, so a stale advisor file could
        # beat the real (recent) main session. Skip advisor files outright.
        if "__advisor" in f.name.lower():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        key_score = 0
        if full_key and _exact(lower, full_key.lower()):
            key_score += 100
        if tail_ok and _exact(lower, tail.lower()):
            key_score += 1
        if key_score == 0:
            continue  # no key signal — skip regardless of dir bonus
        score = key_score
        if any(p in f.name.lower() or p in str(f.parent).lower() for p in ("postmesh-", "ora-")):
            score += 10
        if score > best_score:
            best, best_score, best_key_score = f, score, key_score
    return best if best_key_score > 0 else None


def _review_start_ts(review_key: str, manifest) -> Optional[float]:
    """A2: review start timestamp (epoch) — meta bound_at → manifest created_at.

    Used as the lower bound of the filesystem source's time window (only
    session files modified AFTER the review started are considered).
    """
    meta = _read_oracle_meta(review_key)
    bound_at = meta.get("bound_at", "")
    if bound_at:
        try:
            return datetime.fromisoformat(bound_at.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            pass
    if manifest is not None and getattr(manifest, "created_at", 0):
        try:
            return float(manifest.created_at)
        except (TypeError, ValueError):
            return None
    return None


def _review_reply_to_candidates(review_key: str) -> set[str]:
    """A2: plausible ``reply_to`` encodings for a review key.

    Mailbox ``reply_to`` is validated as a safe path component — a raw
    review key with ':' (proj:oracle:gfx:blur) can never appear verbatim.
    Senders sanitize: full substitution (proj-oracle-gfx-blur), the short
    slug used by tmux (``replace(':', '-')[-12:]``), or — since I3 — the
    sha256 prefix used by ``_review_sid``. The old truncation slug is kept
    for backward compatibility with pre-I3 sessions.
    """
    safe = review_key.replace(":", "-").replace("/", "-").replace("\\", "-")
    slug = review_key.replace(":", "-")[-12:]
    hash_slug = hashlib.sha256(review_key.encode("utf-8")).hexdigest()[:12]
    return {review_key, safe, slug, hash_slug}


def _scan_mailbox_report(review_key: str, manifest) -> Optional[str]:
    """A2-② mailbox source: latest REPORT envelope answering the review key.

    Matches REPORT messages whose ``reply_to`` encodes the review key
    (raw / sanitized / slug — see _review_reply_to_candidates). Scans the
    swarm session history (manifest → meta), then falls back to a recursive
    scan of the whole mailbox root (a plugin may report under a different
    session). Returns the REPORT body when found, else None.
    """
    store = MailboxStore()
    swarm_ids: list[str] = []
    if manifest is not None and getattr(manifest, "swarm_session_id", ""):
        swarm_ids.append(manifest.swarm_session_id)
    meta = _read_oracle_meta(review_key)
    if meta.get("swarm_session_id"):
        swarm_ids.append(meta["swarm_session_id"])
    reply_keys = _review_reply_to_candidates(review_key)

    def _match(msg: dict) -> bool:
        return (msg.get("kind") == "REPORT"
                and (msg.get("reply_to") or "") in reply_keys)

    # Known sessions first — canonical history, newest first.
    for swarm_id in dict.fromkeys(swarm_ids):
        try:
            for msg in store.read_history(swarm_id, kind="REPORT"):
                if _match(msg):
                    return msg.get("body", "") or ""
        except Exception:
            continue
    # Fallback: bounded recursive scan of every session history directory.
    # P2: cap at 200 files to prevent runaway scans on large mailboxes.
    try:
        candidates: list[tuple[float, dict]] = []
        file_count = 0
        scan_done = False
        for dirpath, _dirs, files in os.walk(resolve_root()):
            if scan_done:
                break
            if Path(dirpath).name != "history":
                continue
            for name in files:
                file_count += 1
                if file_count > 200:
                    scan_done = True
                    break
                if not name.endswith(".json"):
                    continue
                p = Path(dirpath) / name
                try:
                    msg = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if _match(msg):
                    try:
                        candidates.append((p.stat().st_mtime, msg))
                    except OSError:
                        continue
        if candidates:
            candidates.sort(key=lambda t: t[0], reverse=True)
            return candidates[0][1].get("body", "") or ""
    except Exception:
        pass
    return None


def _truncate_result(text: str, max_bytes: int) -> tuple[str, bool, int, int]:
    """P1: UTF-8 安全截断——行边界优先，退让空白。

    返回 (truncated_text, was_truncated, truncated_bytes, total_bytes)。
    max_bytes <= 0 表示不截断。
    """
    if max_bytes <= 0:
        return text, False, 0, len(text.encode("utf-8"))
    total = len(text.encode("utf-8"))
    if total <= max_bytes:
        return text, False, 0, total
    # 截断：找 max_bytes 边界，不劈多字节
    truncated = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    # 优先行边界：从末尾向前找最近的 \n（在后 30% 范围内）
    search_start = max(0, int(len(truncated) * 0.7))
    nl_pos = truncated.rfind("\n", search_start)
    if nl_pos > search_start:
        truncated = truncated[:nl_pos]
    else:
        # 退让空白字符
        ws_pos = truncated.rfind(" ", search_start)
        if ws_pos > search_start:
            truncated = truncated[:ws_pos]
    return truncated, True, len(truncated.encode("utf-8")), total


def _trunc_notice(text: str, was_trunc: bool, trunc_bytes: int, total_bytes: int) -> str:
    """P0-2: append tail notice when truncation occurred."""
    if was_trunc:
        return f"{text}\n\n…[truncated {trunc_bytes}/{total_bytes} bytes]"
    return text


def cmd_oracle_result(args: argparse.Namespace) -> int:
    """A2: unified result extraction — session transcript → mailbox REPORT → FS scan.

    Source priority (first hit wins):
      ① session_transcript — meta.json/manifest backend_session_id → the
         standard session-file API (_find_session_file + extract)
      ② mailbox_report    — swarm history REPORT envelope with
         reply_to == review_key (the task's terminal outcome)
      ③ filesystem        — recursive scan matching review_key EXACTLY with
         an mtime window after the review started (fallback scan)

    Output is JSON carrying source/confidence so callers can weigh the
    answer. ``--all`` returns every assistant message, default the latest.
    """
    review_key = args.review_key
    strict = bool(getattr(args, "strict", False))
    want_all = bool(getattr(args, "all", False))
    raw_mode = bool(getattr(args, "raw", False))
    include_digest = bool(getattr(args, "include_digest", False))
    manifest = ParkRegistry().lookup(review_key)
    session_dir = _get_session_dir(manifest)
    start_since = _review_start_ts(review_key, manifest)
    max_msgs = 10**6 if want_all else 1
    # P1: 截断上限——--all 跳过上限，否则读环境变量或默认值
    max_bytes = 0 if want_all else int(
        os.environ.get("ORACLE_RESULT_MAX_BYTES", _DEFAULT_RESULT_MAX_BYTES))

    # P2-2: pre-load advisor digest (once) when --include-digest requested.
    _cached_digest: Optional[dict] = None
    if include_digest:
        _cached_digest = _load_advisor_digest(review_key)

    out: dict = {
        "review_key": review_key,
        "source": "",
        "confidence": 0.0,
        "messages": [],
        "meta": {"strict": strict, "all": want_all},
    }

    # ── ① session transcript（meta.json session_id → 标准 API）────────
    bound_sid = _resolve_bound_session_id(review_key, manifest)
    if bound_sid:
        out["meta"]["session_id"] = bound_sid
        path = _find_session_file(bound_sid, session_dir=session_dir)
        if path is not None:
            msgs = _extract_assistant_messages(path, max_messages=max_msgs)
            if msgs:
                display_text = msgs[-1] if len(msgs) == 1 else "\n".join(msgs)
                trunc_text, was_trunc, trunc_bytes, total_bytes = _truncate_result(
                    display_text, max_bytes)
                out.update({
                    "source": "session_transcript",
                    "confidence": 0.95,
                    "messages": [trunc_text],
                })
                out["meta"]["path"] = str(path)
                if was_trunc:
                    out["truncated"] = True
                    out["truncated_bytes"] = trunc_bytes
                    out["total_bytes"] = total_bytes
                    out["hint"] = "use --all for full result"
                else:
                    out["truncated"] = False
                # P2-2: inject advisor digest into result when requested.
                if _cached_digest is not None:
                    out["advisor_digest"] = _cached_digest
                if raw_mode:
                    print(_trunc_notice(trunc_text, was_trunc, trunc_bytes, total_bytes))
                else:
                    print(json.dumps(out, indent=2))
                # P2-1: 运行时裁剪已完成 turn 的旧 event（防 opencode.db 无界增长）。
                # strip 失败绝不阻塞主流程——任何异常都吞掉。
                try:
                    if bound_sid and _is_opencode_session_id(bound_sid):
                        _strip_running_session(bound_sid)
                except Exception:
                    pass
                return 0

    # ── ② mailbox REPORT（reply_to == review_key 的终端信封）─────────
    report = _scan_mailbox_report(review_key, manifest)
    if report:
        trunc_text, was_trunc, trunc_bytes, total_bytes = _truncate_result(
            report, max_bytes)
        out.update({
            "source": "mailbox_report",
            "confidence": 0.9,
            "messages": [trunc_text],
        })
        if was_trunc:
            out["truncated"] = True
            out["truncated_bytes"] = trunc_bytes
            out["total_bytes"] = total_bytes
            out["hint"] = "use --all for full result"
        else:
            out["truncated"] = False
        # P2-2: inject advisor digest into result when requested.
        if _cached_digest is not None:
            out["advisor_digest"] = _cached_digest
        if raw_mode:
            print(_trunc_notice(trunc_text, was_trunc, trunc_bytes, total_bytes))
        else:
            print(json.dumps(out, indent=2))
        return 0

    # ── ③ filesystem（递归 + 精确 key + start 后时间窗）──────────────
    if not strict:
        try:
            path = _fallback_find_session_for_key(review_key, since=start_since,
                                                  session_dir=session_dir)
        except Exception:  # pragma: no cover — defensive
            path = None
        if path is not None:
            msgs = _extract_assistant_messages(path, max_messages=max_msgs)
            if msgs:
                display_text = msgs[-1] if len(msgs) == 1 else "\n".join(msgs)
                trunc_text, was_trunc, trunc_bytes, total_bytes = _truncate_result(
                    display_text, max_bytes)
                out.update({
                    "source": "filesystem",
                    "confidence": 0.7,
                    "messages": [trunc_text],
                })
                out["meta"]["path"] = str(path)
                out["meta"]["since"] = start_since
                if was_trunc:
                    out["truncated"] = True
                    out["truncated_bytes"] = trunc_bytes
                    out["total_bytes"] = total_bytes
                    out["hint"] = "use --all for full result"
                else:
                    out["truncated"] = False
                # P2-2: inject advisor digest into result when requested.
                if _cached_digest is not None:
                    out["advisor_digest"] = _cached_digest
                if raw_mode:
                    print(_trunc_notice(trunc_text, was_trunc, trunc_bytes, total_bytes))
                else:
                    print(json.dumps(out, indent=2))
                return 0

    # ── Nothing found ─────────────────────────────────────────────────
    # U3: error output is JSON to stderr (consistent with ask/revive).
    if strict:
        detail = (f"strict mode — filesystem fallback refused; "
                  f"backend_session_id={bound_sid!r}")
    elif not bound_sid:
        detail = "oracle start 后才有（no bound backend session）"
    else:
        detail = (f"session {bound_sid!r} not found; no mailbox REPORT; "
                  f"no matching session file")
    print(json.dumps({
        "error": "no_result",
        "review_key": review_key,
        "strict": strict,
        "detail": detail,
    }, indent=2), file=sys.stderr)
    return 1


def cmd_oracle_watch(args: argparse.Namespace) -> int:
    """Watch a review's runtime events (cursor-resumable)."""
    review_key = args.review_key
    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        runtime_id = info.get("runtime_id", "")
        session_id = info.get("session_id", "")
    except GatewayError as exc:
        # U3: error output is JSON to stderr.
        print(json.dumps({
            "error": exc.code,
            "message": exc.message,
            "review_key": review_key,
        }, indent=2), file=sys.stderr)
        return 1

    from codeagent.gateway.cli import cmd_events_watch

    ns = argparse.Namespace(
        ev_cmd="watch",
        session=session_id,
        request_id="",
        runtime_id=runtime_id,
        cursor=args.cursor,
        filters="",
        limit=200,
        interval=args.interval,
        timeout=args.timeout,
        # 改进项1: human-readable by default; --jsonl is opt-in.
        jsonl=bool(getattr(args, "jsonl", False)),
        plain=bool(getattr(args, "plain", False)),
        exit_on=getattr(args, "exit_on", ""),
        max_events=getattr(args, "max_events", 0),
        duration=getattr(args, "duration", 0.0),
    )
    return cmd_events_watch(ns)


# ── wait（B1：阻塞到 agent_end 事件，然后内联最终文本）────────────────


def _wait_final_text(review_key: str, session_dir: str = "") -> Optional[str]:
    """B1: extract the latest assistant text for inline printing on agent_end.

    Same sources as ``oracle result`` (primary backend session transcript,
    then best-effort scan) but returns the text instead of printing.
    """
    manifest = ParkRegistry().lookup(review_key)
    # Root-cause fix (2026-08-12): resolve via lazy-sync so a bound backend
    # sid (persisted only after plugin handshake) is backfilled — previously
    # manifest/meta stayed empty and _find_session_file("") fell through to
    # the score-based fallback, which could pick a stale __advisor session.
    backend_id = _resolve_bound_session_id(review_key, manifest)
    path = _find_session_file(backend_id, session_dir=session_dir) if backend_id else None
    if path is None:
        try:
            path = _fallback_find_session_for_key(review_key, session_dir=session_dir)
        except Exception:  # pragma: no cover — defensive
            path = None
    if path is None:
        return None
    msgs = _extract_assistant_messages(path, max_messages=1)
    return msgs[-1] if msgs else None


def _wait_for_new_output(review_key: str, runtime_id: str, session_id: str,
                         timeout: float = 300.0, interval: float = 5.0,
                         max_bytes: int = 0, info: Optional[dict] = None,
                         auto_recover: bool = False,
                         session_dir: str = "") -> int:
    """A15: 等待 oracle 新产出并内联打印——供 cmd_oracle_wait 和 ask --wait 共用。

    等待方式：boot drain 设高水位 cursor，主轮询只看 cursor 后新事件。
    ASSISTANT_PROGRESS（新产出）或 agent_end（兜底）触发返回。
    不做 baseline 文本对比（JSONL 更新/截断曾误触发旧内容返回）。
    cursor 后的 ASSISTANT_PROGRESS 一定是新 turn 的产出。

    返回 0 = 命中新产出并打印, 1 = 超时或强卡死信号, 130 = KeyboardInterrupt。
    """
    # boot drain：设高水位 cursor，后续只看新事件（不再 baseline 文本对比）。
    cursor: int = 0
    deadline = time.monotonic() + timeout

    # 内联卡死检测所需 runtime info——未传时回退拉取（供 ask --wait 等路径）。
    if info is None:
        try:
            info = _gateway().call("runtime.info", {"review_key": review_key})
        except GatewayError:
            info = {}
    polls: int = 0

    # P1-1: 初始全量 drain——先检查是否已有 agent_end 事件落地
    try:
        boot = _gateway().call("events.list", {
            "cursor": 0,
            "filters": ["TASK_STATE"],
            "limit": 10_000,
            "session_id": session_id,
            "runtime_id": runtime_id,
        })
        for ev in boot.get("events", []):
            if ev.get("kind") == "TASK_STATE" and (ev.get("payload") or {}).get("state") == "agent_end":
                final = _wait_final_text(review_key, session_dir=session_dir)
                if final is not None:
                    trunc, was_trunc, t_bytes, total = _truncate_result(final, max_bytes)
                    print(_trunc_notice(trunc, was_trunc, t_bytes, total))
                return 0
        cursor = int(boot.get("cursor") or 0)
    except GatewayError:
        pass  # 落入主循环重试

    # P1-1: auto-recover 一次性标志
    _recovered = False

    # 主轮询循环：TASK_STATE + ASSISTANT_PROGRESS 双 filter
    while True:
        try:
            result = _gateway().call("events.list", {
                "cursor": cursor,
                "filters": ["TASK_STATE", "ASSISTANT_PROGRESS"],
                "limit": 200,
                "session_id": session_id,
                "runtime_id": runtime_id,
            })
        except GatewayError as exc:
            print(json.dumps({"status": "error", "error": exc.code,
                              "message": exc.message}, indent=2))
            return 1
        for ev in result.get("events", []):
            kind = ev.get("kind")
            payload = ev.get("payload") or {}
            if kind == "ASSISTANT_PROGRESS":
                # cursor 后的 ASSISTANT_PROGRESS 一定是新 turn 产出，无需 baseline 文本对比
                # （baseline 对比曾因 JSONL 更新/截断误触发旧内容返回）。
                # P2-0d: 不打印 quick——避免与 final 重叠；quick 仅作产出存在信号。
                final = _wait_final_text(review_key, session_dir=session_dir)
                if final is not None:
                    trunc, was_trunc, t_bytes, total = _truncate_result(final, max_bytes)
                    print(_trunc_notice(trunc, was_trunc, t_bytes, total))
                    # P2-1: 运行时裁剪已完成 turn 的旧 event（防 opencode.db 无界增长）。
                    # strip 失败绝不阻塞主流程——任何异常都吞掉。
                    try:
                        _manifest = ParkRegistry().lookup(review_key)
                        _backend_sid = _resolve_bound_session_id(review_key, _manifest)
                        if _backend_sid and _is_opencode_session_id(_backend_sid):
                            _strip_running_session(_backend_sid)
                    except Exception:
                        pass
                    return 0
                continue  # 无文本（罕见）——继续等
            if kind == "TASK_STATE" and payload.get("state") == "agent_end":
                final = _wait_final_text(review_key, session_dir=session_dir)
                if final is not None:
                    trunc, was_trunc, t_bytes, total = _truncate_result(final, max_bytes)
                    print(_trunc_notice(trunc, was_trunc, t_bytes, total))  # B1: 内联最终文本
                return 0
        cursor = int(result.get("cursor") or cursor or 0)
        polls += 1
        # 每 3 次轮询内联卡死检测。
        if polls % 3 == 0:
            # P2-0d-2: re-fetch info from gateway to ensure stuck detection uses fresh state.
            try:
                info = _gateway().call("runtime.info", {"review_key": review_key})
            except GatewayError:
                pass
            stuck = _detect_oracle_stuck(review_key, info, session_dir=session_dir)
            if stuck and stuck.get("signal") == "strong":
                # P1-1: auto-recover — 仅尝试一次
                if auto_recover and not _recovered:
                    _recovered = True
                    print(json.dumps({"status": "recovering", "reason": "stuck",
                                      "hint": stuck.get("hint", "")}, indent=2))
                    # Step 1: soft release（停 runtime + 置 park 为 released_soft）
                    try:
                        _info = _gateway().call("runtime.info",
                                                {"review_key": review_key})
                        _rid = _info.get("runtime_id")
                        if _rid:
                            _gateway().call("runtime.stop",
                                            {"runtime_id": _rid,
                                             "reason": "auto-recover stuck"})
                            _gateway().call("runtime.purge_stopped",
                                            {"review_key": review_key})
                    except Exception as exc:
                        print(json.dumps({"status": "recover_failed",
                                          "phase": "release",
                                          "error": str(exc)}, indent=2))
                        return 1
                    try:
                        ParkRegistry().release(review_key, mode="soft")
                    except Exception as exc:
                        print(json.dumps({"status": "recover_failed",
                                          "phase": "release_park",
                                          "error": str(exc)}, indent=2))
                        return 1
                    # Step 2: revive（warm 或 cold，复用 cmd_oracle_revive 决策树）
                    try:
                        from codeagent.park.router import revive_or_spawn as _ros
                        manifest = ParkRegistry().lookup(review_key)
                        if manifest is None:
                            raise RuntimeError("park row gone after release")
                        decision = _ros(review_key,
                                        is_alive=_is_runtime_alive)
                        if decision.method == "warm":
                            _revive_warm(review_key, manifest, "bg")
                        else:
                            _revive_cold(review_key, manifest, "bg")
                    except Exception as exc:
                        print(json.dumps({"status": "recover_failed",
                                          "phase": "revive",
                                          "error": str(exc)}, indent=2))
                        return 1
                    # Step 3: 刷新 runtime 信息，继续主循环
                    try:
                        info = _gateway().call("runtime.info",
                                               {"review_key": review_key})
                        runtime_id = info.get("runtime_id", "")
                        session_id = info.get("session_id", "")
                        if info.get("status") != "active":
                            print(json.dumps(
                                {"status": "recover_failed",
                                 "phase": "post_revive",
                                 "error": "runtime not active after revive"},
                                indent=2))
                            return 1
                    except Exception as exc:
                        print(json.dumps({"status": "recover_failed",
                                          "phase": "post_revive",
                                          "error": str(exc)}, indent=2))
                        return 1
                    deadline = time.monotonic() + timeout  # P2-0d-3: reset deadline for new runtime
                    continue  # 主循环将用新 runtime 继续轮询
                # auto_recover=False（默认）或已尝试过：保持原行为
                print(json.dumps({"status": "stuck",
                                  "hint": stuck.get("hint", "")}, indent=2))
                return 1
        if time.monotonic() >= deadline:
            print(json.dumps({"status": "timeout",
                              "suggestion": "use oracle result"}, indent=2))
            return 1
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 130


def cmd_oracle_wait(args: argparse.Namespace) -> int:
    """B1+A3: block until NEW assistant output (or agent_end), print final text.

    Polls the gateway event stream every ``--interval`` (default 5s) up to
    ``--timeout`` (default 300s).  Waits for a NEW ``ASSISTANT_PROGRESS``
    event (parked oracle emits this on each produced turn while staying
    active — agent_end never fires because auto-exit/park keep the runtime
    alive) or a terminal ``TASK_STATE.agent_end``.  On output, prints the
    final assistant text inline and exits 0.  On timeout, emits
    ``{status: timeout, suggestion: "use oracle result"}`` and returns 1.
    """
    review_key = args.review_key
    session_dir = _get_session_dir(ParkRegistry().lookup(review_key))
    interval = max(0.1, float(getattr(args, "interval", 5.0) or 5.0))
    timeout = max(0.0, float(getattr(args, "timeout", 300.0) or 300.0))
    # P1: 截断上限（--all 跳过上限）
    max_bytes = 0 if getattr(args, "all", False) else int(
        os.environ.get("ORACLE_RESULT_MAX_BYTES", _DEFAULT_RESULT_MAX_BYTES))

    try:
        info = _gateway().call("runtime.info", {"review_key": review_key})
        runtime_id = info.get("runtime_id", "")
        session_id = info.get("session_id", "")
    except GatewayError as exc:
        print(json.dumps({"status": "error", "error": exc.code,
                          "message": exc.message}, indent=2))
        return 1
    if not runtime_id:
        print(json.dumps({"status": "error", "error": "NO_RUNTIME",
                          "message": f"no active runtime for review key {review_key!r} "
                                     "(use 'oracle revive' first)"}, indent=2))
        return 1

    # A3: lifecycle dispatch — cold → fail fast; binding pending → hint but
    # keep waiting (sid may bind shortly); hot/warm → wait for new output.
    health = info.get("runtime_health", {})
    if info.get("status") != "active" or not health.get("alive"):
        print(json.dumps({"status": "error", "error": "NOT_ACTIVE",
                          "message": "runtime not alive — use 'oracle revive' first"},
                         indent=2))
        return 1
    if not info.get("backend_session_id", ""):
        print(json.dumps({"status": "binding_pending", "method": "wait",
                          "detail": "runtime alive but backend session binding pending — "
                                    "will wait for new output regardless"}, indent=2))
        # non-fatal: keep waiting (binding may complete; ASSISTANT_PROGRESS still fires)

    # A15: 委托给共享的 _wait_for_new_output 辅助函数
    auto_recover = bool(getattr(args, "auto_recover", False))
    return _wait_for_new_output(review_key, runtime_id, session_id,
                                timeout=timeout, interval=interval,
                                max_bytes=max_bytes, info=info,
                                auto_recover=auto_recover,
                                session_dir=session_dir)


# ── release ────────────────────────────────────────────────────────────


def _tmux_kill_oracle_runtime(review_key: str) -> tuple[bool, list[str]]:
    """I1: gateway 不可达时的泄漏防御——尽力终止 review 的 tmux runtime。

    1. PID 级：扫描 ``$XDG_STATE_HOME/aimeshchat/runtime/*/spec.json``，
       对 review_key 匹配的 runtime 按其 pid 文件 SIGTERM→SIGKILL。
       （supervisor 以 start_new_session 启动 omp 子进程，tmux kill-pane
       只杀 supervisor 本身——PID 级清理才能保证 OMP 进程真正退出。）
    2. pane 级：私有 tmux server（aimeshchat-gateway）上按
       ``ora-<safe>-`` 窗口名前缀 kill-pane（supervisor pane）。

    返回 (是否终止了任何目标, 目标列表)。全部尽力而为，不抛异常。
    """
    from codeagent.launchers.tmux import TMUX_SESSION_NAME, kill_pane, tmux_cmd

    targets: list[str] = []

    # 1) PID 级：runtime dir spec.json 匹配 review_key。
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    runtime_root = base / "aimeshchat" / "runtime"
    if runtime_root.is_dir():
        for spec_path in runtime_root.glob("*/spec.json"):
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if spec.get("review_key") != review_key:
                continue
            rid = spec.get("runtime_id", "")
            pid_file = spec_path.parent / f"{rid}.pid"
            try:
                pid = int(pid_file.read_text().strip())
            except (OSError, ValueError):
                pid = None
            if pid is None:
                continue
            try:
                os.kill(pid, 15)  # SIGTERM
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except (ProcessLookupError, OSError):
                        break
                    time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                    os.kill(pid, 9)  # SIGKILL
                except (ProcessLookupError, OSError):
                    pass
                targets.append(f"pid:{pid}")
            except (ProcessLookupError, OSError) as exc:
                log.debug("oracle release: pid kill failed (%s): %s", pid, exc)

    # 2) pane 级：窗口名前缀匹配。
    #    P2-D: 主前缀用 _review_sid（sha256[:16] 确定性，与 P0-A 对齐），
    #    保留旧前缀（replace(':','-')[-12:]）匹配 pre-P0-A 残留 pane。
    new_prefix = _review_sid(review_key)  # "postmesh-{sha256[:16]}"
    old_safe = review_key.replace(":", "-")[-12:]
    old_prefix = f"ora-{old_safe}-"  # backward compat for pre-rename panes
    try:
        proc = subprocess.run(
            tmux_cmd("list-windows", "-t", TMUX_SESSION_NAME, "-F", "#{window_name}|#{pane_id}"),
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                name, sep, pane = line.partition("|")
                if not sep:
                    continue
                # 新前缀精确匹配或旧前缀前缀匹配（兼容遗留 pane）
                if (name == new_prefix or name.startswith(new_prefix)
                        or name.startswith(old_prefix)):
                    if kill_pane(pane):
                        targets.append(f"pane:{pane}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("oracle release: tmux pane scan failed: %s", exc)

    return bool(targets), targets


# ── OpenCode 会话作用域清理（release 时，oracle-lite 设计）──────────────
# opencode.db 全局无界增长根因：event/event_sequence 按 aggregate_id 聚合，
# 不在 session 级联下——删 session 行清不掉 event。FK 链独立：
#   event.aggregate_id → event_sequence.aggregate_id（ON DELETE CASCADE），
#   但 event_sequence 无外键指向 session。
# 本函数在 release 时按 session_id 作用域清理，不修全局。

# opencode.db 默认路径（~/.local/share/opencode/opencode.db）。
_OPENCODE_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

# OpenCode session ID 前缀（"ses_" 开头，如 ses_011149b70ffevYGk4czfPUSxTz）。
_OPENCODE_SID_PREFIX = "ses_"

# OpenCode 新 turn/generation 起点的 event.type 标记（P2-1 运行时精简用）。
# OpenCode 在每次开始新一轮生成时发出 session.next.agent.switched.1 /
# session.next.model.switched.1（语义等同 gateway 的 TURN_STARTED）。
# 同一 turn 起点可同时发出两者（seq 相差 1-2），故按 seq 间隙去重，
# 避免把同一起点算成两个 turn。
_TURN_START_EVENT_TYPES = frozenset({
    "session.next.agent.switched.1",
    "session.next.model.switched.1",
})
# 相邻 turn-start marker 的 seq 间隙阈值：<= 此值视为同一 turn 起点。
_TURN_MARKER_DEDUP_GAP = 3


def _is_opencode_session_id(sid: str) -> bool:
    """判断 backend_session_id 是否为 OpenCode 会话格式（"ses_" 前缀）。"""
    return sid.startswith(_OPENCODE_SID_PREFIX)


def _cleanup_opencode_session_on_release(review_key: str, manifest,
                                         *, strip_only: bool) -> dict:
    """release 时按需清理该 oracle 的 OpenCode 会话（仅 opencode backend）。

    解析 backend_session_id：manifest → meta.json → gateway runtime.info。
    仅当 sid 命中 OpenCode 会话格式（"ses_" 前缀）才清理；否则返回
    {"skipped": reason} 幂等跳过（OMP backend 走现有 strip 路径）。
    """
    sid = getattr(manifest, "backend_session_id", "") or ""
    if not sid:
        meta = _read_oracle_meta(review_key)
        sid = (meta or {}).get("backend_session_id", "") or ""
    if not sid:
        try:
            info = _gateway().call("runtime.info", {"review_key": review_key})
            sid = info.get("backend_session_id", "") or ""
        except Exception as exc:
            log.debug("oracle release: backend session resolve failed: %s", exc)
    if not sid:
        return {"skipped": "no backend_session_id"}
    if not _is_opencode_session_id(sid):
        return {"skipped": f"not an opencode session ({sid!r})"}

    cleanup = _purge_opencode_session(sid, strip_only=strip_only)
    cleanup["backend_session_id"] = sid
    cleanup["strip_only"] = strip_only
    return cleanup


def _purge_opencode_session(backend_session_id: str, *,
                            strip_only: bool = False) -> dict:
    """清理 OpenCode 会话的 opencode.db 数据（作用域级，非全局）。

    两种模式：
    - strip_only=False（hard purge / 默认）：删 session 行（级联 message/part/
      session_message/session_input/session_context_epoch/todo/session_share）
      + 删 event_sequence 行（级联 event）。完整清理。
    - strip_only=True（soft release）：仅删 event_sequence（级联 event）——
      清理 event 膨胀数据，保留 session/message 可 revive。

    安全措施：
    - 写前备份 opencode.db（.bak，覆盖旧备份）。
    - 单事务执行，失败回滚。
    - 幂等：session 不存在跳过（不报错）。

    返回 {"deleted_session": bool, "deleted_events": bool,
           "backup": str, "error": str | None}。
    """
    result: dict = {
        "deleted_session": False,
        "deleted_events": False,
        "backup": "",
        "error": None,
    }
    db_path = _OPENCODE_DB_PATH
    if not db_path.exists():
        result["error"] = "opencode.db not found"
        return result
    if not backend_session_id:
        result["error"] = "empty backend_session_id"
        return result

    # 写前备份（覆盖旧 .bak，与现有 _merge_flat_yaml 备份惯例一致）。
    bak_path = db_path.with_suffix(".db.bak")
    # 先 PASSIVE checkpoint 把 WAL 折叠进主库，保证备份包含未 checkpoint 数据。
    # PASSIVE 不阻塞（opencode 若在跑也能并发生效），失败不影响后续清理。
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.close()
    except sqlite3.Error:
        pass
    try:
        shutil.copy2(str(db_path), str(bak_path))
        result["backup"] = str(bak_path)
    except OSError as exc:
        log.warning("opencode session cleanup: backup failed: %s", exc)
        # 备份失败不阻塞清理——数据已确认要删，备份只是防御。
        result["backup"] = f"failed: {exc}"

    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        # 启用 FK 级联（opencode.db 默认 PRAGMA foreign_keys=0）。
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            sid = backend_session_id

            # ① 删 event_sequence（级联删 event）—— 两种模式都做。
            #    event_sequence.aggregate_id = session_id（独立 FK 链）。
            cur = conn.execute(
                "DELETE FROM event_sequence WHERE aggregate_id = ?", (sid,)
            )
            result["deleted_events"] = cur.rowcount > 0

            if not strip_only:
                # ② hard purge：删 session 行（级联 message/part/session_message/
                #    session_input/session_context_epoch/todo/session_share）。
                cur = conn.execute("DELETE FROM session WHERE id = ?", (sid,))
                result["deleted_session"] = cur.rowcount > 0

            conn.commit()
            log.info(
                "opencode session cleanup: sid=%s strip_only=%s "
                "deleted_session=%s deleted_events=%s",
                sid, strip_only,
                result["deleted_session"], result["deleted_events"],
            )
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = f"sqlite error: {exc}"
        log.error("opencode session cleanup failed: sid=%s: %s",
                  backend_session_id, exc)
    except Exception as exc:
        result["error"] = str(exc)
        log.error("opencode session cleanup failed: sid=%s: %s",
                  backend_session_id, exc)

    # WAL checkpoint：释放 freelist 页。
    if result["error"] is None:
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except sqlite3.Error:
            # checkpoint 失败不影响清理结果——下次 SQLite 自然 checkpoint。
            pass

    # P1-2: 大库 VACUUM——仅当 db 文件 > 100MB 时物理收缩。
    # WAL checkpoint 只释放 freelist 页回 OS，不缩小文件；
    # VACUUM 重建整个数据库文件，是唯一物理收缩手段。
    # 必须用独立连接（VACUUM 不能在事务内执行）。
    if result["error"] is None:
        try:
            db_size = db_path.stat().st_size
            if db_size > 100 * 1024 * 1024:  # > 100MB
                vacuum_conn = sqlite3.connect(str(db_path), timeout=60)
                try:
                    vacuum_conn.execute("VACUUM")
                    new_size = db_path.stat().st_size
                    result["vacuum"] = True
                    result["vacuum_before_mb"] = round(db_size / (1024 * 1024), 1)
                    result["vacuum_after_mb"] = round(new_size / (1024 * 1024), 1)
                    log.info(
                        "opencode session cleanup: VACUUM %s %.1fMB → %.1fMB",
                        backend_session_id,
                        db_size / (1024 * 1024),
                        new_size / (1024 * 1024),
                    )
                finally:
                    vacuum_conn.close()
        except (sqlite3.Error, OSError) as exc:
            result["vacuum"] = False
            result["vacuum_error"] = str(exc)
            log.warning("opencode session cleanup: VACUUM failed: %s", exc)

    return result


def _is_turn_completed(events: list, idx: int) -> bool:
    """True when ``events[idx]`` 之后已有新 turn-start marker（turn 已完成）。

    *events* 是按 seq 升序排列的该会话 event 行（每项含 ``type``）。一个 turn
    只有在其后出现了新的 turn-start marker（``session.next.*.switched.1``，
    语义等同 gateway 的 TURN_STARTED）才算完成——即该 turn 已结束、新一轮开始。
    最后一个 in-flight turn 之后没有 marker，本函数对它恒返回 False，
    从而保证 in-flight 数据永不误删。
    """
    if idx < 0 or idx >= len(events):
        return False
    return any(ev.get("type") in _TURN_START_EVENT_TYPES
               for ev in events[idx + 1:])


def _strip_running_session(backend_session_id: str, keep_recent: int = 2) -> dict:
    """P2-1: 运行时精简 OpenCode 会话——删除已完成 turn 的旧 event 数据。

    release 时的 ``_purge_opencode_session`` 一次性清空该会话全部 event；
    本函数在 oracle 运行期间被调用（每完成一个 turn 触发一次），只裁剪已
    确认完成的旧 turn，保留最近 ``keep_recent`` 个 turn（含当前 in-flight
    turn），从而阻止 opencode.db 的 ``event`` 表随长会话无界增长。

    与 release 清理的差异：
    - 只删 ``event`` 表旧行，绝不触碰 ``event_sequence``（单行 seq 计数器）
      以及 session/message/part（warm revive 依赖的转录数据）。
    - 安全：仅删除"已确认完成"的 turn（其后已有新 turn-start marker）；
      in-flight turn 永不删除。

    注：opencode.db 的 event_sequence 每个会话仅一行（aggregate_id 为主键，
    记录 seq 计数器），逐 turn 数据实际存放在 ``event`` 表。因此"裁剪旧
    turn"落实为按 seq 删除 ``event`` 旧行，而非删除 event_sequence 行。

    返回 {"trimmed": 删除的 event 行数, "kept": 保留的 event 行数,
          "error": str | None}。
    """
    result: dict = {"trimmed": 0, "kept": 0, "error": None}
    keep_recent = max(1, keep_recent)
    db_path = _OPENCODE_DB_PATH
    if not db_path.exists():
        result["error"] = "opencode.db not found"
        return result
    if not backend_session_id:
        result["error"] = "empty backend_session_id"
        return result

    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        # 启用 FK（opencode.db 默认 PRAGMA foreign_keys=0）；本函数直接删
        # event 行，不依赖级联，但保持与 _purge_opencode_session 一致。
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            rows = conn.execute(
                "SELECT seq, type FROM event "
                "WHERE aggregate_id = ? ORDER BY seq ASC",
                (backend_session_id,),
            ).fetchall()
            if not rows:
                # 无 event 可裁（全新会话或已清空）——幂等返回。
                return result

            events = [{"seq": s, "type": t} for s, t in rows]

            # turn 起点索引：首条事件（session.created）是 turn1 起点；每个
            # turn-start marker 是新 turn 起点。相邻 marker（seq 间隙 <=
            # _TURN_MARKER_DEDUP_GAP）属于同一起点，去重。
            turn_starts = [0]
            last_marker_seq = events[0]["seq"]
            for i in range(1, len(events)):
                ev = events[i]
                if ev["type"] in _TURN_START_EVENT_TYPES:
                    if ev["seq"] - last_marker_seq > _TURN_MARKER_DEDUP_GAP:
                        turn_starts.append(i)
                    last_marker_seq = ev["seq"]

            num_turns = len(turn_starts)
            if num_turns <= keep_recent:
                # turn 数未超过保留上限——全部保留（含 in-flight）。
                result["kept"] = len(events)
                return result

            # 保留最近 keep_recent 个 turn 的起点索引。
            keep_from = turn_starts[num_turns - keep_recent]

            # 安全门：keep_from 之前的 turn 必须全部"已完成"（其后已有新
            # turn-start marker）；否则放弃裁剪，避免误删 in-flight 数据。
            to_trim = turn_starts[:num_turns - keep_recent]
            if not all(_is_turn_completed(events, i) for i in to_trim):
                result["kept"] = len(events)
                return result

            cutoff_seq = events[keep_from]["seq"]
            cur = conn.execute(
                "DELETE FROM event WHERE aggregate_id = ? AND seq < ?",
                (backend_session_id, cutoff_seq),
            )
            conn.commit()
            result["trimmed"] = cur.rowcount
            result["kept"] = len(events) - cur.rowcount
            log.info(
                "opencode running session strip: sid=%s trimmed=%d kept=%d "
                "keep_recent=%d",
                backend_session_id, result["trimmed"], result["kept"],
                keep_recent,
            )
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = f"sqlite error: {exc}"
        log.debug("opencode running session strip failed: sid=%s: %s",
                  backend_session_id, exc)
    except Exception as exc:
        result["error"] = str(exc)
        log.debug("opencode running session strip failed: sid=%s: %s",
                  backend_session_id, exc)

    return result


def _lazy_db_cleanup(threshold_mb: int = 100,
                    hard_limit_seconds: int = 30 * 24 * 3600) -> int:
    """P1-2: opencode.db 惰性清理——db > threshold 时清理已释放会话。

    扫描 ParkRegistry 中 lifecycle=RELEASED_SOFT 且 backend_session_id
    为 OpenCode 格式（"ses_" 前缀）的条目：
    - 释放时间 < hard_limit_seconds（30 天）→ strip_only=True（保留会话
      数据以支持 warm revive，只精简转录文本）。
    - 释放时间 >= hard_limit_seconds → strip_only=False（hard purge，
      物理删除 session + event 数据）。
    RELEASED_HARD 行已被 delete() 真销毁，不在扫描范围内。

    仅当 opencode.db 存在且超过阈值时触发（避免无谓 IO）。
    返回已清理的会话数。
    """
    db_path = _OPENCODE_DB_PATH
    if not db_path.exists():
        return 0
    try:
        db_size_mb = db_path.stat().st_size / (1024 * 1024)
    except OSError:
        return 0
    if db_size_mb < threshold_mb:
        return 0

    cleaned = 0
    now = time.time()
    try:
        registry = ParkRegistry()
        # 直接查 park_leases 表中 released_soft 行——list_active 仅返回 HOT_PARKED。
        with registry._connect() as conn:
            rows = conn.execute(
                "SELECT manifest_json, released_at FROM park_leases "
                "WHERE lifecycle = 'released_soft'",
            ).fetchall()
        for row in rows:
            try:
                d = json.loads(row[0])
                sid = d.get("backend_session_id", "")
                if sid and _is_opencode_session_id(sid):
                    released_at = row[1] if len(row) > 1 and row[1] else 0
                    old_enough = (now - released_at) >= hard_limit_seconds if released_at else False
                    _purge_opencode_session(sid, strip_only=not old_enough)
                    cleaned += 1
            except Exception as exc:
                log.debug("lazy_db_cleanup: skip entry: %s", exc)
    except Exception as exc:
        log.debug("lazy_db_cleanup: registry scan failed: %s", exc)

    if cleaned > 0:
        try:
            final_mb = round(db_path.stat().st_size / (1024 * 1024), 1)
        except OSError:
            final_mb = 0
        log.info(
            "lazy_db_cleanup: cleaned %d sessions, db_size_mb=%.1f, final_mb=%.1f",
            cleaned, db_size_mb, final_mb,
        )
    return cleaned


# ── P2-2: advisor tiered retention（digest 留存）───────────────────────

# P2-2: advisor JSONL 中 toolResult 需含此最小字符数才算"证据"。
_ADVISOR_EVIDENCE_MIN_CHARS = 200
# P2-2: digest JSON 最大字节数（UTF-8 安全截断）。
_DIGEST_MAX_BYTES = 2048


def _find_advisor_session_file(backend_session_id: str,
                               session_dir: str = "") -> Optional[Path]:
    """P2-2: locate the __advisor JSONL file for a backend session id.

    Scans the OMP sessions tree for files matching
    ``*__advisor*_{backend_session_id}.jsonl`` or any ``__advisor*.jsonl``
    in the same directory as the main session file.
    Returns the most recent matching file, or None.

    When *session_dir* is non-empty, ONLY that directory is searched — this
    prevents stale matches from old sessions in the default root.
    """
    if not backend_session_id:
        return None
    if session_dir:
        search_root = Path(session_dir)
        if not search_root.is_dir():
            return None
    else:
        search_root = Path.home() / ".omp" / "agent" / "sessions"
        if not search_root.is_dir():
            return None
    # Strategy: find the main session directory first, then look for
    # __advisor files in the same directory.
    main = _find_session_file(backend_session_id, session_dir=session_dir)
    if main is not None:
        parent = main.parent
        advisor_files = sorted(
            parent.glob("*__advisor*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if advisor_files:
            return advisor_files[0]
    # Fallback: recursive scan for __advisor files mentioning the sid.
    for dirpath, _dirs, files in os.walk(search_root):
        for name in files:
            if "__advisor" in name.lower() and backend_session_id in name:
                return Path(dirpath) / name
    return None


def _extract_advisor_digest(
    session_path: Path,
    *,
    max_bytes: int = _DIGEST_MAX_BYTES,
) -> dict:
    """P2-2: extract a structured digest from an advisor session JSONL.

    Reads the session transcript, extracts the last assistant message
    (conclusion) and counts tool results that contain substantial
    analysis/review content (evidence). Returns a compact dict:

        {
            "conclusion": str,       # last assistant message (≤2KB)
            "evidence_count": int,   # tool results with ≥200 chars
            "token_estimate": int,   # rough char/4 estimate
            "source": str,           # source file path
            "extracted_at": str,     # ISO timestamp
        }

    Truncates ``conclusion`` to ``max_bytes`` (UTF-8, line-boundary).
    """
    conclusion = ""
    evidence_count = 0
    total_chars = 0

    try:
        with open(session_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = obj.get("message", {})
                role = msg.get("role", "")

                if role == "assistant":
                    content = msg.get("content", [])
                    text = "".join(
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                    if text.strip():
                        conclusion = text
                        total_chars += len(text)

                elif role == "toolResult" or obj.get("type") == "tool_result":
                    # Count tool results with substantial analysis content.
                    content = msg.get("content", [])
                    if not content:
                        # Some formats store text directly in obj["text"]
                        text = str(obj.get("text", ""))
                    else:
                        text = "".join(
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
                    if len(text) >= _ADVISOR_EVIDENCE_MIN_CHARS:
                        evidence_count += 1
                        total_chars += len(text)
    except OSError as exc:
        log.debug("_extract_advisor_digest: read failed: %s", exc)
        return {
            "conclusion": "",
            "evidence_count": 0,
            "token_estimate": 0,
            "source": str(session_path),
            "error": str(exc),
        }

    # UTF-8 safe truncation to max_bytes.
    if max_bytes > 0 and len(conclusion.encode("utf-8")) > max_bytes:
        truncated = conclusion.encode("utf-8")[:max_bytes].decode(
            "utf-8", errors="ignore",
        )
        # Prefer line boundary.
        nl = truncated.rfind("\n", int(len(truncated) * 0.7))
        if nl > 0:
            truncated = truncated[:nl]
        conclusion = truncated

    return {
        "conclusion": conclusion,
        "evidence_count": evidence_count,
        "token_estimate": total_chars // 4,
        "source": str(session_path),
        "extracted_at": datetime.now(timezone.utc).strftime(ISO_TIMESTAMP_FORMAT),
    }


def _oracle_digest_path(review_key: str) -> Path:
    """P2-2: .digest.json path alongside meta.json (~/.omp/oracle/<safe-key>/)."""
    safe = review_key.replace(":", "-").replace("/", "-").replace("\\", "-")
    return Path.home() / ".omp" / "oracle" / safe / "digest.json"


def _save_advisor_digest(review_key: str, manifest) -> Optional[dict]:
    """P2-2: extract and persist advisor digest from the session JSONL.

    Locates the advisor session file for the manifest's backend session,
    extracts key conclusions, and saves to ``.digest.json`` alongside
    ``meta.json``. Returns the digest dict if successful, None otherwise.
    """
    if manifest is None:
        return None
    sid = manifest.backend_session_id or ""
    if not sid:
        return None
    sd = _get_session_dir(manifest)
    advisor_path = _find_advisor_session_file(sid, session_dir=sd)
    if advisor_path is None:
        log.debug("_save_advisor_digest: no advisor session for sid=%s", sid)
        return None

    digest = _extract_advisor_digest(advisor_path)
    digest_path = _oracle_digest_path(review_key)
    try:
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(
            json.dumps(digest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(
            "advisor digest saved: review_key=%s conclusion_len=%d evidence=%d",
            review_key, len(digest.get("conclusion", "")),
            digest.get("evidence_count", 0),
        )
    except OSError as exc:
        log.warning("_save_advisor_digest: write failed: %s", exc)
        return None
    return digest


def _load_advisor_digest(review_key: str) -> Optional[dict]:
    """P2-2: load a previously saved advisor digest (returns None if absent)."""
    path = _oracle_digest_path(review_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cmd_oracle_release(args: argparse.Namespace) -> int:
    """Write terminal state, stop the runtime, release the park lease.

    P1-1: 默认 soft release —— lifecycle=RELEASED_SOFT，保留 OMP session
    文件，`oracle revive` 可 warm 复活；--purge 硬销毁 —— 删 OMP session
    文件 + 删 park 行（registry.release(mode="hard") → registry.delete）。
    """
    review_key = args.review_key
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    # B2: release guard — refuse to silently drop unread REPORTs in the
    # oracle inbox.  Advisory + interactive confirmation; --force skips.
    if manifest:
        try:
            store = MailboxStore()
            inbox_dir = store.agent_subdir(manifest.swarm_session_id, _ORACLE_AGENT, "inbox")
            unread_reports = 0
            for f in store.list_messages(inbox_dir):
                try:
                    msg = json.loads(f.read_bytes())
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    continue
                if msg.get("kind") == "REPORT":
                    unread_reports += 1
            if unread_reports > 0 and not getattr(args, "force", False):
                answer = input(f"有 {unread_reports} 条未读 REPORT，确认释放？(y/N，或 use --force to skip) ").strip().lower()
                if answer not in ("y", "yes"):
                    print(json.dumps({
                        "review_key": review_key,
                        "release_aborted": True,
                        "unread_reports": unread_reports,
                    }, indent=2))
                    return 1
        except Exception:
            # B2: mailbox unreachable/unreadable — the guard is advisory,
            # never a hard gate on release.
            pass

    # P2-3: snapshot before release — captures final state before lifecycle
    # change so a subsequent cold revive is not stale.
    if manifest:
        try:
            save_snapshot(ReviewSnapshot(
                review_key=review_key,
                round=manifest.round,
                last_question=getattr(args, "prompt", "") or "",
                generated_at=time.time(),
            ))
        except Exception as exc:
            log.debug("oracle release: snapshot save failed (%s)", exc)

    # Stop the runtime (gateway) — the only way to terminate it.
    # I1: gateway 不可达时不再只打 warning 继续释放——尝试 tmux/PID 兜底
    # 终止（泄漏防御），并在输出 JSON 中如实标记 runtime_leaked。
    stopped = False
    runtime_leaked = False
    gateway_unreachable = False
    if manifest:
        try:
            info = _gateway().call("runtime.info", {"review_key": review_key})
            rid = info.get("runtime_id")
            if rid:
                _gateway().call("runtime.stop", {"runtime_id": rid, "reason": "oracle release"})
                # 立即清理该 review_key 的所有 stopped 旧记录（内存 + ControlStore），
                # 避免多次 release/revive 累积多个 runtime 记录。
                _gateway().call("runtime.purge_stopped", {"review_key": review_key})
                stopped = True
        except GatewayError as exc:
            # NOT_FOUND = gateway 正常但 runtime 本就不存在（无泄漏）；
            # GATEWAY_DOWN/CONNECT_FAILED = gateway 不可达（可能泄漏）。
            if exc.code in ("GATEWAY_DOWN", "GATEWAY_CONNECT_FAILED"):
                gateway_unreachable = True
            print(f"warning: runtime stop failed: {exc.code}: {exc.message}", file=sys.stderr)
        except Exception as exc:
            gateway_unreachable = True
            print(f"warning: runtime stop failed: {exc}", file=sys.stderr)
        if not stopped and gateway_unreachable:
            # I1: 泄漏防御——PID 级终止 + tmux kill-pane。
            killed, targets = _tmux_kill_oracle_runtime(review_key)
            if killed:
                stopped = True
                print(
                    f"warning: gateway unreachable — terminated leaked runtime "
                    f"({', '.join(targets)})",
                    file=sys.stderr,
                )
            else:
                runtime_leaked = True
                print(
                    "warning: gateway unreachable and no leaked runtime found to "
                    "terminate — the tmux OMP process may still be running",
                    file=sys.stderr,
                )

    # Release the park lease.
    purge = bool(getattr(args, "purge", False))
    keep_advisor = bool(getattr(args, "keep_advisor", False))
    release_mode = "soft"
    strip_report: dict = {"removed": []}
    opencode_cleanup: dict = {}
    advisor_digest: Optional[dict] = None

    # P2-2: extract advisor digest BEFORE any deletion — captures the
    # advisor's conclusions as a compact summary that survives release.
    if manifest:
        advisor_digest = _save_advisor_digest(review_key, manifest)

    if manifest:
        if purge:
            # P1-1: 硬销毁——删 OMP session 文件 + 删 park 行。
            # P2-2: --keep-advisor skips advisor session files from purge
            # so the full __advisor.jsonl survives for debugging.
            _purge_omp_session(manifest, skip_advisor=keep_advisor)
            # MFT: OpenCode backend —— 作用域清理 opencode.db 会话数据
            # （删 session 行 + event/event_sequence，按 aggregate_id 独立删）。
            opencode_cleanup = _cleanup_opencode_session_on_release(
                review_key, manifest, strip_only=False,
            )
            registry.release(review_key, mode="hard")
            release_mode = "hard"
        else:
            # P1-1: 软释放（默认）——RELEASED_SOFT，session 文件保留可 revive。
            # B: 释放前 strip bash/eval 全量 artifact + __advisor 转录（保主 jsonl）。
            # P2-2: --keep-advisor skips transcript stripping entirely so the
            # full __advisor.jsonl survives alongside the digest for debugging.
            if not keep_advisor:
                strip_report = _strip_oracle_transcript(manifest)
            # MFT: OpenCode backend —— soft release 仅清理 event 膨胀数据
            # （保留 session/message 可 warm revive）。
            opencode_cleanup = _cleanup_opencode_session_on_release(
                review_key, manifest, strip_only=True,
            )
            registry.release(review_key, mode="soft")
            release_mode = "soft"

    print(json.dumps({
        "review_key": review_key,
        "runtime_stopped": stopped,
        "runtime_leaked": runtime_leaked,  # I1
        "park_released": manifest is not None,
        "release_mode": release_mode,
        "session_purged": purge and manifest is not None,
        "transcript_stripped": strip_report,  # B: 事后精简结果
        "opencode_cleanup": opencode_cleanup,  # MFT: 会话作用域清理结果
        "advisor_digest": advisor_digest,  # P2-2: tiered retention digest
        "keep_advisor": keep_advisor,  # P2-2: advisor files preserved
    }, indent=2))
    return 0


# ── doctor（P2-4：三源一致性检查）─────────────────────────────────────


def _opencode_session_exists(backend_session_id: str) -> bool:
    """Check if a session id exists in opencode.db (session table)."""
    db_path = _OPENCODE_DB_PATH
    if not db_path.exists():
        return False
    if not backend_session_id:
        return False
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            row = conn.execute(
                "SELECT 1 FROM session WHERE id = ?", (backend_session_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _doctor_list_all_park_entries() -> list[dict]:
    """List ALL park entries (any lifecycle) for cross-source validation.

    Returns list of dicts with keys: review_key, lifecycle, backend_session_id.
    """
    entries: list[dict] = []
    try:
        registry = ParkRegistry()
        with registry._connect() as conn:
            rows = conn.execute(
                "SELECT manifest_json FROM park_leases",
            ).fetchall()
        for (mj,) in rows:
            try:
                d = json.loads(mj)
                entries.append({
                    "review_key": d.get("review_key", ""),
                    "lifecycle": d.get("lifecycle", ""),
                    "backend_session_id": d.get("backend_session_id", ""),
                })
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception as exc:
        log.debug("doctor: park scan failed: %s", exc)
    return entries


def cmd_oracle_doctor(args: argparse.Namespace) -> int:
    """P2-4: three-source consistency check.

    Sources:
    1. Gateway runtime.info — runtime presence/status
    2. Park registry lifecycle — park lease state
    3. opencode.db session existence — backend session liveness

    Issue types:
    - STALE_RUNTIME: gateway says active but park lifecycle is terminal
    - ORPHAN_DB: opencode.db session exists but no park entry references it
    - ORPHAN_TMUX: tmux pane exists but no park entry matches
    - MISSING_DB: park entry has backend_session_id but session not in opencode.db

    Returns 0 if healthy, 1 if issues found, 2 if fix failed.
    """
    do_fix = bool(getattr(args, "fix", False))
    issues: list[dict] = []
    fix_failed = False

    # 1. Gateway connectivity probe.
    gw_ok = False
    try:
        _gateway().call("capabilities.get")
        gw_ok = True
    except Exception as exc:
        issues.append({
            "type": "GATEWAY_UNREACHABLE",
            "review_key": "",
            "detail": str(exc),
            "fix_action": "start gateway: aimeshchat gateway start",
        })

    # 2. Scan ALL park entries for cross-source validation.
    all_entries = _doctor_list_all_park_entries()
    active_entries = ParkRegistry().list_active()
    active_keys = {m.review_key for m in active_entries}

    # Collect all backend_session_ids referenced by any park entry.
    all_park_sids: set[str] = set()

    for entry in all_entries:
        review_key = entry["review_key"]
        lifecycle = entry["lifecycle"]
        bsid = entry["backend_session_id"]
        if bsid:
            all_park_sids.add(bsid)

        # Check ①: lifecycle vs gateway runtime.info status (only for active).
        if gw_ok and lifecycle == "hot_parked":
            try:
                info = _gateway().call("runtime.info", {"review_key": review_key})
                rt_status = info.get("status", "")
                if rt_status not in ("active", "not_found", ""):
                    # Gateway reports a non-active runtime for a HOT_PARKED entry.
                    issues.append({
                        "type": "STALE_RUNTIME",
                        "review_key": review_key,
                        "detail": f"gateway status={rt_status!r} but lifecycle={lifecycle}",
                        "fix_action": "runtime.stop",
                    })
            except Exception:
                pass

        # Check ②: backend_session_id existence in opencode.db.
        if bsid and _is_opencode_session_id(bsid):
            exists = _opencode_session_exists(bsid)
            if lifecycle in ("hot_parked", "cold_resumable") and not exists:
                issues.append({
                    "type": "MISSING_DB",
                    "review_key": review_key,
                    "detail": f"backend_session_id={bsid!r} not found in opencode.db",
                    "fix_action": "log warning (can't fix missing data)",
                })

    # Check ③: ORPHAN_DB — sessions in opencode.db with no park reference.
    if _OPENCODE_DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(_OPENCODE_DB_PATH), timeout=5)
            try:
                rows = conn.execute(
                    "SELECT id FROM session WHERE id LIKE ?",
                    (_OPENCODE_SID_PREFIX + "%",),
                ).fetchall()
                for (sid,) in rows:
                    if sid not in all_park_sids:
                        issues.append({
                            "type": "ORPHAN_DB",
                            "review_key": "",
                            "detail": f"session {sid!r} in opencode.db but no park entry",
                            "fix_action": "purge (strip_only=True) if older than 7 days",
                        })
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.debug("doctor: opencode.db scan failed: %s", exc)

    # Check ④: ORPHAN_TMUX — tmux panes matching oracle patterns.
    from codeagent.launchers.tmux import TMUX_SESSION_NAME, tmux_cmd
    try:
        proc = subprocess.run(
            tmux_cmd("list-windows", "-t", TMUX_SESSION_NAME, "-F",
                     "#{window_name}|#{pane_id}"),
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                name, sep, pane = line.partition("|")
                if not sep:
                    continue
                if name.startswith("postmesh-") or name.startswith("ora-"):
                    # Check if any park entry matches this pane name.
                    matched = False
                    for entry in all_entries:
                        if _review_sid(entry["review_key"]) == name:
                            matched = True
                            break
                        old_safe = entry["review_key"].replace(":", "-")[-12:]
                        if name.startswith(f"ora-{old_safe}-"):
                            matched = True
                            break
                    if not matched:
                        issues.append({
                            "type": "ORPHAN_TMUX",
                            "review_key": "",
                            "detail": f"tmux pane {pane} (window={name}) has no park entry",
                            "fix_action": "log warning (refuse auto-kill, too risky)",
                        })
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("doctor: tmux scan failed: %s", exc)

    # 3. Apply conservative fixes.
    fix_results: list[dict] = []
    if do_fix and issues:
        for issue in issues:
            itype = issue["type"]
            if itype == "STALE_RUNTIME":
                try:
                    info = _gateway().call("runtime.info",
                                           {"review_key": issue["review_key"]})
                    rid = info.get("runtime_id")
                    if rid:
                        _gateway().call("runtime.stop",
                                        {"runtime_id": rid,
                                         "reason": "oracle doctor fix"})
                        fix_results.append({
                            "type": itype, "review_key": issue["review_key"],
                            "action": "runtime.stop", "success": True,
                        })
                except Exception as exc:
                    fix_failed = True
                    fix_results.append({
                        "type": itype, "review_key": issue["review_key"],
                        "action": "runtime.stop", "success": False,
                        "error": str(exc),
                    })

            elif itype == "ORPHAN_DB":
                # Extract session id from detail.
                sid = issue["detail"].split("session ")[1].split("'")[0] if "session " in issue["detail"] else ""
                if sid:
                    try:
                        result = _purge_opencode_session(sid, strip_only=True)
                        if result.get("error"):
                            fix_failed = True
                        fix_results.append({
                            "type": itype, "session_id": sid,
                            "action": "purge(strip_only=True)",
                            "success": not result.get("error"),
                            "detail": result,
                        })
                    except Exception as exc:
                        fix_failed = True
                        fix_results.append({
                            "type": itype, "session_id": sid,
                            "action": "purge(strip_only=True)",
                            "success": False, "error": str(exc),
                        })

            elif itype == "ORPHAN_TMUX":
                log.warning("doctor: ORPHAN_TMUX %s — refusing auto-kill (too risky)",
                            issue["detail"])

            elif itype == "MISSING_DB":
                log.warning("doctor: MISSING_DB %s — can't fix missing data",
                            issue["detail"])

    healthy = len([i for i in issues if i["type"] != "GATEWAY_UNREACHABLE"]) == 0
    report = {
        "healthy": len(active_entries) if healthy else len(active_entries) - len(issues),
        "issues": issues,
    }
    if fix_results:
        report["fix_results"] = fix_results
    print(json.dumps(report, indent=2))
    if fix_failed:
        return 2
    return 0 if healthy else 1


# ── revive（P1-2：RELEASED_SOFT / COLD_RESUMABLE → HOT_PARKED）──────────


def _purge_omp_session(manifest: ParkManifest, *,
                       skip_advisor: bool = False) -> list[str]:
    """P1-1: 硬销毁——删除 OMP session 文件 + swarm session 目录（--purge）。

    删除对象（存在才删，返回实际删除路径列表）：
    - manifest.omp_session_path（若已记录）
    - _find_session_file(backend_session_id) 命中的 ``*_{sid}.jsonl``
    - 同目录下 ``*_{sid}`` 命名的会话子目录（``<ts>_<sid>/``，含 __advisor
      等附属文件）
    - I5: swarm session 目录（mailbox session dir + _outbox/_dead_letter
      附属目录）——purge 必须把 mailbox 痕迹一起清掉，否则重新 start 同
      review_key 会撞见陈旧的 inbox/history。

    P2-2: ``skip_advisor=True`` preserves ``__advisor*.jsonl`` files when
    deleting session directories — their conclusions are already captured in
    the digest but the raw files may be needed for debugging.
    """
    removed: list[str] = []
    targets: set[Path] = set()

    raw_path = getattr(manifest, "omp_session_path", "") or ""
    if raw_path:
        p = Path(raw_path)
        if p.exists():
            targets.add(p)

    sid = manifest.backend_session_id or ""
    if sid:
        sd = _get_session_dir(manifest)
        found = _find_session_file(sid, session_dir=sd)
        if found is not None:
            targets.add(found)
            parent = found.parent
            if parent.is_dir():
                for child in parent.glob(f"*_{sid}"):
                    if child.is_dir() and child not in targets:
                        targets.add(child)

    # I5: swarm session 目录 + 其 outbox/dead-letter 附属目录。
    swarm_id = getattr(manifest, "swarm_session_id", "") or ""
    if swarm_id:
        store = MailboxStore()
        for p in (
            store.session_dir(swarm_id),
            store.root / "_outbox" / swarm_id,
            store.root / "_dead_letter" / swarm_id,
        ):
            if p.exists():
                targets.add(p)

    for p in sorted(targets, key=str):
        try:
            if p.is_dir():
                if skip_advisor:
                    # P2-2: selectively delete directory contents, preserving
                    # __advisor files. Walk children, delete non-advisor first,
                    # then rmtree if empty.
                    preserved = False
                    for child in sorted(p.rglob("*"), reverse=True):
                        if child.is_file() and "__advisor" in child.name.lower():
                            preserved = True
                            continue
                        if child.is_file():
                            child.unlink()
                            removed.append(str(child))
                        elif child.is_dir():
                            try:
                                child.rmdir()  # only succeeds if empty
                                removed.append(str(child))
                            except OSError:
                                pass
                    # Remove the directory itself if now empty.
                    if not preserved:
                        try:
                            p.rmdir()
                            removed.append(str(p))
                        except OSError:
                            pass
                else:
                    shutil.rmtree(p)
                    removed.append(str(p))
            else:
                if skip_advisor and "__advisor" in p.name.lower():
                    continue
                p.unlink()
                removed.append(str(p))
        except OSError as exc:
            print(f"warning: purge session file failed: {p}: {exc}", file=sys.stderr)
    return removed


def _strip_oracle_transcript(manifest: ParkManifest) -> dict:
    """B: 事后精简 oracle 会话——委托 ``oracle_transcript_strip`` 独立脚本。

    释放路径：删除 bash-original、truncate bash.log/eval.log/read.* 到 2KB、
    过滤主 jsonl（保留 user/assistant + session identity 头行）、删 __advisor。

    force=True：release 路径调用时 runtime 已停止（cmd_oracle_release 确保）。
    """
    from codeagent.scripts.oracle_transcript_strip import strip_for_manifest

    return strip_for_manifest(manifest, force=True)


def _is_runtime_alive(manifest: "ParkManifest") -> bool:
    """D1: 检查 park manifest 对应的 runtime 是否真正 alive。

    通过 gateway runtime.info 查询，而非仅信任 lifecycle 标记。
    gateway 不可达时保守返回 False（降级到 warm/cold）。
    """
    try:
        info = _gateway().call("runtime.info", {
            "review_key": manifest.review_key,
        })
        health = info.get("runtime_health", {})
        return info.get("status") == "active" and health.get("alive", False)
    except Exception:
        return False


def cmd_oracle_revive(args: argparse.Namespace) -> int:
    """P1-2: 从 RELEASED_SOFT / COLD_RESUMABLE 复活。

    D1: 使用 revive_or_spawn 决策树（含 runtime alive 检查），
    而非内联重复路由逻辑。
    D2: 输出加 declared: true/false（gateway presence 声明结果）。

    mode: bg（默认）/pane 走 RuntimeRegistry.spawn（监督式 runtime）；
    resume 走 ``omp --resume`` 前台附着（绕过 aimeshchat）。
    """
    # P4: refuse to revive when gateway is unreachable (auto-start attempted).
    if not _ensure_gateway_or_hint():
        return 1

    from codeagent.park.router import revive_or_spawn

    review_key = args.review_key
    mode = getattr(args, "mode", "bg") or "bg"
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    if manifest is None:
        print(json.dumps({"error": "not_found", "hint": "use 'oracle start' first"}, indent=2),
              file=sys.stderr)
        return 1
    if manifest.lifecycle == Lifecycle.RELEASED_HARD:
        print(json.dumps({"error": "purged", "hint": "use 'oracle start' to create a new instance"}, indent=2),
              file=sys.stderr)
        return 1
    if manifest.lifecycle == Lifecycle.BROKEN:
        print(json.dumps({"error": "broken", "hint": "use 'oracle release --purge' then 'oracle start'"}, indent=2),
              file=sys.stderr)
        return 1

    # D1: 用 revive_or_spawn 做路由决策（含 runtime alive 检查）。
    decision = revive_or_spawn(review_key, is_alive=_is_runtime_alive)

    if decision.method == "hot":
        # HOT_PARKED 且 runtime alive → 不做 revive，提示用 ask。
        print(json.dumps({
            "error": "already_active",
            "hint": "use 'oracle ask' to deliver a prompt",
            "review_key": review_key,
        }, indent=2), file=sys.stderr)
        return 1

    # D1: method 由 decision 决定（warm/cold），不再手算 bound_sid。
    method = decision.method
    declared: Optional[bool] = None  # D2: gateway presence 声明结果
    try:
        if method == "warm":
            runtime_id, backend_session_id, model_chain = _revive_warm(review_key, manifest, mode)
        else:
            runtime_id, backend_session_id, model_chain = _revive_cold(review_key, manifest, mode)
        declared = True  # D2: revive 完成（resume 模式 gateway 声明也成功）
    except RuntimeError as exc:
        # D2: _attach_omp_session 上浮的 A7 门拒绝等真实错误。
        err_msg = str(exc)
        declared = False
        # resume 模式下 A7 门拒绝是致命错误。
        if mode == "resume":
            print(json.dumps({
                "error": "declare_failed",
                "review_key": review_key,
                "message": err_msg,
                "declared": declared,
            }, indent=2), file=sys.stderr)
            return 1
        # bg/pane 模式下 gateway 声明失败不阻塞 spawn，只警告。
        log.warning("oracle revive: gateway declare failed (%s)", err_msg)
    except Exception as exc:
        declared = False
        print(json.dumps({
            "error": f"{method}_failed",
            "review_key": review_key,
            "message": str(exc),
            "declared": declared,
        }, indent=2), file=sys.stderr)
        return 1

    # A1: refresh the bound-session meta after a successful revive so the
    # next warm attempt reuses the (possibly new) backend session id.
    if backend_session_id:
        _write_oracle_meta(review_key, backend_session_id, "bound",
                           swarm_session_id=manifest.swarm_session_id or "")

    print(json.dumps({
        "review_key": review_key,
        "method": method,
        "mode": mode,
        "lifecycle": Lifecycle.HOT_PARKED.value,
        "runtime_id": runtime_id,
        "backend_session_id": backend_session_id,
        "model_chain": model_chain,
        "declared": declared,  # D2: gateway presence 声明结果
    }, indent=2))

    # P1-2: 顺便惰性清理 opencode.db（revive 成功说明有活跃使用，趁机清理已释放会话）。
    try:
        _lazy_db_cleanup()
    except Exception as exc:
        log.debug("oracle revive: lazy_db_cleanup failed: %s", exc)

    return 0


# ── attach（P2：统一入口——hot send 或 revive）──────────────────────────


def cmd_oracle_attach(args: argparse.Namespace) -> int:
    """P2: Unified attach entry — connect (HOT_PARKED) or revive (released/cold).

    Routes:
    - absent         → error not_found (hint: use start)
    - HOT_PARKED     → delegate to cmd_oracle_ask (hot send to live runtime)
    - RELEASED_SOFT  → delegate to cmd_oracle_revive (warm revive)
    - COLD_RESUMABLE → delegate to cmd_oracle_revive (cold revive)
    - RELEASED_HARD  → cmd_oracle_revive reports purged
    - BROKEN         → cmd_oracle_revive reports broken

    mode: bg（默认）/pane/resume — forwarded to revive when applicable.
    """
    review_key = args.review_key
    registry = ParkRegistry()
    manifest = registry.lookup(review_key)

    # P2: manifest absent → cannot attach
    if manifest is None:
        print(json.dumps({
            "error": "not_found",
            "hint": "use 'oracle start' first",
        }, indent=2), file=sys.stderr)
        return 1

    # P2: already active → hot send via ask
    if manifest.lifecycle == Lifecycle.HOT_PARKED:
        return cmd_oracle_ask(args)

    # P2: released/cold/broken → revive (lifecycle routing inside revive)
    return cmd_oracle_revive(args)


def _revive_warm(review_key: str, manifest: ParkManifest, mode: str) -> tuple[str, str, list[str]]:
    """P1-2: warm 复活——复用 backend_session_id 的原生会话。

    resume → _attach_omp_session 前台附着（无 runtime_id）。
    bg/pane → RuntimeRegistry.spawn（``omp --resume <sid>`` 监督式 tmux
    pane），与 cmd_oracle_ask 的 warm 路径同构。
    """
    sid = manifest.swarm_session_id or _review_sid(review_key)
    # A1: 复用 meta.json 绑定的 backend session（manifest 缺失/被清时仍 warm）。
    bound_sid = _resolve_bound_session_id(review_key, manifest)

    if mode == "resume":
        # D2: 传播 _attach_omp_session 的错误（A7 门拒绝等）。
        declare_err = _attach_omp_session(bound_sid, review_key, manifest.workdir)
        if declare_err:
            raise RuntimeError(declare_err)
        return "", bound_sid, []

    # B2: 从 manifest 读 ExecutionSpec 显式字段（start 时落盘），不再重推导。
    # 旧 manifest 无 primary_model 时回退 _model_chain_from_manifest（迁移兼容）。
    agent_type = manifest.agent_type or _ORACLE_AGENT
    if manifest.primary_model:
        primary_model = manifest.primary_model
        model_chain = [primary_model]
    else:
        # 旧 manifest 迁移兼容：走原有解析链
        model_chain = _model_chain_from_manifest(agent_type, manifest)
        primary_model = model_chain[0] if model_chain else (manifest.model or "")
    chain_env: dict[str, str] = {}
    if model_chain:
        chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(model_chain)

    backend = _resolve_backend(agent_type, "omp")
    reg = RuntimeRegistry()
    handle = reg.spawn(backend, {
        "session_id": sid,
        "agent_id": _ORACLE_AGENT,
        "review_key": review_key,
        "workdir": manifest.workdir or os.getcwd(),
        "task": "",
        "model": primary_model,
        "variant": manifest.variant or "",  # B2: ExecutionSpec variant 透传
        "backend_session_id": bound_sid,
        "gateway_socket": str(control_socket_path()),
        "owner_pid": os.getpid(),
        "nonce": uuid4().hex[:12],
        "short_task": False,
        "env": chain_env,
        "session_dir": manifest.omp_session_dir,  # P-SI: 会话隔离目录
    })
    try:
        # P2-11 同款保护：session id 提取窗口失败时保留原值，避免破坏续接点。
        new_backend_session_id = handle.backend_session_id or bound_sid
        if not handle.backend_session_id:
            print(f"warning: revive warm: backend session id extraction window failed — "
                  f"preserving previous id {bound_sid!r}", file=sys.stderr)
        _flip_to_hot(review_key, manifest, sid=sid, backend_session_id=new_backend_session_id)
        _adopt_runtime(review_key, sid, handle, backend)
    except Exception:
        # P2-12 同款：后续步骤失败时停掉已 spawn 的 runtime，避免泄漏。
        try:
            reg.stop(handle.runtime_id, "revive-warm-failed")
        except Exception:
            pass
        raise
    return handle.runtime_id, new_backend_session_id, model_chain


def _revive_cold(review_key: str, manifest: ParkManifest, mode: str) -> tuple[str, str, list[str]]:
    """P1-2: cold 复活——快照重建新会话（无 backend session 可复用时）。

    resume 模式没有可附着的会话，退化为 bg（监督式 spawn）。
    首轮 task 注入 build_cold_context（与 cmd_oracle_ask cold 分支一致）。
    """
    if mode == "resume":
        mode = "bg"
    # P2-3: snapshot staleness check — warn if the cold revive context is
    # older than the threshold (degraded quality, potentially stale).
    age = _snapshot_age_days(manifest)
    if age < 0:
        log.warning(
            "oracle revive cold %s: no snapshot found — cold revive has "
            "no prior context to reconstruct from", review_key,
        )
    elif age > _SNAPSHOT_STALE_DAYS:
        log.warning(
            "oracle revive cold %s: snapshot is %.1f days old (threshold=%d) "
            "— cold revive quality may be degraded",
            review_key, age, _SNAPSHOT_STALE_DAYS,
        )
    sid = _review_sid(review_key)
    # B2: 从 manifest 读 ExecutionSpec 显式字段（start 时落盘），不再重推导。
    # 旧 manifest 无 primary_model 时回退 _model_chain_from_manifest（迁移兼容）。
    agent_type = manifest.agent_type or _ORACLE_AGENT
    if manifest.primary_model:
        primary_model = manifest.primary_model
        model_chain = [primary_model]
    else:
        # 旧 manifest 迁移兼容：走原有解析链
        model_chain = _model_chain_from_manifest(agent_type, manifest)
        primary_model = model_chain[0] if model_chain else (manifest.model or "")
    chain_env: dict[str, str] = {}
    if model_chain:
        chain_env["OMP_MODEL_FALLBACK_CHAIN"] = ",".join(model_chain)
    backend = _resolve_backend(agent_type, "omp")
    reg = RuntimeRegistry()
    handle = reg.spawn(backend, {
        "session_id": sid,
        "agent_id": _ORACLE_AGENT,
        "review_key": review_key,
        "workdir": manifest.workdir or os.getcwd(),
        "task": build_cold_context(review_key),
        "model": primary_model,
        "variant": manifest.variant or "",  # B2: ExecutionSpec variant 透传
        "gateway_socket": str(control_socket_path()),
        "owner_pid": os.getpid(),
        "nonce": uuid4().hex[:12],
        "short_task": False,
        "env": chain_env,
        "session_dir": manifest.omp_session_dir,  # P-SI: 会话隔离目录
    })
    try:
        _flip_to_hot(review_key, manifest, sid=sid,
                     backend_session_id=handle.backend_session_id or "")
        _adopt_runtime(review_key, sid, handle, backend)
    except Exception:
        try:
            reg.stop(handle.runtime_id, "revive-cold-failed")
        except Exception:
            pass
        raise
    return handle.runtime_id, handle.backend_session_id or "", model_chain


def _flip_to_hot(review_key: str, manifest: ParkManifest, sid: str,
                 backend_session_id: str) -> None:
    """P1-2: revive 成功后把 park manifest 原位翻回 HOT_PARKED。

    用 update()（而非 acquire()）——COLD_RESUMABLE 不是终态，acquire
    会拒绝回迁（仅 RELEASED_SOFT 允许重新 acquire）；update 与
    cmd_oracle_ask 的 warm 路径一致。
    """
    # D3: replace() preserves every untouched field — the old manual copy
    # dropped/needed defensive getattr for omp_session_path.
    ParkRegistry().update(review_key, replace(
        manifest,
        swarm_session_id=sid,
        lifecycle=Lifecycle.HOT_PARKED,
        backend_session_id=backend_session_id,
        round=manifest.round + 1,
        last_activity_at=time.time(),
        release_mode="",
        config_fingerprint=_config_fingerprint(manifest.agent_type or ""),  # P1-4: 刷新指纹
    ))


def _attach_omp_session(session_id: str, review_key: str, workdir: str = "") -> Optional[str]:
    """P1-3: resume 模式——前台 ``omp --resume`` 附着（绕过 aimeshchat）。

    设计稿的 ``omp -s <sid>`` 在真实 omp CLI（v17）中不存在：交互式续接
    旗标是 ``-r/--resume=<sid>``，这里用实际旗标。附带向 gateway 声明
    presence（runtime.declare 是 Phase 3 可选 API，失败静默降级）。

    D2: 返回 None 表示声明成功；返回错误字符串表示失败。
    区分「gateway 不可达」（静默降级，返回 None）和「A7 门拒绝/其他真实
    错误」（返回错误描述，上浮给调用方）。
    """
    try:
        subprocess.Popen(
            ["omp", "--resume", session_id],
            cwd=workdir or None,
        )
    except OSError as exc:
        print(f"warning: omp --resume attach failed: {exc}", file=sys.stderr)
    try:
        _gateway().call("runtime.declare", {  # P3: weak presence declaration
            "review_key": review_key,
            "backend_session_id": session_id,
            "mode": "native_resume",
            "agent_id": _ORACLE_AGENT,
        })
        return None  # D2: declared successfully
    except (ConnectionError, OSError, FileNotFoundError) as exc:
        # D2: gateway 不可达 — 静默降级（非致命）。
        log.debug("oracle revive: gateway presence declare skipped (%s)", exc)
        return None
    except GatewayError as exc:
        # D2: A7 门拒绝或结构化 gateway 错误 — 上浮。
        return f"gateway declare failed: {exc.code}: {exc.message}"
    except Exception as exc:
        # D2: 未预期错误 — 上浮（不再掩盖）。
        return f"gateway declare failed: {exc}"
