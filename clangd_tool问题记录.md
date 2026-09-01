# clangd_tool.py 问题记录

记录 2026-09-01 排查"大项目建立索引完成后, 请求查找函数引用会响应超时"发现的问题及修复。

## 问题 1(主因): `bufsize=0` + 一次性 read 导致大响应被截断, LSP 流错位

### 现象

- 小项目(test_proj / test_clangd)一切正常;
- 大项目上 `wait_for_background_index` 等索引完成后, 第一条
  `textDocument/references` 请求干等满 `request_timeout`(默认 1200s)后抛
  `TimeoutError`, 之后该会话基本废掉。

### 原因

`ClangdSession.__init__` 里 `subprocess.Popen(..., bufsize=0)` 使
`proc.stdout` 成为裸的 `io.FileIO`。**裸流的 `read(n)` 只做一次系统调用,
管道缓冲区里有多少就返回多少, 不保证读满 n 字节**。

而 `lsp_read_responses` 读消息体用的是:

```python
body = clangd_proc.stdout.read(content_length)   # 可能只读到一小块!
```

管道缓冲区通常只有几十 KB。大项目里索引完成后 references 的响应体往往有
成百上千条引用位置(数 MB JSON), clangd 分多次写入管道, 这边一次 read
只读到头一小块 → `json.loads` 失败 → `continue` → **消息流错位**: 剩下的
body 字节被当成下一个"头部"解析, 即使偶尔重新对齐, 这条响应也已经丢了。

于是 `lsp_request` 永远等不到匹配 id 的响应, 干等满 `request_timeout` 后
超时。

**为什么小项目没事**: `initialize`、`publishDiagnostics` 等消息都很小,
一次系统调用就能读全; 只有索引完成后的 references 响应才大到需要跨多次
管道写入, 所以恰好表现为"建完索引后第一次查引用就超时"。

### 修复

- 新增 `_read_exact(fp, n)`: 循环读满 n 字节, EOF 返回 None;
  `lsp_read_responses` 读消息体改用它。
- `Popen` 去掉 `bufsize=0`, 恢复默认缓冲(Stdin 变 BufferedWriter,
  `lsp_send` 里的 `flush()` 不受影响)。

### 验证方法

在旧的 `except` 分支里打印 `content_length` 和 `len(body)`, 若超时时出现
`len(body) < content_length` 即为此原因。

## 问题 2: 共享 resp_q 上不匹配的响应被直接丢弃

### 原因

`resp_q` 是会话级共享队列, `lsp_request` 循环里 id 不匹配的响应直接丢弃:

```python
if r.get("id") == req_id:
    return r
# 不匹配的 -> 丢弃
```

同一个 session 上两个线程并发查询时, A 会把 B 的响应拿走扔掉, B 必然
超时; 超时请求的"迟到响应"也会被当垃圾丢掉。低并发没现象, 并发一上来
就是随机超时。

### 修复

- `ClangdSession` 改用**响应注册表** `_resp_registry: {请求id: 专用Queue}`,
  reader 线程按 id 精确投递到各请求自己的队列, 并发请求互不干扰;
- `lsp_request` 新增 `resp_registry / registry_lock` 参数走注册表路径;
  旧共享队列路径(兼容 test_clangd.py 等旧调用方)读到别人的响应时放回队尾
  (带 `method` 的 server->client 请求则应答掉, 防止 clangd 卡死),
  不再静默丢弃。

## 问题 3: 客户端 id 与 clangd 的 server->client 请求 id 可能碰撞

### 原因

客户端请求 id 与 clangd 发来的 `window/workDoneProgress/create` 等请求的
id 都是整数, 且旧 reader 用"是否在 pending_ids 里"来区分响应和请求。
如果 clangd 的请求 id 恰好等于某个 pending 请求的 id, reader 会把 clangd
的**请求**当成**响应**投进队列, 而真正的响应反而被回了个 `result: null`,
导致拿到空结果。

### 修复

- `_alloc_id` 改为返回带前缀的字符串 id(`pyclient-N`), 两端 id 永远
  不会碰撞;
- reader 改用 JSON-RPC 规范判别: 带 `id` 且带 `method` 的是
  server->client 请求, 只带 `id` 的是响应, 与 id 取值无关。

## 顺带的行为变化

- 请求超时后迟到的响应: 旧代码会被当成 server->client 请求回给 clangd
  一个假响应; 新代码按"无 method 的孤儿响应"直接记日志丢弃。
- `clangd_service.py` 旧代码以 2 个参数调用 `lsp_read_responses`, 原本会
  因缺少 `notify_q` 参数而崩溃; 现在 `notify_q` 之后的参数都有默认值,
  该调用方式恢复可用(走共享 resp_q 的旧行为)。

## 遗留的已知限制(未改, 属设计取舍)

- `wait_for_background_index` 的"静默 20s 判完成"和"30s 没收到首条
  progress 就放弃"两个提前退出口, 在大项目冷启动时可能判早了。这不会
  导致超时(clangd 不会因索引未完成而挂起请求), 但第一次查询可能拿到
  不完整的引用结果。若要更稳, 可把 `first_message_timeout` 调大, 或在
  放弃等待后强制对目标文件做一次 preamble 就绪等待。

## 修复涉及的文件

- `clangd_tool.py`:
  - 新增 `_read_exact()`;
  - `lsp_read_responses()`: `_read_exact` 读消息体、按 JSON-RPC 规范分类、
    支持响应注册表;
  - `lsp_request()`: 新增注册表路径, 旧路径不再丢弃他人响应;
  - `ClangdSession`: `bufsize=0` 移除、`_resp_registry` 替代
    `_pending_ids`、`_alloc_id` 返回 `pyclient-N` 字符串 id。
