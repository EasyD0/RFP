# 持久化 clangd 服务: 复用单个 clangd 进程处理多次 LSP 请求
import atexit
import subprocess
import threading
import queue
import time
from pathlib import Path

from clangd_tool import (
    Clangd_EXE,
    lsp_send,
    lsp_read_responses,
    path_to_file_uri,
    build_text_document_didOpen,
)


class ClangdService:
    """
    持久化的 clangd LSP 服务, 复用单个 clangd 进程.

    背景:
        clangd_tool.find_references 每次调用都会 subprocess.Popen 启动新 clangd,
        完成后 terminate. clangd 启动开销大 (加载索引、解析 compile_commands),
        频繁调用时浪费严重.

        本类持有长期运行的 clangd 进程, find_references 多次调用复用同一进程,
        同一文件只 didOpen 一次, 后续直接 references.

    线程安全:
        内部用锁串行化 LSP 请求. LSP 是 request-response 模式,
        同一时刻只能处理一个请求 (通过 id 配对响应).

    多进程使用:
        本服务设计为 "每个 worker 进程持有一个实例", 而非跨 worker 共享.
        原因: LSP 协议有状态 (didOpen 后才能 references), 跨进程共享需要额外
        IPC 层和状态协调, 复杂度收益不划算. clangd 官方设计也是一个客户端
        一个 clangd 实例 (VSCode 等 IDE 均如此).

        在 multiprocessing 场景下, 推荐通过 CodeContext.__setstate__ 在每个
        worker 进程里创建独立的 ClangdService, worker 内所有调用复用该实例,
        worker 退出时自动关闭. 若 worker 被强杀 (terminate), 用
        clangd_tool.kill_all_clangd_processes() 兜底清理.

    资源管理:
        - with 语句: 退出时自动 close()
        - atexit: 进程正常退出时兜底关闭
        - __del__: GC 时兜底 (不保证一定调用)
        - 被 terminate 强杀时: 依赖 kill_all_clangd_processes() 兜底

    用法:
        # 单进程
        with ClangdService(Clangd_EXE, project_dir) as svc:
            refs1 = svc.find_references(file1, line, col)
            refs2 = svc.find_references(file2, line, col)

        # 多进程 worker 内
        svc = ClangdService(Clangd_EXE, project_dir)
        try:
            refs = svc.find_references(file, line, col)
        finally:
            svc.close()
    """

    def __init__(self, clangd_exe: str = Clangd_EXE, project_dir: str | Path = ""):
        self.clangd_exe = clangd_exe
        self.project_dir = str(Path(project_dir).resolve()) if project_dir else ""
        self.proc: subprocess.Popen | None = None
        self.out_q: queue.Queue | None = None
        self.reader: threading.Thread | None = None
        self._req_id = 0
        self._opened_files: set[str] = set()  # 已 didOpen 的文件 URI
        self._lock = threading.Lock()  # 串行化 LSP 请求
        self._closed = False
        self._initialize()
        atexit.register(self.close)

    def _initialize(self):
        """启动 clangd 进程并发送 initialize 请求"""
        args = [self.clangd_exe, "--background-index"]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.out_q = queue.Queue()
        self.reader = threading.Thread(
            target=lsp_read_responses, args=(self.proc, self.out_q), daemon=True
        )
        self.reader.start()

        init_req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "processId": None,
                "rootPath": self.project_dir,
                "rootUri": (
                    path_to_file_uri(self.project_dir) if self.project_dir else None
                ),
                "capabilities": {
                    "textDocument": {
                        "references": {"dynamicRegistration": False},
                        "documentSymbol": {"dynamicRegistration": False},
                    }
                },
            },
        }
        self._request(init_req)
        # initialized notification (无 id, 无响应)
        lsp_send(self.proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def _next_id(self) -> int:
        """生成递增的 LSP 请求 id"""
        self._req_id += 1
        return self._req_id

    def _request(self, msg: dict, timeout: int = 30) -> dict:
        """
        发送带 id 的请求并等待同 id 的响应.
        调用方需持有 self._lock (确保 id 配对不被其他请求插入).
        """
        req_id = msg["id"]
        lsp_send(self.proc, msg)
        t0 = time.time()
        while True:
            if time.time() - t0 > timeout:
                raise TimeoutError(f"LSP request id={req_id} timeout")
            try:
                r = self.out_q.get(timeout=0.1)
            except queue.Empty:
                continue
            # LSP 响应通过 id 配对; 跳过其他 id 的响应 (如异步通知)
            if r.get("id") == req_id:
                return r

    def find_references(
        self,
        target_file: str | Path,
        line_0based: int,
        character_0based: int,
        include_declaration: bool = False,
    ) -> list[dict]:
        """
        查找符号的所有引用, 复用已启动的 clangd.

        同一文件只 didOpen 一次 (记录在 _opened_files 中), 后续调用直接发 references.
        若文件内容变化, 需先调 did_close() 清除记录, 下次会重新 didOpen.

        :param target_file: 目标文件路径
        :param line_0based: 行号 (0-based, LSP 规范)
        :param character_0based: 列号 (0-based)
        :param include_declaration: 是否包含声明处
        :return: list[{"uri": str, "start": {...}, "end": {...}}]
        """
        if self._closed:
            raise RuntimeError("ClangdService 已关闭")

        target_file = str(Path(target_file).resolve())
        file_uri = path_to_file_uri(target_file)

        with self._lock:
            # didOpen (仅首次)
            if file_uri not in self._opened_files:
                did_open = build_text_document_didOpen(target_file)
                lsp_send(self.proc, did_open)
                self._opened_files.add(file_uri)

            # references
            refs_req = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "textDocument/references",
                "params": {
                    "textDocument": {"uri": file_uri},
                    "position": {
                        "line": line_0based,
                        "character": character_0based,
                    },
                    "context": {"includeDeclaration": include_declaration},
                },
            }
            refs_resp = self._request(refs_req)

        result = refs_resp.get("result")
        locations = result or []
        out = []
        for loc in locations:
            uri = loc.get("uri")
            r = loc.get("range", {})
            start = r.get("start", {})
            out.append({"uri": uri, "start": start, "end": r.get("end", {})})
        return out

    def did_close(self, target_file: str | Path):
        """
        通知 clangd 关闭某文件, 清除内部 didOpen 记录.
        下次 find_references 该文件时会重新 didOpen (读取最新内容).
        用于文件内容被外部修改后强制刷新.
        """
        if self._closed or self.proc is None:
            return
        target_file = str(Path(target_file).resolve())
        file_uri = path_to_file_uri(target_file)
        with self._lock:
            if file_uri in self._opened_files:
                close_req = {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didClose",
                    "params": {"textDocument": {"uri": file_uri}},
                }
                lsp_send(self.proc, close_req)
                self._opened_files.discard(file_uri)

    def close(self):
        """
        关闭 clangd 进程, 释放资源.
        幂等: 多次调用安全.
        先 terminate (SIGTERM), 超时则 kill (SIGKILL).
        """
        if self._closed:
            return
        self._closed = True

        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                # 进程已退出或其他异常, 忽略
                pass
            finally:
                self.proc = None
                self.reader = None
                self.out_q = None
                self._opened_files.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __del__(self):
        # GC 兜底, 不保证一定调用; 真正的兜底是 atexit + kill_all_clangd_processes
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    # 单进程使用示例: 两次调用复用同一个 clangd 进程
    project_dir = r"D:\Code\Python\ReduceFalsePositives\test_proj"
    target_file = r"D:\Code\Python\ReduceFalsePositives\test_proj\a.c"

    with ClangdService(Clangd_EXE, project_dir) as svc:
        # 第一次调用: 启动 clangd + didOpen + references
        refs1 = svc.find_references(target_file, line_0based=1, character_0based=5)
        print(f"第一次调用: 找到 {len(refs1)} 处引用")

        # 第二次调用: 复用 clangd, 直接 references (同文件不再 didOpen)
        refs2 = svc.find_references(target_file, line_0based=2, character_0based=5)
        print(f"第二次调用: 找到 {len(refs2)} 处引用")

    # with 退出时自动 close(), clangd 进程被清理
