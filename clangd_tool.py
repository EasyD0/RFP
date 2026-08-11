# 利用clangd 工具定位引用
import os, json, subprocess, threading, queue
from pathlib import Path
from urllib.parse import quote, unquote

Clangd_EXE = r"c:\msys64\mingw64\bin\clangd.exe"

def path_to_file_uri(path: str | Path) -> str:
    # Windows: 绝对路径如 C:\a\b.cpp -> file:///C:/a/b.cpp
    # Linux/macOS: /a/b.cpp -> file:///a/b.cpp
    path = Path(path).absolute().as_posix()
    if len(path) >= 2 and path[1] == ":":
        # window 路径
        return "file:///" + quote(path[0] + ":" + path[2:])

    # Unix路径
    return "file://" + quote(path)


def file_url_to_path(url: str) -> Path:
    """
    将 file:// URI 转换回本地文件系统路径 (Path 对象)。

    处理逻辑：
    1. 解析 URL 结构。
    2. 处理 URL 编码 (如 %20 转为空格)。
    3. 区分 Windows 和 Unix 路径：
       - Windows: file:///C:/path
       - Unix:    file:///path
    """
    if not url.startswith("file://"):
        raise ValueError(f"Invalid file URI: {url}")

    # 1. 去掉 file:// 前缀
    path_part = url[7:]  # 去掉 "file://"

    # 2. URL 解码 (处理 %20, %E4%B8%AD%E6%96%87 等)
    path_part = unquote(path_part)

    if len(path_part) >= 3 and path_part[0] == "/" and path_part[2] == ":":
        # 判断是否为 Windows 路径
        drive_letter = path_part[1]
        remaining_path = path_part[3:]
        windows_path = f"{drive_letter}:{remaining_path}"
        return Path(windows_path)
    return Path(path_part)


def lsp_send(proc, msg: dict):
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
    proc.stdin.write(header)
    proc.stdin.write(data)
    proc.stdin.flush()


def lsp_read_responses(proc: subprocess.Popen, out_q):
    """
    读 LSP 响应：解析 Content-Length + body
    为了简化：只把整条 response 消息放入队列
    """
    while True:
        # 读到空行前的头
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
            out_q.put(msg)
        except Exception:
            # 忽略不可解析
            pass


def build_text_document_didOpen(text: str, file_path: str | Path):
    return {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": path_to_file_uri(file_path),
                "languageId": "cpp",  # 你也可以按后缀改：c/cpp/objc 等
                "version": 1,
                "text": text,
            }
        },
    }


def lsp_request(proc: subprocess.Popen, out_q, msg: dict, timeout=30):
    req_id = msg["id"]
    lsp_send(proc, msg)
    # 等待同 id 的 response
    import time

    t0 = time.time()
    while True:
        if time.time() - t0 > timeout:
            raise TimeoutError(f"LSP request id={req_id} timeout")
        try:
            r = out_q.get(timeout=0.1)
        except queue.Empty:
            continue
        if r.get("id") == req_id:
            return r


def find_references(
    clangd_exe: str,
    project_dir: str,
    # compile_commands_json: str,
    target_file: str,
    line_0based: int,
    character_0based: int,
    include_declaration: bool = False,
):
    """
    查找一个函数符号的所有引用
    :param clangd_exe:
    :param project_dir:
    :param target_file:
    :param line_0based:
    :param character_0based:
    :param include_declaration:
    :return:
    """
    project_dir = os.path.abspath(project_dir)
    target_file = os.path.abspath(target_file)
    compile_dir = os.path.dirname(os.path.abspath(compile_commands_json))

    # 读目标文件全文（didOpen 需要 text）
    with open(target_file, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    proc = subprocess.Popen(
        [
            clangd_exe,
            "--background-index",
            # f"--compile-commands-dir={compile_dir}",
            # 可以加："--log=verbose"
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    out_q = queue.Queue()
    reader = threading.Thread(
        target=lsp_read_responses, args=(proc, out_q), daemon=True
    )
    reader.start()

    # 1) initialize
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "processId": None,
            "rootPath": project_dir,
            "rootUri": path_to_file_uri(project_dir),
            "capabilities": {
                "textDocument": {
                    "references": {"dynamicRegistration": False},
                    "documentSymbol": {"dynamicRegistration": False},
                }
            },
        },
    }
    init_resp = lsp_request(proc, out_q, init_req)
    # 2) initialized notification
    lsp_send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})

    # 3) didOpen（让 clangd 知道文件内容与当前版本）
    did_open = build_text_document_didOpen(text, target_file)
    lsp_send(proc, did_open)

    # 4) references
    req_id = 2
    refs_req = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "textDocument/references",
        "params": {
            "textDocument": {"uri": path_to_file_uri(target_file)},
            "position": {
                "line": line_0based,  # LSP line: 0-based
                "character": character_0based,  # 由你提供：assume 0-based
            },
            "context": {"includeDeclaration": include_declaration},
        },
    }
    refs_resp = lsp_request(proc, out_q, refs_req)

    # 5) 解析结果
    result = refs_resp.get("result")
    # result 可能为 None 或 Location[]
    locations = result or []
    out = []
    for loc in locations:
        uri = loc.get("uri")
        r = loc.get("range", {})
        start = r.get("start", {})
        out.append({"uri": uri, "start": start, "end": r.get("end", {})})

    # （可选）关闭进程
    proc.terminate()
    return out


def get_ref_code(ref_code_locaitons: list[dict]) -> list[str]:
    """
    从定位的引用位置中提取代码
    :param ref_code_locaitons:
    :return:
    """
    res = []
    for loc in ref_code_locaitons:
        uri = loc["uri"]
        file_path = file_url_to_path(uri)
        start_line = loc["start"].get("line", "") # 0-based
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
    # 示例：把你的参数填这里
    clangd_exe = r"D:\Program Files\LLVM\bin\clangd.exe"
    project_dir = r"D:\Code\Python\ReduceFalsePositives\test_proj"
    compile_commands_json = r"D:\myproj\build\compile_commands.json"
    target_file = r"D:\Code\Python\ReduceFalsePositives\test_proj\a.c"

    refs = find_references(
        clangd_exe=clangd_exe,
        project_dir=project_dir,
        # compile_commands_json=compile_commands_json,
        target_file=target_file,
        line_0based=1,
        character_0based=5,
        include_declaration=False,
    )
    for x in refs:
        print(x["uri"], x["start"].get("line"), x["start"].get("character"))