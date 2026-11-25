#!/usr/bin/env python3
"""
Codex-MCP 的 Claude Code hook：把结束的 Codex 任务结果送回派出它的会话。

用法：python hook.py <UserPromptSubmit|PostToolUse|Stop>
- 三个事件都以 additionalContext 输出；Stop 收到 additionalContext 会让回合继续（Claude Code 2.1.163 起）
- UserPromptSubmit / PostToolUse：送回未送达的结果
- Stop：有未送达结果 -> 附上结果让回合继续；有未 detach 的任务在跑且没有 codex_wait 在等 ->
  提示去 codex_wait（每个任务只提醒一次）。已有 wait 在等的任务不提醒：那是被 Claude Code
  转入后台的调用，返回时会唤醒会话

会话归属：状态文件以 Claude 进程 pid 命名（MCP 服务的父进程），hook 沿自己的
祖先链找第一个存在的文件；不依赖环境变量，多个 Claude 窗口互不干扰。
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import STATE_DIR, ancestors, process_table, server_alive  # noqa: E402

MAX_STOP_REMINDERS = 1  # 每个任务的提醒次数：提醒后 Claude 仍选择结束（例如用户说不用等）就直接结束回合
TERMINAL = ("completed", "interrupted", "failed")
# Claude Code 把超过 10000 字的 additionalContext 存成文件，只给模型路径和 2000 字预览
TEXT_LIMIT = 9500


def pack(header: str, finished, status_tool: str):
    """在上限内放尽量多的完整结果，放不下的留给下一次 hook；单个结果本身超长时截断并指向全文。"""
    text, sent = header, []
    for t in finished:
        body = t["text"]
        room = TEXT_LIMIT - len(text) - 2
        if len(body) > room:
            if sent:
                break
            note = f'\n…(truncated; {status_tool}(ids=["{t["id"]}"], full=true) shows the full result)'
            body = body[:max(0, room - len(note))] + note
        text += "\n\n" + body
        sent.append(t)
    if len(sent) < len(finished):
        text += f"\n\n{len(finished) - len(sent)} more finished task(s) will be delivered by the next hook."
    return text, sent


def read_json(path: Path):
    """读状态或 ack 文件。server 用 os.replace 换文件的瞬间，Windows 上读会报 PermissionError，稍等重试。"""
    for attempt in range(4):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


def find_session():
    table = process_table()
    for pid in ancestors(os.getpid(), table):
        path = STATE_DIR / f"session-{pid}.json"
        if not path.exists():
            continue
        try:
            data = read_json(path)
        except (OSError, ValueError):
            return None, None
        if server_alive(table, data.get("server_pid"), pid):
            return path, data
        return None, None  # 文件残留但 MCP 服务已退出，交给下次 server 启动时清理
    return None, None


def emit(event: str, text: str) -> None:
    obj = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.stdin.buffer.read()  # 输入不需要，但要读完，避免上游写管道阻塞

    path, data = find_session()
    if not path:
        return
    ack_path = path.with_name(path.stem + ".ack.json")
    # 并行工具调用会同时触发多个 hook 进程：拿不到锁就放弃本次，避免同一结果重复送达
    lock_path = path.with_name(path.stem + ".lock")
    try:
        if time.time() - lock_path.stat().st_mtime > 30:
            lock_path.unlink()  # 上一个 hook 超时终止后留下的锁
    except OSError:
        pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:  # 已有 hook 持锁（FileExistsError），或锁文件正被删除（PermissionError）
        return
    os.close(fd)
    try:
        # 拿到锁后重读：find_session 的快照可能已过时，期间工具调用可能已交付结果（reported）
        try:
            data = read_json(path)
        except (OSError, ValueError):
            return
        deliver(event, path, data, ack_path)
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def deliver(event: str, path: Path, data, ack_path: Path) -> None:
    try:
        ack = read_json(ack_path) if ack_path.exists() else {}
    except OSError:
        return
    except ValueError:
        ack = {}  # 原子替换写入，读到坏内容说明文件已损坏：当作空的重建，代价是已送达的结果可能再送一次
    if not isinstance(ack, dict):
        ack = {}
    ack.setdefault("delivered", [])
    ack.setdefault("stop_reminders", {})

    def save_ack() -> None:
        tmp = ack_path.with_name(f"{ack_path.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(ack), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, ack_path)
                return
            except PermissionError:  # server 或杀毒软件短暂占用
                time.sleep(0.05 * (attempt + 1))
        try:
            tmp.unlink()  # 写不进去就不记账：下一次 hook 可能重复送达或重复提醒，不会漏
        except OSError:
            pass

    tasks = (data or {}).get("tasks") or {}
    finished = [t for t in tasks.values()
                if t["status"] in TERMINAL and not t.get("reported") and t["id"] not in ack["delivered"]]
    running = [t for t in tasks.values()
               if t["status"] == "running" and not t.get("detach") and not t.get("waited")]

    if finished:
        text, sent = pack("Codex subagent tasks finished; results below. Show review findings to the user before "
                          "changing anything. A task reporting done is not sign-off; verify critical changes yourself.",
                          finished, "codex_status")
        emit(event, text)
        ack["delivered"].extend(t["id"] for t in sent)
        save_ack()
        return

    if event == "Stop" and running:
        to_remind = [t for t in running if ack["stop_reminders"].get(t["id"], 0) < MAX_STOP_REMINDERS]
        if not to_remind:
            return
        for t in to_remind:
            ack["stop_reminders"][t["id"]] = ack["stop_reminders"].get(t["id"], 0) + 1
        ids = ", ".join(t["id"] for t in to_remind)
        emit(event, (
            f"Codex subagent tasks are still running and no codex_wait is waiting for them: {ids}. "
            f"If you need their results, call codex_wait once (no timeout needed): after about two minutes it "
            f"moves to the background; end your turn then, and the result wakes this session when the tasks finish. "
            f"If the user said not to wait, just end your turn: the tasks keep running and their results arrive "
            f"with the next message. Do not interrupt tasks just to end the turn."))
        save_ack()  # 先输出再记账：输出失败时这次提醒不算用掉


if __name__ == "__main__":
    main()
