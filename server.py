#!/usr/bin/env python3
"""
Codex-MCP -- 把 Codex 当子代理用的 MCP 服务（Windows，仅标准库）

一个常驻的 `codex app-server` 子进程承载所有任务：每个任务一个 thread，
派发后立即返回，状态靠事件流实时更新，支持中途 steer / interrupt / 续问。
任务状态写入状态文件，由 hook.py 送回派出它的 Claude 会话。
"""

import contextlib
import copy
import ctypes
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.2.8"


def _detect_client(argv: List[str]) -> str:
    for i, arg in enumerate(argv[1:], 1):
        if arg == "--client" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if arg.startswith("--client="):
            return arg.split("=", 1)[1].strip().lower()
    return os.environ.get("CODEX_SUB_CLIENT", "claude").strip().lower() or "claude"


CLIENT = _detect_client(sys.argv)
IS_AGY = CLIENT == "antigravity"

if IS_AGY:
    STATE_DIR = Path(os.environ.get("CODEX_SUB_AGY_STATE_DIR") or (
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "codex-sub-agy"))
else:
    STATE_DIR = Path(os.environ.get("CODEX_SUB_STATE_DIR") or (
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "codex-sub"))
LOG_FILE = STATE_DIR / "server.log"

# 子代理的模型参数独立于 ~/.codex/config.toml（桌面聊天可能开着 Fast，子代理走标准档）
DEFAULT_SANDBOX = os.environ.get("CODEX_SUB_SANDBOX", "danger-full-access")
DEFAULT_MODEL = os.environ.get("CODEX_SUB_MODEL", "gpt-6.2-sol")
DEFAULT_EFFORT = os.environ.get("CODEX_SUB_EFFORT", "high")
DEFAULT_SERVICE_TIER = os.environ.get("CODEX_SUB_SERVICE_TIER", "default")
FAST_SERVICE_TIER = "priority"  # Codex 界面里的 Fast

# 可选模型：Codex 界面显示名 -> app-server 的 model id
MODELS = ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra"]
MODEL_DESC = ("GPT-6.2 Sol = gpt-6.2-sol (default), GPT-6.2 Astra = gpt-6.2-astra, GPT-6.1 Terra = gpt-6.1-terra. "
              "Set only when the user asks for a specific model.")

MAX_WAIT_S = 3000  # Claude 下也是 codex_wait 的默认超时：等待转入后台后不占前台，一次等待应覆盖整个任务
# Antigravity 单次 MCP 调用固定超时上限为 3 分钟，单次等待上限控制在 150s 以内以优雅返回状态
WAIT_CAP_S = 150 if IS_AGY else MAX_WAIT_S
DEFAULT_WAIT_S = 120 if IS_AGY else MAX_WAIT_S
HEARTBEAT_S = 10
RPC_TIMEOUT_S = 60
RESULT_PREVIEW_CHARS = 6000

# 随 initialize 返回，Claude Code 会加载到系统提示
# 两个宿主共用的派发条件：只在这三种情况派，其余自己做
_WHEN_TO_DELEGATE = """When to delegate: only in these cases; otherwise do the work yourself.
- The user asks for Codex.
- The work splits into independent parts that can run in parallel with your own work or with each other.
- You want a second opinion on changes: use codex_review."""

_CLAUDE_INSTRUCTIONS = f"""codex-sub: run tasks on a Codex subagent (default GPT-6.2 Sol; GPT-6.2 Astra / GPT-6.1 Terra on request) in the background.
{_WHEN_TO_DELEGATE}
Writing the prompt: the subagent cannot see this conversation, so include the background, relevant file paths and constraints. State the goal, scope (which files or directories it may change), done criteria (which checks to run) and return format (conclusions with file:line, no large code dumps). Pass the project's absolute path as cwd. For follow-ups (thread_id), send only the new instructions.
Results: finished results are delivered to this session automatically. Show codex_review findings to the user before changing anything. A task reporting "done" is not sign-off; verify critical changes yourself.
Waiting: when you have nothing else to do, call codex_wait once. After about two minutes Claude Code moves the call to the background; end your turn then, and the result wakes this session when the task finishes. Do not start a second wait for tasks that already have one."""

_AGY_INSTRUCTIONS = f"""codex-sub: run tasks on a Codex subagent (default GPT-6.2 Sol; GPT-6.2 Astra / GPT-6.1 Terra on request) in the background.
{_WHEN_TO_DELEGATE}
Writing the prompt: Codex cannot see this conversation, so include the background, relevant file paths and constraints. State the goal, scope (which files or directories it may change), done criteria (which checks to run) and return format (conclusions with file:line, no large code dumps). Pass the current workspace's absolute path as cwd and your current Conversation ID as conversation_id. For follow-ups (thread_id), send only the new instructions.
Results: finished results are delivered to this conversation automatically by hooks. Show codex_review findings to the user before changing anything. A task reporting "done" is not sign-off; verify critical changes yourself.
Waiting: when you have nothing else to do, call codex_wait. If it times out, the task keeps running in the background; call codex_wait again or check codex_status."""

INSTRUCTIONS = _AGY_INSTRUCTIONS if IS_AGY else _CLAUDE_INSTRUCTIONS

# 经 thread/start、thread/resume 的 developerInstructions 交给每个子代理线程，~/.codex/AGENTS.md 照常加载；
# 与 gemini-sub 的同名常量一致。全局 AGENTS.md 里"先问用户"类规则在非交互模式下无法询问用户，第二条给出裁决
SUBAGENT_PROMPT = """You are a background subagent working on a task from another agent, and no one can answer questions.
- Do not ask or wait for confirmation; make reasonable assumptions and state them in your report. Actions your rules say need the user's confirmation: do them only if the task asks for them, otherwise list them as pending.
- Do the work yourself; do not hand it to other agents through MCP tools, even if a rule suggests it. Your own subagents are fine.
- Keep progress narration brief. Unless the task specifies an output format, end with a short report: what you did, files changed, checks run and their results, open issues."""


def log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 进程信息（Windows Toolhelp 快照；hook.py 复用）
# ---------------------------------------------------------------------------
def process_table() -> Dict[int, Tuple[int, str]]:
    """pid -> (ppid, exe 名)。"""
    result: Dict[int, Tuple[int, str]] = {}
    if os.name != "nt":
        return result

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == ctypes.c_void_p(-1).value:
        return result
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.decode("mbcs", "replace"))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return result


def ancestors(pid: int, table: Dict[int, Tuple[int, str]], limit: int = 8) -> List[int]:
    chain: List[int] = []
    cur = pid
    while len(chain) < limit and cur in table:
        cur = table[cur][0]
        if not cur or cur in chain:
            break
        chain.append(cur)
    return chain


def server_alive(table: Dict[int, Tuple[int, str]], server_pid: Any, claude_pid: Any) -> bool:
    """状态文件所属的 MCP 服务还活着：pid 存在、是 python、父进程就是文件名里的 Claude 进程（防 pid 复用）。"""
    entry = table.get(server_pid)
    return bool(entry) and entry[1].lower().startswith("python") and entry[0] == claude_pid


def terminate_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _attach_cleanup_job(proc: subprocess.Popen) -> Optional[int]:
    """Windows 下为子进程绑定由本 MCP 服务进程持有的 KILL_ON_JOB_CLOSE Job Object：本进程异常终止时由内核清理作业内进程树。"""
    if os.name != "nt":
        return None
    try:
        import ctypes.wintypes as wt

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k32.SetInformationJobObject.restype = wt.BOOL
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k32.AssignProcessToJobObject.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
                and k32.AssignProcessToJobObject(job, int(proc._handle))):
            return int(job)
        k32.CloseHandle(job)
    except Exception as exc:
        log(f"绑定 Job Object 失败（降级为退出清理）: {exc!r}")
    return None


def _close_job(job: Optional[int]) -> None:
    if job and os.name == "nt":
        try:
            import ctypes.wintypes as wt
            k32 = ctypes.windll.kernel32
            k32.CloseHandle.argtypes = [wt.HANDLE]
            k32.CloseHandle(job)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 定位 codex 可执行文件
# ---------------------------------------------------------------------------
def find_codex() -> List[str]:
    """优先桌面应用自带的最新版（能读新版 config.toml），其次 PATH。"""
    override = os.environ.get("CODEX_SUB_BIN")
    if override:
        return [override]
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = sorted(glob.glob(str(base / "*" / "codex.exe")), key=os.path.getmtime)
    if candidates:
        return [candidates[-1]]
    found = shutil.which("codex")
    if found:
        if found.lower().endswith((".cmd", ".bat")):
            # npm 的 cmd 垫片会让 cmd.exe 参与传参；直接调 node + codex.js
            js = Path(found).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = shutil.which("node")
            if js.exists() and node:
                return [node, str(js)]
        return [found]
    raise RuntimeError("codex not found; set CODEX_SUB_BIN to its path")


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
class Task:
    def __init__(self, task_id: str, prompt: str, cwd: str, detach: bool, conversation_id: str = ""):
        self.id = task_id
        self.prompt = prompt
        self.cwd = cwd
        self.detach = detach
        self.conversation_id = conversation_id
        self.thread_id = ""
        self.turn_id = ""
        self.status = "running"  # running | completed | interrupted | failed
        self.started_at = time.time()
        self.completed_at: Optional[float] = None
        self.model: Optional[str] = None
        self.branch: Optional[str] = None
        self.worktree: Optional[str] = None
        self.message = ""          # 最终/最新的助手消息
        self.message_stream = ""   # delta 累积，用于进度预览
        self.last_activity = ""
        self.files: List[str] = []
        self.error: Optional[str] = None
        self.reported = False      # 终态结果已通过工具返回给 Claude，hook 不再重复送达
        self.waiters = 0           # 正在等它的工具调用数；>0 时 Stop hook 跳过提醒
        self.done = threading.Event()

    @property
    def elapsed(self) -> float:
        return (self.completed_at or time.time()) - self.started_at

    def finish(self, status: str, error: Optional[str] = None) -> None:
        if self.done.is_set():
            return
        self.status = status
        self.error = error
        self.completed_at = time.time()
        self.done.set()


def format_task(task: Task, full: bool = False) -> str:
    head = f"[{task.id}] {task.status} | {task.elapsed:.0f}s | thread {task.thread_id or '-'}"
    if task.branch:
        head += f" | branch {task.branch} @ {task.worktree}"
    if task.detach:
        head += " | detached"
    lines = [head, f"  task: {task.prompt[:120]}"]
    if task.status == "running":
        if task.last_activity:
            lines.append(f"  last action: {task.last_activity}")
        preview = (task.message_stream or task.message).strip()
        if preview:
            lines.append(f"  output preview: …{preview[-300:]}")
        if task.files:
            lines.append(f"  files changed so far: {len(task.files)}")
        return "\n".join(lines)
    if task.files:
        lines.append("  files changed: " + ", ".join(task.files))
    if task.error:
        lines.append(f"  error: {task.error}")
    msg = task.message.strip() or "(no text output)"
    if not full and len(msg) > RESULT_PREVIEW_CHARS:
        msg = msg[:RESULT_PREVIEW_CHARS] + f'\n…(truncated; {len(task.message)} characters in total; codex_status(ids=["{task.id}"], full=true) for the full text)'
    lines.append("--- result ---" if task.status == "completed" else "--- output before it ended ---")
    lines.append(msg)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 状态文件（给 hook.py 读）
# ---------------------------------------------------------------------------
def session_files(path: Path) -> List[Path]:
    """一个会话的全部文件：状态、hook 的 ack 与锁、app-server 的 stderr。"""
    return [path] + [path.with_name(path.stem + s) for s in (".ack.json", ".lock", ".stderr")]


def parse_host_pid(path: Path) -> Optional[int]:
    """从 session-<host_pid>.json 或 session-<host_pid>-<server_pid>.json 解析宿主 pid。"""
    parts = path.stem.split("-")
    if len(parts) not in (2, 3) or parts[0] != "session":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


class State:
    def __init__(self) -> None:
        self.tasks: Dict[str, Task] = {}
        self.claude_pid = os.getppid()
        # Antigravity 下同一 Language Server 刷新 MCP 时 host_pid 不变，带 server_pid 避免旧进程 cleanup 误删新进程文件
        self.path = STATE_DIR / (f"session-{self.claude_pid}-{os.getpid()}.json" if IS_AGY
                                 else f"session-{self.claude_pid}.json")
        self.stderr_path = session_files(self.path)[3]
        self.app_server_pid: Optional[int] = None
        self.closing = False
        self._lock = threading.Lock()

    def add(self, task: Task) -> None:
        with self._lock:
            self.tasks[task.id] = task

    def flush(self) -> None:
        with self._lock:
            if self.closing:
                return
            all_tasks = list(self.tasks.values())
            if IS_AGY:
                # 同步 hook 已送达标记到内存 reported（粘性置 True），之后既从状态文件移除、也不会因 ack 裁剪而重发
                ack_path = session_files(self.path)[1]
                try:
                    delivered = set((json.loads(ack_path.read_text(encoding="utf-8")) or {}).get("delivered") or ())
                except (OSError, ValueError, AttributeError):
                    delivered = set()
                if delivered:
                    for t in all_tasks:
                        if t.done.is_set() and t.id in delivered:
                            t.reported = True
                # 仅写入运行中或尚未送达的终态任务，避免长驻服务状态文件无限膨胀
                tasks = {
                    t.id: {"id": t.id, "status": t.status, "detach": t.detach,
                           "conversation_id": t.conversation_id,
                           "reported": t.reported, "waited": t.waiters > 0,
                           "text": format_task(t)}
                    for t in all_tasks
                    if t.status == "running" or not t.reported
                }
                data = {
                    "host_pid": self.claude_pid, "server_pid": os.getpid(),
                    "app_server_pid": self.app_server_pid, "updated": time.time(),
                    "tasks": tasks,
                }
            else:
                data = {
                    "server_pid": os.getpid(), "app_server_pid": self.app_server_pid, "updated": time.time(),
                    "tasks": {t.id: {"id": t.id, "status": t.status, "detach": t.detach,
                                     "reported": t.reported, "waited": t.waiters > 0,
                                     "text": format_task(t)}
                              for t in all_tasks},
                }
            body = json.dumps(data, ensure_ascii=False)
            tmp = self.path.with_suffix(".tmp")
            for attempt in range(5):
                try:
                    tmp.write_text(body, encoding="utf-8")
                    os.replace(tmp, self.path)
                    return
                except PermissionError:  # Windows 上 hook 正在读时 replace 会失败
                    time.sleep(0.05 * (attempt + 1))
            log("状态文件写入失败（持续被占用）")

    def cleanup(self) -> None:
        with self._lock:
            self.closing = True
        for p in session_files(self.path):
            try:
                p.unlink()
            except OSError:
                pass


def cleanup_stale_sessions() -> None:
    """清理已退出 MCP 服务的旧会话文件，并终止残留的 app-server（父进程异常终止时 finally 不会执行）。"""
    snapshot_at = time.time()
    table = process_table()
    for path in STATE_DIR.glob("session-*.json"):
        if path.name.endswith(".ack.json"):
            continue
        try:
            # 快照之后才写的文件，其服务可能刚启动、不在快照里
            if path.stat().st_mtime >= snapshot_at - 1:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        claude_pid = data.get("host_pid") if isinstance(data.get("host_pid"), int) else parse_host_pid(path)
        if claude_pid is None:
            continue
        if server_alive(table, data.get("server_pid"), claude_pid):
            continue
        # Windows 不会给残留子进程换父进程：父 pid 仍是已退出的 MCP 服务，借此排除 pid 被复用成别的进程
        app_pid = data.get("app_server_pid")
        entry = table.get(app_pid)
        if entry and entry[0] == data.get("server_pid") and entry[1].lower().startswith(("codex", "node")):
            log(f"清理残留 app-server {app_pid}（会话文件 {path.name}）")
            terminate_tree(app_pid)
        for p in session_files(path):
            try:
                p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# codex app-server 客户端
# ---------------------------------------------------------------------------
# 非交互模式：自动拒绝服务端发来的交互式审批与提问请求，按各方法要求的应答格式
_NON_INTERACTIVE_REPLIES: Dict[str, Dict[str, Any]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/requestUserInput": {"answers": {}},
    "item/permissions/requestApproval": {"permissions": {}},
    "item/tool/call": {"contentItems": [], "success": False},
    "applyPatchApproval": {"decision": "abort"},
    "execCommandApproval": {"decision": "abort"},
}


class AppServer:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self._start_lock = threading.Lock()
        self._lock = threading.Lock()
        # 写管道可能阻塞（app-server 来不及读），不能占着读循环也要用的 _lock
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._events: Dict[int, threading.Event] = {}
        self.by_thread: Dict[str, Task] = {}
        self.launch_lock = threading.Lock()  # 开 thread + 起 turn 串行化，防止并发续接同一 thread
        self._stderr = None
        self._job: Optional[int] = None

    def ensure(self) -> None:
        with self._start_lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._reset("app-server is not running")
            cmd = find_codex() + ["app-server"]
            log(f"启动 app-server: {cmd}")
            _close_job(self._job)
            self._job = None
            if self._stderr:
                self._stderr.close()
                self._stderr = None
            self._stderr = open(STATE.stderr_path, "wb")
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr)
            except Exception:
                self._stderr.close()
                self._stderr = None
                raise
            self._job = _attach_cleanup_job(proc)
            with self._lock:
                self.proc = proc
            reader = threading.Thread(target=self._reader, args=(proc,), daemon=True)
            reader.start()
            threading.Thread(target=self._watch, args=(proc, reader), daemon=True).start()
            try:
                self.request("initialize", {
                    "clientInfo": {"name": "codex-sub-agy" if IS_AGY else "codex-sub", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                })
                self._write({"jsonrpc": "2.0", "method": "initialized"})
            except Exception:
                # 先终止进程树再关 Job：覆盖 Popen 返回到绑定 Job 之间已拉起的子进程
                terminate_tree(proc.pid)
                job, self._job = self._job, None
                _close_job(job)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2)
                with self._lock:
                    self.proc = None
                if self._stderr:
                    self._stderr.close()
                    self._stderr = None
                raise
            STATE.app_server_pid = proc.pid
            STATE.flush()

    def stop(self) -> None:
        with self._lock:
            proc, self.proc = self.proc, None  # 读循环见 proc 已换掉，不再读 stderr、不再回写状态
        job, self._job = self._job, None
        if proc:
            # 先 taskkill /T 再关 Job：即使启动器在绑定 Job 前已派生子进程也能整棵清掉
            terminate_tree(proc.pid)
        _close_job(job)
        if proc:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        if self._stderr:
            self._stderr.close()
            self._stderr = None

    def _reset(self, reason: str, proc: Optional[subprocess.Popen] = None) -> None:
        """进程没了：唤醒所有在途请求，未完成任务标失败。

        传 proc 时只在它仍是当前进程时清，并把当前进程置空：新进程已经起来的话，ensure() 起它之前已经清过，
        旧进程的退出不能再动新进程的请求和任务。
        """
        with self._lock:
            if proc is not None:
                if self.proc is not proc:
                    return
                self.proc = None  # 读线程和 _watch 都会报告同一次退出，只处理一次
            for rid, ev in self._events.items():
                self._pending[rid] = {"error": {"message": reason}}
                ev.set()
            self._events.clear()
            tasks = list(self.by_thread.values())
            self.by_thread.clear()
        for task in tasks:
            task.finish("failed", reason)

    def _write(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            proc = self.proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise RuntimeError("app-server is not running")
            proc.stdin.write(data)
            proc.stdin.flush()

    def _write_quiet(self, obj: Dict[str, Any]) -> None:
        try:
            self._write(obj)
        except (OSError, RuntimeError) as exc:
            log(f"应答 app-server 请求失败: {exc!r}")

    def request(self, method: str, params: Dict[str, Any], timeout: float = RPC_TIMEOUT_S) -> Dict[str, Any]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            ev = self._events[rid] = threading.Event()
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            ok = ev.wait(timeout)
        finally:
            with self._lock:
                self._events.pop(rid, None)
                resp = self._pending.pop(rid, None)
        if not ok or resp is None:
            raise RuntimeError(f"{method} timed out after {timeout}s")
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error'].get('message', resp['error'])}")
        return resp.get("result") or {}

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        for raw in proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
                if "method" not in msg:
                    with self._lock:
                        ev = self._events.get(msg.get("id"))
                        if ev is not None:
                            self._pending[msg["id"]] = msg
                            ev.set()
                elif "id" in msg:
                    reply = _NON_INTERACTIVE_REPLIES.get(msg["method"])
                    out = ({"jsonrpc": "2.0", "id": msg["id"], "result": reply} if reply is not None else
                           {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "codex-sub does not handle this request"}})
                    # 另起线程写：读循环一旦卡在写管道上，app-server 的输出没人读，双方互等
                    threading.Thread(target=self._write_quiet, args=(out,), daemon=True).start()
                else:
                    self._on_notification(msg["method"], msg.get("params") or {})
            except Exception as exc:  # 单条消息出错不能中断读循环
                log(f"读循环异常: {exc!r}")
        self._on_exit(proc)

    def _watch(self, proc: subprocess.Popen, reader: threading.Thread) -> None:
        """app-server 派生的进程可能继承它的 stdout，它退出后管道不一定 EOF：按进程退出判定，先给读线程几秒读完剩余输出。"""
        proc.wait()
        reader.join(timeout=5)
        self._on_exit(proc)

    def _on_exit(self, proc: subprocess.Popen) -> None:
        if self.proc is not proc:
            return
        tail = _stderr_tail()
        log(f"app-server 退出: {tail[-200:]}")
        self._reset("app-server exited" + (f": {tail[-500:]}" if tail else ""), proc)
        STATE.flush()

    def _on_notification(self, method: str, p: Dict[str, Any]) -> None:
        task = self.by_thread.get(p.get("threadId", ""))
        if task is None:
            return
        turn_id = p.get("turnId") or (p.get("turn") or {}).get("id")
        if task.turn_id and turn_id and turn_id != task.turn_id:
            return  # 同一 thread 上旧 turn 迟到的事件
        if method == "turn/started":
            task.turn_id = task.turn_id or turn_id or ""
        elif method == "item/started":
            item = p.get("item") or {}
            kind = item.get("type")
            if kind == "commandExecution":
                task.last_activity = "$ " + str(item.get("command", ""))[:200]
            elif kind == "fileChange":
                task.last_activity = "edit " + ", ".join(c.get("path", "") for c in item.get("changes", []))[:200]
            elif kind == "mcpToolCall":
                task.last_activity = f"MCP {item.get('server')}/{item.get('tool')}"
            elif kind == "webSearch":
                task.last_activity = "search " + str(item.get("query", ""))[:120]
        elif method == "item/agentMessage/delta":
            task.message_stream += p.get("delta", "")
        elif method == "item/completed":
            item = p.get("item") or {}
            kind = item.get("type")
            if kind == "agentMessage":
                task.message = item.get("text") or task.message_stream
                task.message_stream = ""
            elif kind == "exitedReviewMode" and item.get("review"):
                task.message = item["review"]
            elif kind == "fileChange":
                for c in item.get("changes", []):
                    path = c.get("path")
                    if path and path not in task.files:
                        task.files.append(path)
        elif method == "thread/status/changed":
            flags = (p.get("status") or {}).get("activeFlags") or []
            if flags:
                task.last_activity = "waiting for approval/input (declined automatically): " + ", ".join(flags)
        elif method == "error":
            msg = (p.get("error") or {}).get("message", "")
            task.last_activity = ("retrying: " if p.get("willRetry") else "error: ") + msg[:200]
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            if not task.message and task.message_stream:
                task.message = task.message_stream
            err = turn.get("error") or {}
            error = None
            if err:
                error = err.get("message", "")
                if err.get("codexErrorInfo"):
                    error = f"[{err['codexErrorInfo']}] {error}"
            task.finish(turn.get("status", "completed"), error)
            STATE.flush()


def _stderr_tail() -> str:
    try:
        with open(STATE.stderr_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2000))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


APP = AppServer()
STATE = State()


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
class Call:
    """一次 tools/call：进度通知，以及客户端的取消（用户按 Esc、调用超时都会发 notifications/cancelled）。"""

    def __init__(self, token: Any = None) -> None:
        self.token = token
        self.cancelled = threading.Event()
        self.reports: List[Task] = []  # 应答里带了终态结果的任务

    def progress(self, text: str) -> None:
        if self.token is not None:
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": self.token, "progress": time.time(), "message": text}})


def _create_worktree(task_id: str, cwd: str) -> Dict[str, str]:
    run = lambda *a: subprocess.run(
        ["git", "-C", cwd, *a], stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    top = run("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(f"worktree requires a git repository: {top.stderr.strip()}")
    root = Path(top.stdout.strip())
    sub = Path(cwd).resolve().relative_to(root.resolve())
    branch = f"codex-sub/{task_id}"
    wt = root.parent / f"{root.name}.codex-sub" / task_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = run("worktree", "add", "-b", branch, str(wt), "HEAD")
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")
    return {"branch": branch, "worktree": str(wt), "cwd": str(wt / sub)}


def _remove_worktree(task: Task, repo_cwd: str) -> None:
    """从原仓库目录执行；Windows 下在待删目录内跑 git 会导致目录无法删除。"""
    if not task.worktree:
        return
    for args in (["worktree", "remove", "--force", task.worktree], ["branch", "-D", task.branch or ""]):
        subprocess.run(["git", "-C", repo_cwd, *args], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _open_thread(task: Task, a: Dict[str, Any], sandbox: str) -> None:
    """新建 thread 或续接旧 thread，填好 task.thread_id / task.model，并登记到 by_thread。"""
    thread_id = a.get("thread_id")
    base = {"cwd": task.cwd, "sandbox": sandbox, "approvalPolicy": "never",
            "model": a.get("model") or DEFAULT_MODEL, "serviceTier": DEFAULT_SERVICE_TIER,
            "developerInstructions": SUBAGENT_PROMPT}
    if thread_id:
        prev = APP.by_thread.get(thread_id)
        if prev is not None:
            if not prev.done.is_set():
                raise ValueError(f"task {prev.id} is still running on thread {thread_id}; wait for it to finish or codex_interrupt it first")
            # 本进程派出的 thread 仍在 app-server 内存里，直接开新 turn（临时会话也行）；
            # 没指定的 cwd / model 沿用原 thread，指定了的由 turn/start 覆盖
            task.thread_id, task.model = thread_id, a.get("model") or prev.model
            if not a.get("cwd"):
                task.cwd = prev.cwd
        else:
            # 上次会话持久化的 thread，需要从磁盘恢复；没指定的 cwd / model 不覆盖
            params = dict(base, threadId=thread_id)
            for key in ("cwd", "model"):
                if not a.get(key):
                    del params[key]
            try:
                res = APP.request("thread/resume", params)
            except RuntimeError as exc:
                raise RuntimeError(f"cannot continue {thread_id}: {exc}. Ephemeral threads can only be continued while this session's "
                                   "app-server is alive; dispatch with persist=true to continue across sessions") from exc
            task.thread_id, task.model = str(res["thread"]["id"]), res.get("model")
            task.cwd = res.get("cwd") or task.cwd
    else:
        # 默认临时会话：不写入 ~/.codex，也不会出现在 Codex 应用的历史里
        res = APP.request("thread/start", dict(base, ephemeral=not a.get("persist")))
        task.thread_id, task.model = str(res["thread"]["id"]), res.get("model")
        if a.get("persist"):
            try:
                APP.request("thread/name/set", {"threadId": task.thread_id, "name": f"codex-sub {task.id}: {task.prompt[:40]}"})
            except RuntimeError:
                pass
    APP.by_thread[task.thread_id] = task


def _launch(a: Dict[str, Any], prompt: str, sandbox: str, start_turn, call: "Call") -> str:
    """spawn / review 的公共流程：建任务 -> 开 thread -> 起 turn -> 登记；任一步失败都不留半成品。"""
    cwd = os.path.abspath(a.get("cwd") or os.getcwd())
    if not os.path.isdir(cwd):
        raise ValueError(f"cwd does not exist: {cwd}")
    if a.get("worktree") and a.get("thread_id"):
        raise ValueError("worktree cannot be combined with thread_id")
    task = Task(uuid.uuid4().hex[:8], prompt, cwd, bool(a.get("detach")),
                conversation_id=str(a.get("conversation_id") or "").strip())
    if IS_AGY:
        log(f"launch task={task.id} conv={task.conversation_id or '-'} cwd={cwd}")
    try:
        if a.get("worktree"):
            info = _create_worktree(task.id, cwd)
            task.branch, task.worktree, task.cwd = info["branch"], info["worktree"], info["cwd"]
        APP.ensure()
        with APP.launch_lock:
            _open_thread(task, a, sandbox)
            try:
                task.turn_id = start_turn(task)
            except Exception:
                if APP.by_thread.get(task.thread_id) is task:
                    del APP.by_thread[task.thread_id]
                raise
    except Exception:
        _remove_worktree(task, cwd)
        raise
    STATE.add(task)
    STATE.flush()

    wait_s = max(0.0, min(float(a.get("wait_s") or 0), WAIT_CAP_S))
    if wait_s <= 0:
        return _report(task, call)
    with _waiting([task]):
        _wait([task], wait_s, call)
        return _deliver(task, call, _hook_delivered())


def _report(task: Task, call: "Call", full: bool = False) -> str:
    """记下这次调用要交付的终态结果；handle() 成功发出应答后才标 reported，hook 之后不再重复送达。"""
    if task.done.is_set() and not task.reported:
        call.reports.append(task)
    return format_task(task, full)


def _hook_delivered() -> set:
    """Claude hook 已经送进会话的任务 id（.ack.json）。读不到就当没有：宁可重复，不能漏送。

    Antigravity 的 hook 没有按长度分批送达，那边一律返回全文。
    """
    if IS_AGY:
        return set()
    try:
        data = json.loads(session_files(STATE.path)[1].read_text(encoding="utf-8"))
        return set(data.get("delivered") or ())
    except (OSError, ValueError, AttributeError):
        return set()


def _deliver(task: Task, call: "Call", delivered: set) -> str:
    """等待类调用的返回：hook 在等待期间已整段送达的结果只给状态行，同一结果不在会话里出现两次。"""
    if task.done.is_set() and task.id in delivered:
        _report(task, call)
        return (format_task(task).splitlines()[0] + "\n  result already delivered to this session; "
                f'codex_status(ids=["{task.id}"]) shows it again')
    return _report(task, call)


def tool_spawn(a: Dict[str, Any], call: "Call") -> str:
    prompt = (a.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt must not be empty")
    effort = a.get("effort") or DEFAULT_EFFORT

    def start(task: Task) -> str:
        # turn/start 的 serviceTier 对之后的回合也生效，每回合都显式传，续问才不会沿用上一回合的 Fast
        params = {"threadId": task.thread_id, "cwd": task.cwd, "effort": effort,
                  "serviceTier": FAST_SERVICE_TIER if a.get("fast") else DEFAULT_SERVICE_TIER,
                  "input": [{"type": "text", "text": prompt}]}
        if a.get("model"):
            params["model"] = a["model"]
        res = APP.request("turn/start", params)
        return res["turn"]["id"]

    return _launch(a, prompt, DEFAULT_SANDBOX, start, call)


def tool_review(a: Dict[str, Any], call: "Call") -> str:
    """Codex 内置代码审查模式（等价于 codex review）：找缺陷，不改文件。"""
    scope = a.get("scope") or "uncommitted"
    value = a.get("value") or ""
    targets = {
        "uncommitted": lambda: {"type": "uncommittedChanges"},
        "base_branch": lambda: {"type": "baseBranch", "branch": value},
        "commit": lambda: {"type": "commit", "sha": value},
        "custom": lambda: {"type": "custom", "instructions": value},
    }
    if scope not in targets:
        raise ValueError(f"unknown scope: {scope}")
    if scope != "uncommitted" and not value:
        raise ValueError(f"scope={scope} requires value")

    def start(task: Task) -> str:
        res = APP.request("review/start", {"threadId": task.thread_id, "target": targets[scope](), "delivery": "inline"})
        return res["turn"]["id"]

    return _launch(dict(a, thread_id=None), f"[review:{scope}] {value}".strip(), DEFAULT_SANDBOX, start, call)


_waiters_lock = threading.Lock()


@contextlib.contextmanager
def _waiting(tasks: List[Task]):
    """登记"有工具调用在等这些任务"，写进状态文件供 Stop hook 判断。

    Claude Code 把运行超过约两分钟的 MCP 调用转入后台，调用返回时唤醒会话；回合结束时仍在途的等待
    只可能是这种后台调用，所以它等着的任务不需要 Stop hook 再提醒。调用被取消或超时返回即撤销登记。
    """
    with _waiters_lock:
        for t in tasks:
            t.waiters += 1
    STATE.flush()
    try:
        yield
    finally:
        with _waiters_lock:
            for t in tasks:
                t.waiters -= 1
        STATE.flush()


def _wait(tasks: List[Task], timeout: float, call: "Call", mode: str = "all") -> None:
    deadline = time.time() + timeout
    last_beat = time.time()
    while True:
        pending = [t for t in tasks if not t.done.is_set()]
        if (not pending or (mode == "any" and len(pending) < len(tasks)) or time.time() >= deadline
                or call.cancelled.is_set()):
            return
        pending[0].done.wait(min(2.0, max(0.1, deadline - time.time())))
        if time.time() - last_beat >= HEARTBEAT_S:
            last_beat = time.time()
            call.progress(f"waiting for {len(pending)} Codex task(s): " + "; ".join(
                f"{t.id} {t.last_activity or t.status}"[:80] for t in pending))


def _get(task_id: str) -> Task:
    task = STATE.tasks.get(task_id)
    if task is None:
        raise ValueError(f"no task {task_id}")
    return task


def _matches_conv(task: Task, conv_id: str) -> bool:
    return not conv_id or task.conversation_id == conv_id


def tool_wait(a: Dict[str, Any], call: "Call") -> str:
    conv_id = str(a.get("conversation_id") or "").strip()
    ids = a.get("ids") or [t.id for t in list(STATE.tasks.values())
                           if not t.done.is_set() and _matches_conv(t, conv_id)]
    if not ids:
        return "no tasks to wait for"
    tasks = [_get(i) for i in ids]
    raw_timeout = a.get("timeout")
    timeout = min(float(DEFAULT_WAIT_S if raw_timeout in (None, 0) else raw_timeout), WAIT_CAP_S)
    with _waiting(tasks):
        _wait(tasks, timeout, call, a.get("mode") or "all")
        delivered = _hook_delivered()
        out = [_deliver(t, call, delivered) for t in tasks]
    still = [t.id for t in tasks if not t.done.is_set()]
    if still:
        out.append(f"note: {', '.join(still)} still running (a wait timeout is not a failure); call codex_wait again or check codex_status.")
    return "\n\n".join(out)


def tool_status(a: Dict[str, Any], call: "Call") -> str:
    ids = a.get("ids")
    if not ids:
        conv_id = str(a.get("conversation_id") or "").strip()
        tasks = [t for t in list(STATE.tasks.values()) if _matches_conv(t, conv_id)]
        if not tasks:
            return "no tasks"
        return "\n".join(
            f"[{t.id}] {t.status:<11} {t.elapsed:5.0f}s  {t.prompt[:70]!r}" + ("  (detached)" if t.detach else "")
            for t in tasks)
    return "\n\n".join(_report(_get(i), call, bool(a.get("full"))) for i in ids)


def tool_steer(a: Dict[str, Any], call: "Call") -> str:
    task = _get(a["id"])
    if task.done.is_set():
        raise ValueError(f"task {task.id} has ended ({task.status}); continue with codex_spawn(thread_id=...)")
    APP.request("turn/steer", {"threadId": task.thread_id, "expectedTurnId": task.turn_id,
                               "input": [{"type": "text", "text": a["prompt"]}]})
    task.last_activity = "received new instructions"
    return f"sent the new instructions to task {task.id} (Codex changes course after its current step)"


def tool_interrupt(a: Dict[str, Any], call: "Call") -> str:
    task = _get(a["id"])
    if not task.done.is_set():
        APP.request("turn/interrupt", {"threadId": task.thread_id, "turnId": task.turn_id})
        task.done.wait(15)
    return _report(task, call)


_SPAWN_SCHEMA = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string", "description": (
            "Task description: goal, scope (which files or directories it may change), done criteria "
            "(which checks to run) and return format (conclusions with file:line, no large code dumps). "
            "The subagent cannot see this conversation.")},
        "cwd": {"type": "string", "description": (
            "Working directory (absolute path); defaults to the directory Claude Code was started in")},
        "wait_s": {"type": "number", "description": (
            "Wait this many seconds before returning: 120-300 gets the result directly for tasks expected to "
            "finish within a few minutes; 0 returns immediately for long tasks.")},
        "thread_id": {"type": "string", "description": (
            "Follow-up: continue on an earlier task's thread with its full context; send only the new instructions.")},
        "persist": {"type": "boolean", "description": (
            "Save the thread to Codex history so a later Claude session can continue it with thread_id; "
            "off by default.")},
        "model": {"type": "string", "enum": MODELS, "description": MODEL_DESC},
        "effort": {"type": "string", "description": (
            "Reasoning effort, default high; valid values depend on the model, and an unsupported value fails "
            "the task. Set only when the user asks.")},
        "fast": {"type": "boolean", "description": (
            "true = use Codex's Fast service tier for this turn; off by default and not carried over to "
            "follow-ups. Set only when the user asks.")},
        "worktree": {"type": "boolean", "description": (
            "Run in a separate git worktree (branch codex-sub/<id>); use when several tasks edit the same "
            "repository in parallel, and merge the branch yourself afterwards. Created from the current HEAD, "
            "so uncommitted changes are not visible.")},
        "detach": {"type": "boolean", "description": (
            "true = background task whose result you don't need; Claude is not stopped at the end of a turn "
            "because of it.")},
    },
}

TOOLS = [
    {"name": "codex_spawn",
     "description": (
         "Dispatch a task to a Codex subagent and return an 8-character task id immediately "
         "(with wait_s > 0, wait that long first and return the result if the task finishes). "
         "You can dispatch several; they run in parallel in one resident Codex process. Results are delivered to "
         "this session automatically when tasks finish; you can also wait with codex_wait, check progress with "
         "codex_status, redirect with codex_steer, or stop with codex_interrupt. "
         "Pass thread_id to continue on an earlier task's thread; that thread must have no running task. "
         "Runs with sandbox off and tool approvals disabled by default: Codex edits files and runs commands directly."),
     "inputSchema": _SPAWN_SCHEMA,
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}},
    {"name": "codex_review",
     "description": (
         "Review changes with Codex's built-in review mode (same as `codex review`): it does not edit files, "
         "focuses on defects, and returns issues ordered by severity with file:line. "
         "scope=uncommitted reviews uncommitted changes in the working tree; base_branch reviews the current branch "
         "against a base branch (value = branch name); commit reviews one commit (value = sha); custom follows your "
         "instructions (value = instructions). Returns a task id; waiting works as in codex_spawn. "
         "Use codex_spawn to have Codex change code; use this only to find problems."),
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string", "enum": ["uncommitted", "base_branch", "commit", "custom"],
                   "description": "Review scope, default uncommitted"},
         "value": {"type": "string", "description": (
             "Branch name / commit sha / custom instructions, depending on scope; not needed for uncommitted")},
         "cwd": {"type": "string", "description": (
             "Git repository directory (absolute path); defaults to the directory Claude Code was started in")},
         "wait_s": {"type": "number", "description": (
             "Wait this many seconds first; reviews usually take 1-3 minutes; 0 returns immediately")},
         "model": {"type": "string", "enum": MODELS, "description": MODEL_DESC},
         "detach": {"type": "boolean", "description": (
             "true = you don't need the result; Claude is not stopped at the end of a turn because of it")}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}},
    {"name": "codex_wait",
     "description": (
         "Wait for one or more tasks to finish and return their results. Without ids, waits for all running tasks "
         "in this session; finished tasks return immediately. After about two minutes Claude Code moves the call to "
         "the background: you can keep working or end your turn, and the result is delivered and wakes the session "
         "when the tasks finish, so one wait per batch is enough; do not call it again. A timeout only returns the "
         "current status; tasks keep running and you can wait again. Sends a progress notification every 10 seconds. "
         "To check without waiting, use codex_status."),
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}, "description": (
             "Task ids (the 8-character ids from codex_spawn / codex_review); omit for all running tasks")},
         "timeout": {"type": "number", "description": (
             f"Maximum seconds to wait; defaults to the cap {MAX_WAIT_S}; usually omit")},
         "mode": {"type": "string", "enum": ["all", "any"], "description": (
             "all = return when all tasks finish (default); any = return when any finishes")}}},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "codex_status",
     "description": (
         "Returns immediately without waiting. Without ids: one line per task in this session with its status. "
         "With ids: details. For running tasks, the latest action, changed files and an output preview; for "
         "finished tasks, the result (truncated beyond 6000 characters; full=true for everything). "
         "Also where you get the partial output of a task stopped with codex_interrupt."),
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}, "description": "Task ids; omit to list tasks only"},
         "full": {"type": "boolean", "description": "true = return the full result text without truncation"}}},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "codex_steer",
     "description": (
         "Add to or correct the instructions of a running task. Codex changes course after its current step "
         "without starting over and keeps the work done so far. Only for running tasks: for a finished task it "
         "returns an error, and you should continue with codex_spawn(thread_id=...) instead. "
         "To stop rather than redirect, use codex_interrupt."),
     "inputSchema": {"type": "object", "required": ["id", "prompt"], "properties": {
         "id": {"type": "string", "description": "8-character id of the running task (from codex_spawn)"},
         "prompt": {"type": "string", "description": "Only the additional instructions; do not repeat the original task"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    {"name": "codex_interrupt",
     "description": (
         "Stop a running task. Codex stops immediately, partial output and file changes are kept, and the task "
         "becomes interrupted; see the result with codex_status. Calling it on a finished task has no side effects "
         "and returns its result. The interrupted thread can still be continued with codex_spawn(thread_id=...). "
         "To redirect rather than stop, use codex_steer."),
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string", "description": "8-character task id (from codex_spawn / codex_review)"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]

def _build_agy_tools(base_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Antigravity 模式下的工具定义：补齐 conversation_id 隔离参数与宿主文案，不影响 Claude 默认定义。"""
    tools = copy.deepcopy(base_tools)
    by_name = {t["name"]: t for t in tools}
    conversation_desc = ("Your current Antigravity Conversation ID; required for hooks to deliver the result to "
                         "this conversation automatically")
    filter_desc = "Without ids, only the tasks of this Conversation ID"

    spawn = by_name["codex_spawn"]
    spawn["description"] += " Always pass the current workspace's absolute path as cwd."
    sp = spawn["inputSchema"]["properties"]
    sp["cwd"]["description"] = ("Working directory (absolute path). Always pass the current project/workspace path; "
                                "may be omitted for thread_id follow-ups to keep the original directory")
    sp["conversation_id"] = {"type": "string", "description": conversation_desc}
    sp["wait_s"]["description"] = (f"Wait this many seconds first (cap {WAIT_CAP_S}): 30-120 gets the result directly "
                                   "for short tasks; 0 returns immediately for long tasks")
    sp["persist"]["description"] = ("Save the thread to Codex history so it can be continued with thread_id after "
                                    "the server restarts; off by default")
    sp["detach"]["description"] = ("true = background task whose result you don't need; no reminder about it at the "
                                   "end of a turn")

    review = by_name["codex_review"]
    review["description"] += " Always pass the git repository's absolute path as cwd."
    rp = review["inputSchema"]["properties"]
    rp["cwd"]["description"] = "Git repository directory (absolute path); always pass the current project/workspace path"
    rp["conversation_id"] = {"type": "string", "description": conversation_desc}
    rp["wait_s"]["description"] = (f"Wait this many seconds first (cap {WAIT_CAP_S}); reviews usually take "
                                   "1-3 minutes; 0 returns immediately")
    rp["detach"]["description"] = "true = you don't need the result; no reminder about it at the end of a turn"

    wait = by_name["codex_wait"]
    wait["description"] = (
        "Wait for one or more tasks to finish and return their results. Pass ids to wait for specific tasks; "
        "without ids, pass conversation_id to wait only for this conversation's running tasks (otherwise all "
        "running tasks). Finished tasks return immediately. A timeout only returns the status; tasks keep running "
        "and you can wait again. Sends a progress notification every 10 seconds. "
        "To check without waiting, use codex_status."
    )
    wp = wait["inputSchema"]["properties"]
    wp["ids"]["description"] = "Task ids (8-character ids from codex_spawn / codex_review); omit for running tasks"
    wp["conversation_id"] = {"type": "string", "description": filter_desc}
    wp["timeout"]["description"] = f"Maximum seconds to wait; default {DEFAULT_WAIT_S}, cap {WAIT_CAP_S}"

    status = by_name["codex_status"]
    status["description"] = (
        "Returns immediately without waiting. Without ids: one line per task with its status (pass conversation_id "
        "to list only this conversation's tasks). With ids: details. For running tasks, the latest action, changed "
        "files and an output preview; for finished tasks, the result (truncated beyond 6000 characters; full=true "
        "for everything). Also where you get the partial output of a task stopped with codex_interrupt."
    )
    status["inputSchema"]["properties"]["conversation_id"] = {"type": "string", "description": filter_desc}
    return tools

if IS_AGY:
    TOOLS = _build_agy_tools(TOOLS)

HANDLERS = {
    "codex_spawn": tool_spawn, "codex_review": tool_review, "codex_wait": tool_wait,
    "codex_status": tool_status, "codex_steer": tool_steer, "codex_interrupt": tool_interrupt,
}


# ---------------------------------------------------------------------------
# MCP stdio 服务
# ---------------------------------------------------------------------------
_out_lock = threading.Lock()
_CALLS: Dict[Any, Call] = {}  # 进行中的 tools/call，按请求 id 找到它好响应取消


def send(obj: Dict[str, Any]) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def handle(req: Dict[str, Any]) -> None:
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codex-sub", "version": __version__},
            "instructions": INSTRUCTIONS}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = str(params.get("name"))
        fn = HANDLERS.get(name)
        call = _CALLS.get(rid) or Call((params.get("_meta") or {}).get("progressToken"))
        try:
            if fn is None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown tool {name}"}})
                return
            if call.cancelled.is_set():
                return
            ok = True
            try:
                result = {"content": [{"type": "text", "text": fn(params.get("arguments") or {}, call)}]}
            except Exception as exc:
                ok = False
                log(f"{name} 失败: {exc!r}")
                result = {"isError": True, "content": [{"type": "text", "text": f"{name} failed: {exc}"}]}
        finally:
            _CALLS.pop(rid, None)
        if call.cancelled.is_set():  # 已取消的请求按 MCP 规范不再应答，结果留给 hook 送
            return
        send({"jsonrpc": "2.0", "id": rid, "result": result})
        # 报错的调用不算交付了其中的结果（例如 codex_status 的 ids 里混了不存在的 id）
        if ok and call.reports:
            for t in call.reports:
                t.reported = True
            STATE.flush()
    elif method == "notifications/cancelled":
        call = _CALLS.get(params.get("requestId"))
        if call is not None:
            call.cancelled.set()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unsupported method {method}"}})


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows 默认 GBK
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_sessions()
    log(f"codex-sub ({CLIENT}) {__version__} 启动，host pid {STATE.claude_pid}")
    STATE.flush()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if req.get("method") == "tools/call":
                # 先登记再交给工作线程：紧跟着到达的 notifications/cancelled 才找得到它
                _CALLS[req.get("id")] = Call(((req.get("params") or {}).get("_meta") or {}).get("progressToken"))
                threading.Thread(target=handle, args=(req,), daemon=True).start()
            else:
                handle(req)
    finally:
        APP.stop()  # 先停 app-server、关掉 stderr 句柄，cleanup 才删得掉 stderr 文件
        STATE.cleanup()
        log("退出")


if __name__ == "__main__":
    main()
