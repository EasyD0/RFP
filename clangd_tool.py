# 利用clangd 工具定位引用
import atexit
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote
from MyPyLib.LogSet import logSetup
from MyPyLib.Common import elapse

logger = logSetup(__name__)

Clangd_EXE = r"c:\msys64\mingw64\bin\clangd.exe"


# ============================================================
# 路径 / URI 互转
# ============================================================

def path_to_file_uri(path: str | Path) -> str:
    """
    磁盘路径转换为链接路径
    """
    path = Path(path).absolute().as_posix()
    if len(path) >= 2 and path[1] == ":":
        # window 路径
        return "file:///" + quote(path[0] + ":" + path[2:])
    # Unix路径
    return "file://" + quote(path)


def file_url_to_path(url: str) -> Path:
    """
    将 file:// URI 转换回本地文件系统路径 (Path 对象)。
    """
    if not url.startswith("file://"):
        raise ValueError(f"Invalid file URI: {url}")
    path_part = url[7:]
    path_part = unquote(path_part)
    if len(path_part) >= 3 and path_part[0] == "/" and path_part[2] == ":":
        drive_letter = path_part[1]
        remaining_path = path_part[3:]
        windows_path = f"{drive_letter}:{remaining_path}"
        return Path(windows_path)
    return Path(path_part)


# ============================================================
# LSP 底层收发
# ============================================================

def lsp_send(proc, msg: dict, lock=None):
    """
    发送 LSP 消息
    :param proc: clangd 进程对象
    :param msg: LSP 消息字典，包含 id、method、params 等字段
    :param lock: 可选线程锁。reader 线程应答 server->client 请求与主线程发请求
                 会并发写 stdin，需要同一把锁串行化，避免消息头/体交错。
    """

    def _do():
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        proc.stdin.write(header)
        proc.stdin.write(data)
        proc.stdin.flush()

    if lock is None:
        _do()
    else:
        with lock:
            _do()


def lsp_read_responses(
    proc: subprocess.Popen,
    resp_q: "queue.Queue",
    notify_q: "queue.Queue",
    pending_ids: set | None = None,
    pending_lock: threading.Lock | None = None,
    send_lock: threading.Lock | None = None,
):
    """
    读 LSP 响应 / 通知：解析 Content-Length + body

    消息分三类：
    - 带 "id" 且该 id 是我们发过请求的 id -> 对我们请求的响应，放入 resp_q；
    - 带 "id" 但 id 不是我们发过的 -> clangd 主动发给我们的请求（server->client），
      必须按 LSP 规范立即应答（例如 window/workDoneProgress/create），
      否则 clangd 会一直等这个应答、不再处理后续客户端请求，造成死锁；
    - 不带 "id" 的通知（比如 $/progress、publishDiagnostics）-> 放入 notify_q。
    """
    while True:
        header_lines = []
        line = b""
        while True:
            line = proc.stdout.readline()
            if not line:
                return
            if line in (b"\r\n", b"\n"):
                break
            header_lines.append(line)
        content_length = None
        for hl in header_lines:
            s = hl.decode("ascii", errors="ignore").strip()
            if s.lower().startswith("content-length:"):
                content_length = int(s.split(":")[1].strip())
        if content_length is None:
            continue
        body = proc.stdout.read(content_length)
        if not body:
            continue
        try:
            msg = json.loads(body.decode("utf-8"))
        except Exception:
            continue

        if "id" not in msg:
            notify_q.put(msg)
            continue

        mid = msg.get("id")
        if pending_ids is None or pending_lock is None:
            # 无会话上下文时保持旧行为：一律当响应
            resp_q.put(msg)
            continue
        with pending_lock:
            is_response = mid in pending_ids
        if is_response:
            resp_q.put(msg)
        else:
            logger.debug("[clangd->client request] method=%s id=%s -> reply null", msg.get("method"), mid)
            lsp_send(proc, {"jsonrpc": "2.0", "id": mid, "result": None}, lock=send_lock)


def lsp_drain_stderr(proc: subprocess.Popen):
    """
    持续读取并丢弃/记录 stderr。
    不消费 stderr 会导致管道缓冲区写满、clangd 阻塞在写日志上，
    进而 stdout 也不再产生响应，表现为"卡住直到超时"。
    """
    try:
        for raw_line in iter(proc.stderr.readline, b""):
            if not raw_line:
                break
            logger.debug("[clangd stderr] %s", raw_line.decode(errors="ignore").rstrip())
    except Exception:
        pass


def build_text_document_didOpen(text: str, file_path: str | Path):
    """
    构建 textDocument/didOpen 请求消息
    :param text: 文本内容
    :param file_path: 文件路径
    :return: textDocument/didOpen 请求消息
    """
    return {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_file_uri(file_path),
                "languageId": "cpp",
                "version": 1,
                "text": text,
            }
        },
    }


def lsp_request(
    proc: subprocess.Popen,
    resp_q: "queue.Queue",
    msg: dict,
    timeout=1000,
    pending_ids: set | None = None,
    pending_lock: threading.Lock | None = None,
    send_lock: threading.Lock | None = None,
):
    """
    发送 LSP 请求并等待响应
    :param proc: clangd 进程对象
    :param resp_q: 响应队列
    :param msg: LSP 请求消息
    :param timeout: 超时时间（秒）
    :param pending_ids: 本会话当前已发出、等待响应的请求 id 集合，reader 线程据此
                        区分"响应"与"server->client 请求"
    :param pending_lock: pending_ids 的锁
    :param send_lock: 写 stdin 的锁
    :return: LSP 响应消息
    """
    req_id = msg["id"]
    if pending_ids is not None and pending_lock is not None:
        with pending_lock:
            pending_ids.add(req_id)
    try:
        lsp_send(proc, msg, lock=send_lock)
        t0 = time.time()
        while True:
            if time.time() - t0 > timeout:
                raise TimeoutError(f"LSP request id={req_id} timeout")
            try:
                r = resp_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if r.get("id") == req_id:
                return r
            # 理论上 resp_q 里只会有本会话 id 对应的响应，这里兜底忽略不匹配的
    finally:
        if pending_ids is not None and pending_lock is not None:
            with pending_lock:
                pending_ids.discard(req_id)


def wait_for_background_index(
    notify_q: "queue.Queue",
    idle_timeout: float = 20.0,
    overall_timeout: float = 3600.0,
    first_message_timeout: float = 30.0,
    poll_interval: float = 0.2,
):
    """
    等待 clangd 后台索引完成，再返回。

    终止条件（谁先满足就退出）：
      1. 收到了 indexing 相关 token 的 "end" 事件 —— 正常完成；
      2. 已经收到过至少一次进度通知，但连续 idle_timeout 秒都没有新通知
         —— 认为索引已经稳定；
      3. 一次进度通知都没收到过，且超过了 first_message_timeout ——
         说明 clangd 不会发进度通知（版本不支持，或索引早已建好磁盘缓存、
         这次启动无事可做），直接放弃等待、马上去查询，避免死等到
         overall_timeout。

    注意：所有时间判断都使用"进入循环迭代时"的新鲜 now，且 first_message_timeout
    不再依赖 queue.Empty 分支触发。旧实现把 now 取在 notify_q.get(timeout=idle_timeout)
    阻塞之前，导致 first_message_timeout 被滞后整整一个 idle_timeout（30s 变 60s）；
    且非 $/progress 通知不断时永不进入 Empty 分支、只能等 overall_timeout（1 小时）。
    """
    seen_progress = False
    active_tokens = set()
    t_start = time.time()
    last_progress = None

    while True:
        now = time.time()

        if now - t_start > overall_timeout:
            logger.warning("等待后台索引完成超过 overall_timeout=%ss，放弃等待，直接继续查询", overall_timeout)
            return False

        if not seen_progress and now - t_start > first_message_timeout:
            logger.warning(
                "等待 %ss 仍未收到任何 $/progress 索引通知，不再等待，直接继续查询",
                first_message_timeout,
            )
            return True

        if seen_progress and last_progress is not None and now - last_progress > idle_timeout:
            logger.info("索引通知已静默 %ss，认为后台索引已完成（或已停滞）", idle_timeout)
            return True

        try:
            msg = notify_q.get(timeout=poll_interval)
        except queue.Empty:
            continue

        method = msg.get("method")
        if method != "$/progress":
            continue

        params = msg.get("params", {})
        token = params.get("token")
        value = params.get("value", {})
        kind = value.get("kind")
        title = value.get("title", "")
        percentage = value.get("percentage")

        seen_progress = True
        last_progress = time.time()
        logger.info("[index progress] token=%s kind=%s title=%s pct=%s", token, kind, title, percentage)

        if kind == "begin":
            active_tokens.add(token)
        elif kind == "end":
            active_tokens.discard(token)
            if not active_tokens:
                logger.info("所有索引进度 token 均已结束，后台索引完成")
                return True


# ============================================================
# 长驻 clangd 会话
#
# 重构要点：以前每次调用 find_references 都会重新起一个 clangd 进程、
# 重新等一次索引（哪怕只是等 first_message_timeout 那 20~30s）。
# 现在把"进程 + 是否已确认索引完成"绑定成一个 ClangdSession 对象，
# 用全局字典按 (project_dir, compile_dir, clangd_exe) 缓存 session：
#   - 索引等待只在该 session 第一次被查询时发生一次；
#   - session._index_ready 这个标记位一旦为 True，
#     后续同一个 session 上的所有查询都会直接跳过等待。
# ============================================================

class ClangdSession:
    def __init__(self, project_dir: str | Path, compile_dir: str | Path, clangd_exe: str = Clangd_EXE):
        self.project_dir = Path(project_dir).absolute().as_posix()
        self.compile_dir = Path(compile_dir).absolute().as_posix()
        self.clangd_exe = clangd_exe

        self.proc = subprocess.Popen(
            [
                clangd_exe,
                "--background-index",
                f"--compile-commands-dir={self.compile_dir}",
                "--log=verbose",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        # LSP 响应队列
        self.resp_q: "queue.Queue" = queue.Queue()
        # LSP 通知队列
        self.notify_q: "queue.Queue" = queue.Queue()
        # 写 stdin 的锁：主线程发请求与 reader 线程应答 server->client 请求会并发写
        self._write_lock = threading.Lock()
        # 当前已发出、等待响应的请求 id 集合（reader 据此区分响应与 server->client 请求）
        self._pending_ids: set = set()
        self._pending_lock = threading.Lock()
        # LSP 响应读取线程
        self.reader = threading.Thread(
            target=lsp_read_responses,
            args=(
                self.proc,
                self.resp_q,
                self.notify_q,
                self._pending_ids,
                self._pending_lock,
                self._write_lock,
            ),
            daemon=True,
        )
        self.reader.start()
        # LSP 进程stderr读取线程
        self.stderr_drainer = threading.Thread(
            target=lsp_drain_stderr, args=(self.proc,), daemon=True
        )
        self.stderr_drainer.start()

        # id 分配锁
        self._id_lock = threading.Lock()
        self._next_id = 1
        # 已打开的文件集合
        self._opened_files: set[str] = set()

        # 关键的全局（会话级）标记：索引是否已经确认处理过（完成 / 放弃等待）。
        # 一旦置 True，本 session 生命周期内不会再进入 wait_for_background_index。
        self._index_ready = False
        self._index_ready_lock = threading.Lock()

        # 初始化 clangd 会话, 发送 initialize 请求和 initialized 通知, 让它立即开始索引代码
        self._initialize()

    def _alloc_id(self) -> int:
        """
        分配一个唯一的请求ID
        :return: 唯一的请求ID
        """
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _initialize(self):
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "processId": None,
                "rootPath": self.project_dir,
                "rootUri": path_to_file_uri(self.project_dir),
                "capabilities": {
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "references": {"dynamicRegistration": False},
                        "documentSymbol": {"dynamicRegistration": False},
                    },
                },
            },
        }
        lsp_request(
            self.proc,
            self.resp_q,
            init_req,
            timeout=60,
            pending_ids=self._pending_ids,
            pending_lock=self._pending_lock,
            send_lock=self._write_lock,
        )
        lsp_send(
            self.proc,
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            lock=self._write_lock,
        )

    def is_alive(self) -> bool:
        """
        检查 clangd 进程是否存活
        :return: 如果进程存活则返回 True，否则返回 False
        """
        return self.proc.poll() is None

    def ensure_index_ready(
        self,
        idle_timeout: float = 20.0,
        overall_timeout: float = 3600.0,
        first_message_timeout: float = 30.0,
    ) -> bool:
        """
        确保索引已经处理过一次。已经确认过的话直接返回，不再重新等待。
        """
        with self._index_ready_lock:
            if self._index_ready:
                logger.info("索引在本会话（pid=%s）中已确认完成，跳过等待", self.proc.pid)
                return True
            finished = wait_for_background_index(
                self.notify_q,
                idle_timeout=idle_timeout,
                overall_timeout=overall_timeout,
                first_message_timeout=first_message_timeout,
            )
            # 无论是正常收到 end、静默判定完成，还是放弃等待，
            # 都标记为"已处理过"，本 session 后续查询不再重复等待。
            self._index_ready = True
            return finished

    def open_file(self, target_file: str | Path):
        """
        打开文件并发送 textDocument/didOpen 通知
        :param target_file: 文件路径
        """
        target_file = Path(target_file).absolute().as_posix()
        if target_file in self._opened_files:
            return
        with open(target_file, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        lsp_send(self.proc, build_text_document_didOpen(text, target_file), lock=self._write_lock)
        self._opened_files.add(target_file)

    def find_references(
        self,
        target_file: str | Path,
        line_0based: int,
        character_0based: int,
        include_declaration: bool = False,
        wait_index: bool = True,
        index_idle_timeout: float = 20.0,
        index_overall_timeout: float = 3600.0,
        index_first_message_timeout: float = 30.0,
        request_timeout: float = 1200.0,
    ):
        target_file = Path(target_file).absolute().as_posix()
        self.open_file(target_file)

        if wait_index:
            self.ensure_index_ready(
                idle_timeout=index_idle_timeout,
                overall_timeout=index_overall_timeout,
                first_message_timeout=index_first_message_timeout,
            )

        req_id = self._alloc_id()
        refs_req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "textDocument/references",
            "params": {
                "textDocument": {"uri": path_to_file_uri(target_file)},
                "position": {"line": line_0based, "character": character_0based},
                "context": {"includeDeclaration": include_declaration},
            },
        }
        refs_resp = lsp_request(
            self.proc,
            self.resp_q,
            refs_req,
            timeout=request_timeout,
            pending_ids=self._pending_ids,
            pending_lock=self._pending_lock,
            send_lock=self._write_lock,
        )

        result = refs_resp.get("result")
        locations = result or []
        out = []
        for loc in locations:
            uri = loc.get("uri")
            r = loc.get("range", {})
            start = r.get("start", {})
            out.append(
                {"uri": file_url_to_path(uri), "start": start, "end": r.get("end", {})}
            )
        return out

    def close(self):
        """
        关闭 clangd 进程
        """
        try:
            self.proc.terminate()
        except Exception:
            pass


# ---- 全局 session 缓存：同一个 (project_dir, compile_dir, clangd_exe) 只保留一个长驻进程 ----
_SESSIONS: dict[str, ClangdSession] = {}
_SESSIONS_LOCK = threading.Lock()


def _session_key(project_dir, compile_dir, clangd_exe) -> str:
    return "|".join(
        [
            Path(project_dir).absolute().as_posix(),
            Path(compile_dir).absolute().as_posix(),
            clangd_exe,
        ]
    )


def get_session(project_dir: str | Path, compile_dir: str | Path, clangd_exe: str = Clangd_EXE,) -> ClangdSession:
    """
    获取一个 clangd 会话
    :param project_dir: 项目目录
    :param compile_dir: 编译目录
    :param clangd_exe: clangd 可执行文件路径
    :return: clangd 会话对象
    """
    key = _session_key(project_dir, compile_dir, clangd_exe)
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
        if session is None or not session.is_alive():
            if session is not None:
                logger.warning("旧的 clangd 会话(pid=%s)已退出，重新启动一个", session.proc.pid)
            session = ClangdSession(project_dir, compile_dir, clangd_exe)
            _SESSIONS[key] = session
        return session


def close_all_sessions():
    """
    关闭所有 clangd 会话
    """
    with _SESSIONS_LOCK:
        for session in _SESSIONS.values():
            session.close()
        _SESSIONS.clear()


atexit.register(close_all_sessions)


# ============================================================
# 对外接口：函数签名保持不变，内部改为复用长驻 session
# ============================================================

@elapse
def find_references(
    project_dir: str | Path,
    compile_dir: str | Path,
    target_file: str | Path,
    line_0based: int,
    character_0based: int,
    include_declaration: bool = False,
    clangd_exe: str = Clangd_EXE,
    wait_index: bool = True,
    index_idle_timeout: float = 20.0,
    index_overall_timeout: float = 3600.0,
    request_timeout: float = 1200.0,
):
    """
    查找一个函数符号的所有引用。

    内部改为使用按 (project_dir, compile_dir, clangd_exe) 缓存的长驻
    ClangdSession：
      - 同一组参数只会启动一个 clangd 进程，重复调用会复用它；
      - 索引只在该 session 第一次被查询时等待一次
        （session._index_ready 标记），之后的调用直接查询，
        不再重新进入 20~30s 的等待。

    如果确定要强制新起一个干净的 clangd 进程重新走一遍索引等待，
    调用前先 close_all_sessions()，或直接用 ClangdSession(...) 手动创建。
    """
    session = get_session(project_dir, compile_dir, clangd_exe)
    return session.find_references(
        target_file=target_file,
        line_0based=line_0based,
        character_0based=character_0based,
        include_declaration=include_declaration,
        wait_index=wait_index,
        index_idle_timeout=index_idle_timeout,
        index_overall_timeout=index_overall_timeout,
        request_timeout=request_timeout,
    )


def kill_all_clangd_processes():
    """
    终止系统中所有正在运行的 clangd 进程.

    用途: multiprocessing.Pool 退出时, worker 进程可能被 terminate(),
    其内部启动的 clangd 子进程会变孤儿. 本函数在 Pool 退出后兜底清理.
    正常退出流程应优先调用 close_all_sessions()，这个函数只是兜底。

    注意: 会杀掉系统里所有名为 clangd 的进程.
    """
    with _SESSIONS_LOCK:
        _SESSIONS.clear()

    if sys.platform == "win32":
        cmd = ["taskkill", "/F", "/T", "/IM", "clangd.exe"]
    else:
        cmd = ["pkill", "-9", "-f", "clangd"]

    try:
        subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError:
        pass


def get_ref_code(ref_code_locaitons: list[dict]) -> list[str]:
    """
    从定位的引用位置中提取代码行
    :param ref_code_locaitons: 引用位置列表，每个元素是一个字典，包含 uri 和 start 字段
    :return: 代码行列表
    """
    res = []
    for loc in ref_code_locaitons:
        uri = loc["uri"]
        file_path = file_url_to_path(uri)
        start_line = loc["start"].get("line", "")
        if not start_line:
            continue
        start_line = int(start_line)

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.readlines()
            if start_line < 0 or start_line >= len(text):
                logger.error(f"引用{file_path}:{start_line}位置超出范围")
                continue
            res.append(text[start_line])
    return res


if __name__ == "__main__":
    Clangd_exe = r"D:\Program Files\LLVM\bin\clangd.exe"
    project_dir = r"D:\Code\Python\ReduceFalsePositives\test_proj"
    compile_commands_json = Path(
        r"D:\Code\Python\ReduceFalsePositives\test_proj\compile_commands.json"
    )
    target_file = r"D:\Code\Python\ReduceFalsePositives\test_proj\a.c"

    # 第一次调用：会等一次索引（正常完成 / 静默判定 / 放弃等待 三选一）
    refs1 = find_references(
        clangd_exe=Clangd_exe,
        project_dir=project_dir,
        compile_dir=compile_commands_json.parent,
        target_file=target_file,
        line_0based=0,
        character_0based=5,
        include_declaration=False,
    )
    for x in refs1:
        print(x["uri"], x["start"].get("line"), x["start"].get("character"))

    # 第二次调用（同一个 project_dir/compile_dir）：
    # 复用同一个 clangd 进程，session._index_ready 已经是 True，
    # 直接查询，不会再等 20~30s。
    refs2 = find_references(
        clangd_exe=Clangd_exe,
        project_dir=project_dir,
        compile_dir=compile_commands_json.parent,
        target_file=target_file,
        line_0based=10,
        character_0based=3,
        include_declaration=False,
    )
    for x in refs2:
        print(x["uri"], x["start"].get("line"), x["start"].get("character"))

    close_all_sessions()

