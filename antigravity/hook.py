#!/usr/bin/env python3
"""
Codex-MCP 的 Antigravity hook：
- PreInvocation：在模型调用前，将本会话已结束但未送达的 Codex 任务结果附加为 systemMessage
- Stop：正常结束回合（NO_TOOL_CALL）时，有未送达结果则送回并继续；有未 detach 且无等待在途的运行中任务则提醒一次
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ["CODEX_SUB_CLIENT"] = "antigravity"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import STATE_DIR, ancestors, log, parse_host_pid, process_table, server_alive  # noqa: E402

MAX_STOP_REMINDERS = 1  # 提醒一次后仍选择结束（例如用户说不用等）就直接结束回合
TERMINAL = ("completed", "interrupted", "failed")


def emit(obj: Dict[str, Any]) -> None:
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def read_payload() -> Dict[str, Any]:
    raw = sys.stdin.buffer.read()
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _belongs(task: Dict[str, Any], conversation_id: str) -> bool:
    cid = str(task.get("conversation_id") or "")
    return bool(cid and conversation_id and cid == conversation_id)


def _has_actionable(event: str, tasks: Dict[str, Any], ack_path: Path, conversation_id: str) -> bool:
    """快路径预判：无待送达结果且（Stop 下）无待提醒运行中任务时，跳过进程表快照和锁文件。"""
    if not tasks:
        return False
    try:
        ack = json.loads(ack_path.read_text(encoding="utf-8")) if ack_path.exists() else {}
    except (OSError, ValueError):
        return True  # 读不到 ack 时走加锁慢路径，避免漏送达
    delivered = set(ack.get("delivered") or ())
    stop_reminders = ack.get("stop_reminders") or {}
    for t in tasks.values():
        if not isinstance(t, dict) or not _belongs(t, conversation_id):
            continue
        status = t.get("status")
        tid = t.get("id")
        if status in TERMINAL and not t.get("reported") and tid not in delivered:
            return True
        if (event == "Stop" and status == "running"
                and not t.get("detach") and not t.get("waited")
                and stop_reminders.get(tid, 0) < MAX_STOP_REMINDERS):
            return True
    return False


def _read_actionable_sessions(event: str, conversation_id: str) -> List[Tuple[Path, Dict[str, Any]]]:
    if not STATE_DIR.exists():
        return []
    items: List[Tuple[Path, Dict[str, Any]]] = []
    for path in STATE_DIR.glob("session-*.json"):
        if path.name.endswith(".ack.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        tasks = data.get("tasks") or {}
        ack_path = path.with_name(path.stem + ".ack.json")
        if _has_actionable(event, tasks, ack_path, conversation_id):
            items.append((path, data))
    return items


def _pick_session(live: List[Tuple[Path, Dict[str, Any]]],
                  conversation_id: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    if not live:
        return None, None
    if conversation_id:
        for path, data in live:
            if any(t.get("conversation_id") == conversation_id for t in (data.get("tasks") or {}).values()):
                return path, data
    live.sort(key=lambda item: float(item[1].get("updated") or 0), reverse=True)
    return live[0]


def find_session(event: str, conversation_id: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    candidates = _read_actionable_sessions(event, conversation_id)
    if not candidates:
        return None, None

    table = process_table()
    live: List[Tuple[int, Path, Dict[str, Any]]] = []
    for path, data in candidates:
        host_pid = data.get("host_pid") if isinstance(data.get("host_pid"), int) else parse_host_pid(path)
        if host_pid is not None and server_alive(table, data.get("server_pid"), host_pid):
            live.append((host_pid, path, data))
    if not live:
        return None, None

    # 优先匹配同一宿主进程下的存活服务，避免多个宿主实例互相干扰
    ancestor_set = set(ancestors(os.getpid(), table))
    same_host = [(path, data) for host_pid, path, data in live if host_pid in ancestor_set]
    if same_host:
        return _pick_session(same_host, conversation_id)

    # 兜底：若 MCP 服务由独立子进程托管导致不在祖先链上，从独立状态目录的存活服务中选
    return _pick_session([(path, data) for _, path, data in live], conversation_id)


def deliver(event: str, conversation_id: str, path: Path, ack_path: Path) -> None:
    # 拿锁后重读最新状态与 ack，防止并发 hook 之间读到旧快照
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ack = json.loads(ack_path.read_text(encoding="utf-8")) if ack_path.exists() else {}
    except (OSError, ValueError):
        emit({})  # 另一个进程正在写，放弃本次避免重复送达
        return
    if not isinstance(data, dict) or not isinstance(ack, dict):
        emit({})
        return

    tasks = data.get("tasks") or {}
    active_ids = set(tasks.keys())
    ack["delivered"] = [tid for tid in (ack.get("delivered") or []) if tid in active_ids]
    ack["stop_reminders"] = {tid: n for tid, n in (ack.get("stop_reminders") or {}).items() if tid in active_ids}

    def save_ack() -> None:
        tmp = ack_path.with_name(f"{ack_path.stem}.{os.getpid()}.tmp")
        body = json.dumps(ack)
        for attempt in range(5):
            try:
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, ack_path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        try:
            tmp.unlink()
        except OSError:
            pass

    finished = [t for t in tasks.values()
                if _belongs(t, conversation_id) and t["status"] in TERMINAL
                and not t.get("reported") and t["id"] not in ack["delivered"]]
    running = [t for t in tasks.values()
               if _belongs(t, conversation_id) and t["status"] == "running"
               and not t.get("detach") and not t.get("waited")]

    if finished:
        text = ("Codex subagent tasks finished; results below. Show review findings to the user before changing "
                "anything. A task reporting done is not sign-off; verify critical changes yourself.\n\n"
                + "\n\n".join(t["text"] for t in finished))
        ack["delivered"].extend(t["id"] for t in finished)
        save_ack()
        if event == "Stop":
            emit({"decision": "continue", "reason": text})
        else:
            emit({"injectSteps": [{"systemMessage": {"systemMessage": text}}]})
        return

    if event == "Stop" and running:
        to_remind = [t for t in running if ack["stop_reminders"].get(t["id"], 0) < MAX_STOP_REMINDERS]
        if not to_remind:
            emit({})
            return
        for t in to_remind:
            ack["stop_reminders"][t["id"]] = ack["stop_reminders"].get(t["id"], 0) + 1
        save_ack()
        ids = ", ".join(t["id"] for t in to_remind)
        emit({
            "decision": "continue",
            "reason": (
                f"Codex subagent tasks are still running: {ids}. If you need their results, call codex_wait "
                f"(or do other work first and wait later). If the user said not to wait, just end your turn: "
                f"the tasks keep running in the background and their results are delivered automatically next time. "
                f"Do not interrupt tasks just to end the turn."
            ),
        })
        return

    emit({})


def main() -> None:
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        payload = read_payload()
        if not event:
            event = str(payload.get("hookEventName") or "")

        # Stop 仅在正常结束回合时检查并提醒；用户取消、报错或触及上限直接结束，未送达结果留到下次 PreInvocation
        term_reason = str(payload.get("terminationReason") or "")
        if event == "Stop" and term_reason not in ("", "NO_TOOL_CALL"):
            emit({})
            return

        conversation_id = str(payload.get("conversationId") or "")
        path, data = find_session(event, conversation_id)
        if not path or data is None:
            emit({})
            return

        ack_path = path.with_name(path.stem + ".ack.json")
        # 并发触发多个 hook 进程时用锁文件互斥，抢不到锁直接放弃避免同一结果重复送达
        lock_path = path.with_name(path.stem + ".lock")
        try:
            if time.time() - lock_path.stat().st_mtime > 30:
                lock_path.unlink()  # 清理上一个 hook 超时终止后遗留的锁
        except OSError:
            pass
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            emit({})
            return
        os.close(fd)
        try:
            deliver(event, conversation_id, path, ack_path)
        finally:
            try:
                os.unlink(lock_path)
            except OSError:
                pass
    except Exception as exc:
        log(f"hook 异常: {exc!r}")
        emit({})


if __name__ == "__main__":
    main()
