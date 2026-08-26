#!/usr/bin/env python3
"""swarm attach — 为手工启动的交互式 omp 实例生成 mailbox 唤醒身份。

背景：omp-mailbox-plugin 启动后轮询环境变量 ``OMP_MAILBOX_IDENTITY_FILE``
指向的身份文件（2s 间隔）；无该 env 则插件完全不激活（"聋哑节点"）。
身份文件正常由 gateway launcher / OMPRunner._extra_env 在拉起 worker 时写入，
但**手工多开的交互式实例**没有任何 launcher —— 本脚本补发身份。

用法::

    python3 scripts/swarm_attach.py <session_id> --agent <agent_id> \
        [--mailbox-root <root>] [--out <export.sh>] [--register]

行为:
  1. 生成最小合法身份 JSON ``{"session_id": ..., "agent_id": ...}`` 写入
     ``~/.omp/mailbox-identity/``（0600 文件 / 0700 目录，原子写）。
     特意不带 ``nonce`` 与 ``owner_pid``：
     - 无 nonce → 插件 nonce 校验条件式放行（无需 gateway handshake）
     - 无 owner_pid → 插件跳过存活校验（attach 进程退出不影响交互实例长期使用）
  2. 输出 ``export`` 行（stdout 或 ``--out <file>``）。在启动 omp **之前** source
     即可获得唤醒能力。
  3. ``--register`` 时尝试把 agent 注册进 swarm session roster（幂等容错：
     session 已存在 / agent 已在 roster 均视为成功）。

安全模型不变：nonce 校验是条件式的，本脚本生成的身份不含 nonce，
与 P2-15 的 gateway ownership-handshake 语义无冲突。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

IDENTITY_DIR = Path.home() / ".omp" / "mailbox-identity"
DEFAULT_MAILBOX_ROOT = Path.home() / ".local/share/aimeshchat/mailbox"


def write_identity(identity_dir: Path, payload: dict) -> Path:
    """原子写入身份文件（tmp → chmod 0600 → rename），目录收紧 0700。"""
    identity_dir.mkdir(parents=True, exist_ok=True)
    try:
        identity_dir.chmod(0o700)
    except OSError:
        pass
    token = f"attach_{int(time.time())}_{secrets.token_hex(4)}"
    path = identity_dir / f"{token}.json"
    tmp = identity_dir / f".tmp-{token}.json"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(str(tmp), str(path))
    os.chmod(path, 0o600)
    return path


def try_register(session_id: str, agent_id: str, mailbox_root: str) -> None:
    """尽力把 agent 注册进 swarm roster；失败仅警告（编排者可能已注册）。"""
    def run(*argv: str) -> None:
        try:
            subprocess.run(
                ["aimeshchat", *argv],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"  ⚠ swarm 注册命令失败（忽略）: {' '.join(argv)}: {exc}", file=sys.stderr)

    # create-session 对已存在 session 报错 → 容错继续
    run("swarm", "create-session", session_id, "--manager", "manager",
        "--members", f"manager,{agent_id}")
    if mailbox_root:
        run("swarm", "create-session", session_id, "--manager", "manager",
            "--members", f"manager,{agent_id}", "--mailbox-root", mailbox_root)
    run("swarm", "register", session_id, "--agent", agent_id, "--host", "__local__",
        "--backend", "omp")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成交互式 omp 实例的 mailbox 唤醒身份")
    parser.add_argument("session_id", help="swarm session id")
    parser.add_argument("--agent", required=True, help="本实例的 agent id（如 w1）")
    parser.add_argument("--mailbox-root", default="",
                        help="mailbox root（默认用插件内置值 ~/.local/share/aimeshchat/mailbox）")
    parser.add_argument("--out", default="", help="将 export 行写入该文件而非 stdout")
    parser.add_argument("--register", action="store_true",
                        help="顺带把 agent 注册进 swarm roster（幂等容错）")
    args = parser.parse_args()

    payload = {"session_id": args.session_id, "agent_id": args.agent}
    identity_path = write_identity(IDENTITY_DIR, payload)
    exports = [
        f"export OMP_MAILBOX_IDENTITY_FILE='{identity_path}'",
        f"export OMP_MAILBOX_SESSION_ID='{args.session_id}'",
        f"export OMP_MAILBOX_AGENT_ID='{args.agent}'",
        "export CODEAGENT_ROLE='worker'",
        f"export SWARM_SESSION_ID='{args.session_id}'",
        f"export OMP_WORKER_ID='{args.agent}'",
    ]
    if args.mailbox_root:
        exports.append(f"export MAILBOX_ROOT='{args.mailbox_root}'")

    text = "\n".join(exports) + "\n"
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"身份文件: {identity_path}")
        print(f"export 行已写入: {out_path}")
        print("启动 omp 前 source 该文件即可获得唤醒能力。")
    else:
        print(f"身份文件: {identity_path}", file=sys.stderr)
        print(text, end="")


if __name__ == "__main__":
    main()
