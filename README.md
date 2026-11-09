# bale

> 让 AI Agent 的工具输出在进入模型上下文之前自动压缩 —— 大日志不占上下文，原文随时可取回。

一个 [DSH](https://github.com/deepseek-ai) （DeepSeek Harness）插件。当 `pwsh` / `bash` / `web_fetch` / `run_code` 等工具产生超大输出时，本插件在**模型看到之前**把它压缩成结构化摘要，同时把完整原文按内容哈希存到本地，模型需要细节时用 `headroom_retrieve(hash)` 一键取回。

实测数据：24,611 字符的构建日志 → 7,531 字符（**30.6%**，ERROR/WARN 零丢失）；28,544 字符的 grep 结果 → 1,750 字符（**6.1%**）；13,763 字符的 git diff → 2,538 字符（**18.4%**，所有 `-/+` 改动行完整保留）。

---

## 目录

- [为什么需要它](#为什么需要它)
- [架构](#架构)
- [支持的压缩类型](#支持的压缩类型)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [配置参考](#配置参考)
- [HTTP 接口](#http-接口)
- [工作原理](#工作原理)
- [设计决策](#设计决策)
- [已知限制](#已知限制)
- [排障](#排障)
- [卸载](#卸载)
- [依赖精简说明](#依赖精简说明)
- [开发与测试](#开发与测试)
- [许可](#许可)

---

## 为什么需要它

Agent 跑一次 `npm run build`、`pytest -v`、`docker logs`，输出动辄几十万字符。这些内容**99% 是重复的 INFO 行**，但会实实在在烧掉上下文窗口，让真正需要推理的部分没有空间。

现有方案要么不分青红皂白全丢（信息损失），要么全留（上下文爆炸）。bale 走第三条路：

- **压缩有结构的部分** —— 重复日志行、未改动的 diff 上下文、同文件的 grep 命中
- **保留有信息量的部分** —— ERROR/WARN 全留、diff 的 `-/+` 全留、首尾边界完整
- **原文永远可追溯** —— 每次压缩生成 16 位哈希，`headroom_retrieve` 逐字节取回

**核心原则：宁可少压，也不造假。**

---

## 架构

```
工具执行（pwsh / bash / ops_bash / web_fetch / run_code）
  │
  └─ DSH 管线: tools/post-execute 钩子  ← 本插件挂载点
       │
       ├─ 白名单 / 保护名单 / 阈值判定
       │
       └─ POST http://127.0.0.1:8797/compress
            │  （spill 截断场景直接送 spillPath 全文）
            │
            └─ 返回 { kind, hash, text, ratio }
                 │
                 ├─ 原文按字节存 → store/<sha256[:16]>.bin
                 │
                 └─ 替换模型所见 content 为「压缩头 + 摘要」

模型需要细节时 → headroom_retrieve(hash) → 完整原文
```

**双进程设计**：

| 进程 | 职责 | 语言 |
|---|---|---|
| DSH 插件 | 钩子挂载、判定、替换、注册 `headroom_retrieve` 工具 | Node.js (ESM) |
| sidecar | 实际压缩算法 + 原文存储 + HTTP 服务 | Python 3.10–3.14 |

这样拆分的原因：压缩算法（LogCompressor、SmartCrusher 等）来自 `headroom-ai` Python 库，而 DSH 插件跑在 Node 里。sidecar 单进程常驻，压缩器懒加载后常驻内存，避免每次调用都付出冷启动代价。

**fail-open 是铁律**：sidecar 挂了、超时、抛异常 —— 任何环节失败都原样放行，绝不让压缩失败导致工具调用失败。

---

## 支持的压缩类型

插件会自动识别内容类型并选择对应策略（`kind`）：

| kind | 适用内容 | 压缩方式 | 实测 ratio |
|---|---|---|---|
| `log` | 带时间戳的日志（构建、测试、容器） | LogCompressor：按行折叠 + ERROR/WARN 优先保留 | **0.2% – 30%** |
| `json` | JSON 数组 / 对象 | SmartCrusher：字典编码 + 去重 | **12% – 46%** |
| `diff` | git diff / patch | DiffCompressor：折叠未改动上下文，保留所有 `-/+` | **18%** |
| `search` | grep / ripgrep 结果 | SearchCompressor：按文件聚合，保留首尾命中 | **6%** |
| `generic` | 其他结构化大文本（文件列表、堆栈、测试报告） | 行级抽样：头 80 行 + 关键行 + 尾 40 行 | **30% – 41%** |

**`generic` 的关键行提取**会额外扫一遍必须保留的行，避免折叠掉关键信息：

```python
CRITICAL_PATTERNS = [
    (r"^[-+][ \t][^+-]|^[-+][^\s+-]\S{2,}", "diff-change"),  # 仅在 diff 块内生效
    (r"\b(ERROR|FATAL|CRITICAL)\b",              "错误"),
    (r"\b(FAIL|FAILED|FAILURE)\b",               "失败"),
    (r"\b(Traceback|Exception|Error:)\b",        "异常"),
    (r'^\s*at\s+[\w.$]+\(|^\s*File "',          "堆栈帧"),
    (r"^diff --git",                           "文件起始"),
    (r"^@@\s",                                 "hunk 头"),
]
```

实测效果：20 个文件的 git diff，`-old` 20/20、`+new` 20/20 全部保留；400 行混合日志的 8 条 ERROR、15 条 WARN 一条不少。

### 已停用：`text`（语义压缩）

基于 Kompress ONNX 模型（274MB）的语义压缩，理论上最智能，但实测在 Windows + ONNX runtime 环境下：

| 测试样本 | ratio | 耗时 |
|---|---|---|
| 中文 39,152 字符 | **1.000**（完全无效） | 0 ms |
| 英文 41,086 字符 | **1.000** | 13.7 s |
| 英文 70,821 字符 | 0.945（仅 5.5%） | 12.8 s |

**收益接近零，耗时 8-27 秒，经常触发 15 秒超时。** 已在 sidecar 层用 `HEADROOM_DISABLED_KINDS=text` 短路（不是插件层过滤——插件层过滤发生在压缩之后，仍会白算）。

> 如果你的环境能跑动 Kompress，可以设 `HEADROOM_DISABLED_KINDS=""` 重新启用。建议先单独测一下自己环境的实际 ratio 和耗时。

---

## 快速开始

### 前置要求

- DSH（DeepSeek Harness）桌面版或 CLI
- Node.js ≥ 20
- Python 3.10–3.14（sidecar 用，实测 3.12.14 与 3.14.7）
- Windows x64（预置 wheel 仅此平台；macOS / Linux 需按 `tools/repack/README.md` 自行重打包）

### 安装步骤

**1. 获取源码**

```bash
git clone https://github.com/AQYLPZ/bale.git
cd bale
```

**2. 创建 Python 环境并安装 headroom-ai**

```bash
python -m venv venv

# Windows
venv/Scripts/python.exe -m pip install -r requirements.txt
# macOS / Linux
venv/bin/python -m pip install -r requirements.txt
```

`requirements.txt` 使用仓库内重打包的精简 wheel（`vendor/wheels/headroom_ai-0.42.0.post1-*.whl`，base 依赖已从 13 条收缩到 7 条，见「依赖精简说明」）。也可以手动安装：

```bash
venv/Scripts/python.exe -m pip install --find-links ./vendor/wheels "headroom-ai[code]==0.42.0.post1"   # Windows
venv/bin/python -m pip install --find-links ./vendor/wheels "headroom-ai[code]==0.42.0.post1"            # macOS / Linux
```

> 该 wheel 是 `cp310-abi3-win_amd64`，仅适用于 Windows x64。macOS / Linux 需基于对应平台的原始 wheel，调整 `tools/repack/repack.py` 中的 `TAG` / `OUT_NAME` 后重新打包。
>
> 从 0.1.4 起，tgz 已包含 `vendor/wheels/`（约 15 MB，tgz 总计约 14.7 MB）。装完插件后可直接：
> 若需 store 瘦身与监控口径修复，请使用 0.1.5 及以上版本。
>
> ```bash
> venv/Scripts/python.exe -m pip install -r node_modules/bale/requirements.txt   # Windows
> venv/bin/python -m pip install -r node_modules/bale/requirements.txt           # macOS / Linux
> ```
>
> `--find-links` 相对 `requirements.txt` 所在目录解析，无需 `cd`。
> 传递依赖（httpx / pydantic / opentelemetry-api 等）仍需联网从 PyPI 获取。

> 不要装 `[ml]` 额外组 —— 它会把值得注意的依赖树拖进来，且本插件的 text 压缩默认停用。

**3. 打包并安装插件**

```bash
# 在仓库根目录内；npm pack 会按 package.json 的 files 白名单打包
npm pack
# 生成 bale-0.1.5.tgz（内部根目录为 package/，这是 pnpm 能识别的格式）
```

把 tgz 放到 profile 的 `vendor/` 目录，然后在 profile 的 `package.json` 里添加：

```json
{
  "dependencies": {
    "bale": "file:./vendor/bale-0.1.5.tgz"
  },
  "dsh": {
    "profile": {
      "bundles": [ "bale" ]
    }
  }
}
```

执行 `pnpm install`。

> **本项目未发布到 npm**（npm 上的 `bale` 是另一个包，当前 0.2.0），请使用上面的本地 tgz 方式安装。tgz 内已包含 `lib/`、`sidecar/sidecar.py`、`cordis.patch.yml`、`requirements.txt` 和 `vendor/wheels/`；Python 依赖仍需按第 2 步单独安装，安装后对应路径是 `node_modules/bale/requirements.txt`。

**4. 配置绝对路径**

在 profile 的 `cordis.patch.yml` 里加：

```yaml
- id: bale
  name: bale
  config:
    enabled: true
    sidecarPython: '<PROFILE>/.headroom/venv/Scripts/pythonw.exe'
    sidecarScript: '<PROFILE>/node_modules/bale/sidecar/sidecar.py'
    storeDir: '<PROFILE>/.headroom/store'
    wsDir: '<PROFILE>/.headroom/headroom_ws'
    logFile: '<PROFILE>/.headroom/logs/headroom-auto.log'
```

> **为什么必须写绝对路径**：插件默认从 `lib/` 上两级推导 venv 位置，这在真实目录（非 symlink）下会算错。算错的后果是 sidecar 拉不起来，而插件会**静默 fail-open** —— 看起来一切正常，实际完全没压缩。

**5. 重启 DSH**

sidecar 会由插件在启动时自动拉起（`pythonw` 无窗口）。验证：

```bash
curl http://127.0.0.1:8797/health
# {"ok": true, "version": "0.1.5", "uptime_s": 12.3, "compressors_loaded": {}, ...}
```

`compressors_loaded` 初始为空是正常的 —— 压缩器懒加载，第一次用到才载入。

---

## 项目结构

```text
bale/
├── lib/
│   ├── index.js            # DSH 钩子、替换逻辑、headroom_retrieve 工具
│   ├── config.js           # 配置默认值与解析
│   └── sidecar-client.js   # HTTP 客户端、sidecar 自动拉起、熔断
├── sidecar/
│   └── sidecar.py          # Python 压缩服务（127.0.0.1:8797）
├── tests/
│   └── unit.mjs            # 单元 / 集成测试（需要 sidecar 在运行）
├── examples/
│   └── cordis.patch.example.yml
├── docs/
│   └── dependency-slimming-plan.md   # Python 依赖精简（重打包 wheel）方案
├── tools/
│   └── repack/             # 重打包脚本、原始 wheel 信息、修改声明
├── vendor/
│   └── wheels/             # 精简后的 headroom_ai-0.42.0.post1 wheel
├── cordis.patch.yml        # 插件自带的 bundle 配置
├── package.json
├── requirements.txt
├── README.md
└── LICENSE
```

---

## 配置参考

### 插件配置（`cordis.patch.yml` 的 `config`）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `minChars` | `8192` | 低于此长度不压缩 |
| `maxChars` | `33554432` | 超过此长度不压缩（32MB，防巨量输出卡死） |
| `tools` | 见下 | 允许压缩的工具白名单 |
| `protectTools` | 见下 | 强制不压缩的工具 |
| `kinds` | `["log","json","code","diff","search","generic"]` | 允许的压缩类型（`text` 默认停用） |
| `kindsAreOpen` | `true` | true = 以 sidecar 判定为准，`kinds` 仅作参考；false = 严格按 `kinds` 过滤 |
| `sidecarUrl` | `http://127.0.0.1:8797` | sidecar 地址 |
| `timeoutMs` | `15000` | 压缩超时（spill 全文场景自动放宽 4 倍） |
| `replaceMode` | `replace` | `replace` 替换内容；`preview` 只加标记不替换 |
| `sidecarPython` | `null` | sidecar 用的 pythonw 绝对路径 |
| `sidecarScript` | `null` | sidecar.py 绝对路径 |
| `storeDir` | `null` | 原文存储目录 |
| `wsDir` | `null` | 工作区目录（CCR 缓存等） |
| `logFile` | `null` | 插件日志路径 |

**默认白名单**：

```yaml
tools: ['ops_bash', 'bash', 'pwsh', 'web_fetch', 'run_code']
```

**默认保护名单**（输出必须逐字精确的场景）：

```yaml
protectTools:
  - read, edit, write, grep, glob
  - ops_read_files, ops_inspect_local_file, ops_edit_file,
    ops_write_file, ops_grep, ops_glob, ops_stat,
    ops_read_table, ops_read_docx, ops_read_pdf
```

> 读代码、搜索代码、读文档类的工具**一律不压** —— 模型需要精确行号、路径和代码原文，任何折叠都可能误导它。

### 环境变量（sidecar 层）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HEADROOM_DISABLED_KINDS` | `text` | 逗号分隔，禁用的 kind。设空串可全部启用 |
| `HEADROOM_STORE_DIR` | `<sidecar>/store` | 原文存储目录 |
| `HEADROOM_STORE_MAX_MB` | `256` | store 容量上限（超限按 mtime 淘汰最旧） |
| `HEADROOM_STORE_TTL_HOURS` | `168` | store 保留时长（7 天） |
| `HEADROOM_WORKSPACE_DIR` | `<sidecar>/headroom_ws` | 工作区 |
| `HEADROOM_LOG_DIR` | `<sidecar>` | sidecar 日志目录 |
| `HEADROOM_HF_HOME` | 自动推导 | HuggingFace 缓存目录（text 压缩用） |
| `HEADROOM_ALLOWED_PATHS` | 空 | 允许 `/compress` 读取的额外目录（`path` 模式白名单） |
| `HEADROOM_CRIT_BUDGET` | `200` | 关键行提取上限（行数） |
| `HEADROOM_SAMPLE_HEAD` | `80` | `generic` 保留头部行数 |
| `HEADROOM_SAMPLE_TAIL` | `40` | `generic` 保留尾部行数 |
| `HEADROOM_SAMPLE_MIN_CHARS` | `8192` | `generic` 抽样触发阈值 |

---

## HTTP 接口

sidecar 只绑定 `127.0.0.1`，不对外暴露。

### `POST /compress`

```json
// 请求（二选一：text 或 path）
{"text": "...", "kind": "log", "language": null}
{"path": "C:/Temp/dsh-subprocess-xxx-stdout.log"}

// 响应
{
  "hash": "aed911cf65b18eb4",
  "kind": "log",
  "in_chars": 64050,
  "out_chars": 103,
  "ratio": 0.0016,
  "ms": 13.7,
  "compressed": true,
  "text": "[800 lines omitted: 800 INFO]\n[800 lines compressed to 0. ...]"
}
```

`compressed: false` 表示无收益（输出不小于输入）或类型被禁用，此时 `text` 返回原文。

### `GET /retrieve/<hash>` 或 `POST /retrieve`

返回 `application/octet-stream` 原始字节。哈希不存在返回 404。

### `GET /health`

```json
{
  "ok": true,
  "version": "0.1.5",
  "uptime_s": 1069.4,
  "compressors_loaded": {"log": true, "generic": true},
  "text_model_ready": false,
  "disabled_kinds": ["text"],
  "store": {"files": 307, "bytes": 33547760, "mb": 31.99, "max_mb": 256, "ttl_hours": 168}
}
```

### `GET /store`

仅返回 store 用量统计，不触发淘汰。

### `GET /evict`

手动触发淘汰（按 `HEADROOM_STORE_MAX_MB` + `HEADROOM_STORE_TTL_HOURS`）。返回 `{"removed": N, "freed_bytes": M, "kept": K}`。

---

## 工作原理

### 1. 钩子挂载

插件监听 DSH 的 `tools/post-execute` 钩子。这是 DSH 提供的官方扩展点，允许在工具结果进入模型上下文之前修改它。

**关键：必须调用 `next()`**

```javascript
ctx.on("tools/post-execute", async (exec, result, next) => {
  // 先让下游插件执行完（DSH 是 waterfall 语义，不调 next() 会否决整条链）
  let downstream;
  try { downstream = await next(); }
  catch { downstream = { kind: "accept" }; }

  try {
    if (!cfg.enabled) return downstream;
    if (downstream.kind === "block") return downstream;  // block 决策必须透传

    const decision = await maybeCompress(exec, result, cfg);
    if (!decision) return downstream;

    // 只覆盖 content，保留下游的 additionalContexts
    return { ...downstream, content: decision.content };
  } catch (e) {
    return downstream;  // fail-open
  }
});
```

不调 `next()` 会导致**所有下游插件的 post-execute 监听器被跳过** —— 这是 cordis 的 waterfall 语义：监听器按最外层优先执行，不调用 `next()` 的那个会阻止后续所有监听器执行。

### 2. 判定流程

```
1. 工具在白名单？          否 → 放行
2. 结果 isError？          是 → 放行（错误信息要完整）
3. content 全是 text 块？  否 → 放行（图片/混合块）
4. 长度在 [minChars, maxChars]？
5. 结果带 truncated + spillPath？→ 送 spill 全文，否则送 content 文本
6. sidecar 返回 compressed: true？→ 替换
   否则 → 放行（记录 skip 日志）
```

### 3. 内容类型判定

```python
def detect_kind(text):
    # 1. 能解析为 JSON → json
    if text.strip()[0] in "[{":
        try: json.loads(text); return "json"
        except: pass

    # 2. diff / search 用轻量正则检测（置信度 > 0.8 才采纳）
    d = detect_content_type(text)
    if d.confidence >= 0.8:
        if d.content_type is GIT_DIFF:       return "diff"
        if d.content_type is SEARCH_RESULTS: return "search"

    # 3. 时间戳行占比 ≥ 30% → log
    if timestamp_lines / total_lines >= 0.3: return "log"

    # 4. 兜底 → generic（行级抽样）
    return "generic"
```

### 4. 输出格式

模型看到的替换结果长这样：

```
[headroom 已压缩] 原文 64050 字符（类型 log）压缩至 0.16%，原文 hash=aed911cf65b18eb4。如需完整原文，调用 headroom_retrieve 工具并传入该 hash。

[800 lines omitted: 800 INFO]
[800 lines compressed to 0. 完整原文可经 headroom_retrieve 取回]
```

`generic` 类会额外在开头放摘要和折叠提示：

```
[headroom] 压缩视图（原文 980 行 / 43941 字符，保留 120 行：头 80 + 关键行 0 + 尾 40）
------------------------------------------------------------
<cwd 与命令输出前 80 行>
[headroom] 已折叠中间 860 行（原文 980 行 / 43941 字符）
<最后 40 行，含 [exit code: 0]>
```

### 5. 取回

插件注册了 `headroom_retrieve` 工具：

```
headroom_retrieve(hash="aed911cf65b18eb4")
→ 返回 64,050 字符的完整原文
```

哈希是原文 SHA-256 的前 16 位十六进制。相同内容总是得到相同哈希，不重复写盘。

---

## 设计决策

这一节记录了几个**反直觉但经过实测验证**的选择，避免后来者重复踩坑。

### ⚠️ 不要在插件层过滤 kind

插件的 `kinds` 过滤发生在**压缩完成之后**：

```javascript
const r = await compressWithEnsure(...)   // ← 先花 8-27 秒压缩
if (!cfg.kinds.has(r.kind)) return null;  // ← 才判断要不要用
```

这意味着只改插件配置挡不住 sidecar 的计算开销。要真正禁用某类压缩，必须用 `HEADROOM_DISABLED_KINDS` 在 sidecar 层短路。

### ⚠️ `compressed: true` 不等于内容可用

LogCompressor 在纯 INFO 日志上的行为是**清零**：

| 输入 | 输出 | 保留行数 |
|---|---|---|
| 400 行纯 INFO | **93 字符** | **0** |
| 400 行含 ERROR | 2,013 字符 | 53 |

`ratio = 0.001` 看起来漂亮，但模型什么都没看到。因此加了两道护栏：

1. **清零检测**：压缩后保留行数 < 5 且原行数 > 20 → 退化到行级抽样
2. **输出过小拒绝**：`ratio < 0.005` 且 `len < 200` → 拒绝替换，返回原文

### ⚠️ 关键行必须在行首

第一版给关键行加了 `[L83]` 行号前缀方便定位，结果破坏 diff 语义：

```
[L83] -    old_2 = 1     ← 模型无法识别这是删除行
```

`-/`+` 必须在行首。已改为**逐字保留，不加任何前缀**。

### ⚠️ 摘要放在开头

第一版把摘要放输出末尾，模型读 `[exit code: N]` 和堆栈结尾时会读错。改成放开头，保证最后一行严格等于原文末行。

### ⚠️ diff-change 正则必须限定在 diff 块内

用 `^[-+]\s*[^+-]` 匹配改动行，会把 `ls -la` 的输出全部误判：

```
-rwxr-xr-x 2 user None 100 Nov 6 16:00 file_1.py   ← 被当成删除行
```

300 行的 ls 列表会吃掉 200 条关键行预算。正确做法是先追踪 `diff --git` / `@@` 确认进入 diff 块，块内才匹配 `-/+`。

### ⚠️ 不接 ContentRouter，只用它的检测器

项目自带的 `ContentRouter` 有 12 种策略，但实测在这台机器上：

| Strategy | 实测 ratio | 结论 |
|---|---|---|
| `tabular` | **1.000**（CSV/JSON/NDJSON 全部不压） | 空转 |
| `config` | **1.000**（YAML/扁平 KV 全部不压） | 空转 |
| `html` | `ModuleNotFoundError: trafilatura` | 缺依赖 |
| `mixed` | 0.998（60 段路由，几乎不省） | 无效 |
| `code_aware` | 0.400 但**把业务逻辑替换成 `pass`** | **危险** |

`code_aware` 最要命：同样的 30 个订单处理类，它保留 `def` 和 docstring，却把 `total = sum(...)`、`self.repo.save(order)` 全删了换成 `pass`。语法完全合法，看起来像真代码 —— 模型会基于错误前提继续推理。

**这不叫信息丢失，叫信息造假。** 所以只借用它的轻量正则检测器（`detect_content_type`，< 2ms）识别 diff/search，不接入整个 ContentRouter。

### ⚠️ 熔断，防止 sidecar 挂掉时每次工具调用卡 8 秒

`ensureSidecar` 原本用 `force: true` 绕过 30 秒节流，导致每次调用都完整等 8 秒：

```
第 1 次失败: 8028 ms
第 2 次失败: 8011 ms   ← 完全没衰减
第 3 次失败: 8008 ms
```

现在加了指数退避熔断：失败后 30s → 60s → 120s → 上限 10 分钟，成功一次立即重置。

```
修复后: 3029 ms / 0 ms / 0 ms / 0 ms / 1 ms
```

### ⚠️ 更新后旧 sidecar 自动退出

sidecar 是 `detached` 常驻进程，`pnpm install` 只替换磁盘文件，不会影响已加载进内存的旧代码。如果更新后只重启 DSH，插件的健康探测会直接复用旧进程，新代码永远不生效。

现在的做法：sidecar 启动时记录自身脚本的 SHA-256，之后每 10 秒重新计算一次。文件被替换后哈希变化，进程写一条 `self-exit` 日志并退出；插件的 `compressWithEnsure` 在下一次连接失败时自动拉起新进程，DSH 重启时也会重新拉起。

因此更新流程里不再需要手动结束进程。代价是更新落盘后的 10 秒窗口内，可能有某次压缩请求失败——按 fail-open 原样放行，不影响工具调用本身。

---

## 已知限制

1. **`minChars` 是硬门槛** —— 大量 4-8K 的中等输出不会被压缩。实测一个 631 次调用的会话里，58% 的输出不到 200 字符，命中率仅 0.3%。这是设计取舍（短输出压缩收益不稳定），不是 bug。

2. **纯 INFO 日志的死区** —— 60-140 行（约 4,500-10,500 字符）的纯 INFO 日志会进入 LogCompressor 的清零区间。当前 `minChars=8192` 下这个区间很窄（8,251-10,531），实测命中 0 次。**如果把 `minChars` 降到 4096，死区会扩大 6 倍，属于高危改动**（需要先修护栏逻辑）。

3. **`generic` 有独立的 8192 门槛** —— `HEADROOM_SAMPLE_MIN_CHARS` 是行级抽样自己的阈值，改插件的 `minChars` 不会影响它。

4. **store 无压缩** —— 原文按字节存，不做 gzip。33MB 对应约 300 次压缩。已通过 `HEADROOM_STORE_MAX_MB` + TTL 控制总量。

5. **单行超长内容不压** —— 压缩成一行的 JSON、HTML 切不出行结构，实测 ratio 恒为 1.000。

6. **`text` 类依赖 ONNX 模型** —— 默认停用。若自行启用，需确保 `HEADROOM_HF_HOME` 下有 Kompress 模型缓存。

7. **仅限本地** —— sidecar 只绑 `127.0.0.1`，不支持远程调用。

---

## 排障

### 工具输出没被压缩

按顺序检查：

```bash
# 1. sidecar 活着吗
curl http://127.0.0.1:8797/health

# 2. 插件加载了吗（看是否有 apply 事件）
tail -20 <logFile>

# 3. 有 compress 事件吗
grep '"event":"compress"' <logFile> | tail -5

# 4. 有 skip 事件吗（看到 skip 说明钩子触发了但被判定放行）
grep '"event":"skip"' <logFile> | tail -5
```

| 现象 | 原因 | 处理 |
|---|---|---|
| 无任何日志 | 插件没加载 | 检查 profile `package.json` 的 bundles 列表 |
| 只有 `skip`，`contentLen` 很小 | 低于 `minChars` | 正常行为 |
| 只有 `skip`，`contentLen` 很大 | 内容被判成 `text` 或其他禁用类型 | 检查 `detect_kind` 结果 |
| `passthrough-error` | sidecar 挂了或超时 | `curl /health` 确认 |
| `ensure-sidecar: ok:false` | python 路径错 | 检查 `sidecarPython` 绝对路径 |

### sidecar 起不来

```bash
# 手动启动看报错
<venv>/Scripts/python.exe -I <sidecar.py> --port 8797

# 端口占用
netstat -ano | findstr 8797      # Windows
lsof -i :8797                     # macOS/Linux
```

### `headroom_retrieve` 返回 404

- 哈希是否抄对（16 位十六进制）
- 是否已被 TTL / 容量淘汰（`GET /store` 查看）
- sidecar 重启后 store 是否指向同一目录（检查 `HEADROOM_STORE_DIR`）

### 压缩后内容不可读

**这是最重要的排障项。** 如果模型抱怨"看不到内容"，检查：

1. 压缩后的 `out_chars` 是否过小（< 200）
2. `ratio` 是否 < 0.005
3. 正文里是否有实际的原始行（而不是只有 `[N lines omitted]`）

如果是，说明护栏没生效 —— 检查 `_MAX_RESTORE_CHARS` 和 `_SAMPLE_MIN_CHARS` 的关系。

---

## 卸载

```bash
# 1. 从 profile package.json 移除
#    - dependencies 里的 bale
#    - dsh.profile.bundles 里的 "bale"

# 2. 移除配置
#    从 cordis.patch.yml 删掉 id: bale 那一段

# 3. 卸载依赖
cd <PROFILE> && pnpm install

# 4. 停 sidecar
#    Windows: taskkill /PID <pid> /F   （pid 从 curl /health 或 netstat 查）
#    macOS/Linux: kill <pid>

# 5. 删除运行时数据（可选）
rm -rf <PROFILE>/.headroom
```

---

## 致谢

压缩算法来自 [headroom-ai](https://pypi.org/project/headroom-ai/) 项目（LogCompressor / SmartCrusher / DiffCompressor / SearchCompressor / ContentRouter）。

本插件做的是把它接到 DSH 的钩子上，并针对 Agent 工具输出的特点做了适配：内容类型判定、关键行提取、清零护栏、熔断、原文存储与取回。

---

## 依赖精简说明

### 为什么要重打包

上游 `headroom-ai` 0.42.0 的 base 依赖把 LLM 代理栈（`litellm`、`openai`、`tokenizers`、`huggingface-hub`、`hf-xet`、`boto3` / `botocore` 等）一并带了进来，另有一个 `ast-grep-cli` 会往 `Scripts/` 写两个各 50 MB 的二进制。这些包在 `log/json/code/diff/search` 压缩路径上从不被 import，却让 venv 多占约 355 MB。

处理方式是重打包 wheel：只修改 `METADATA` 的 `Requires-Dist` 与 `Version`，同步重算 `RECORD`，包内 Python 源码、`_core.pyd`（Rust 压缩本体）与 `headroom/ccr/` 均未改动。产物在 `vendor/wheels/`，版本号 `0.42.0.post1` 用于区分上游原版；SBOM 未修改，对应上游 0.42.0 原版。重打包工具与修改声明见 [`tools/repack/`](tools/repack/)，完整方案与基线见 [`docs/dependency-slimming-plan.md`](docs/dependency-slimming-plan.md)。

实测：venv 从 602 MB / 78 dist 降到 236 MB / 23 dist（`du` 口径下为 632 MB → 240 MB）；5 种压缩的 sha256 与上游 0.42.0 基线逐项一致。

### 必须显式保留 `httpx`（最关键）

上游 METADATA 中 `httpx` 只出现在 `dev` / `mcp` / `proxy` 三个 extra 分支里，当前能装上是因为 `litellm` 顺带带入。删掉 `litellm` 后如果不显式声明，`httpx` 会一起消失，而压缩路径硬依赖它：

```text
smart_crusher.py → ccr.tool_injection → ccr/__init__.py
                 → ccr/batch_processor.py → import httpx
```

实测屏蔽 `httpx` 后 json 压缩直接 `ImportError`。因此精简后的 7 条 base 依赖是：

```text
httpx>=0.24.0
pydantic>=2.0.0
click>=8.3.3
rich>=13.0.0
opentelemetry-api>=1.24.0
tree-sitter-language-pack>=0.10.0,<1.0
tree-sitter>=0.25.2,<0.27
```

`anyio` / `certifi` / `httpcore` / `idna` 由 `httpx` 自身声明，`typing_extensions` 由 `opentelemetry-api` 传递引入，均无需手写。

### 上游发新版时如何重打靶

1. 从 PyPI 下载新版本 wheel，把 URL / 大小 / sha256 记录到 `tools/repack/SOURCE.txt`；
2. 检查新版 base 依赖是否仍包含 litellm 全家桶，确认压缩路径的真实 import 没有新增包；
3. 更新 `tools/repack/repack.py` 中的 `OLD_VERSION` / `NEW_VERSION`；
4. 重新打包：`python tools/repack/repack.py --src <新版 wheel> --out-dir vendor/wheels`；
5. 在隔离 venv 中跑 `docs/dependency-slimming-plan.md` §1.4 的 5 种压缩基线，确认 sha256 一致或明确记录算法变更；
6. 更新 `requirements.txt` 的版本锁定，并重新生成生产 venv。

---

## 开发与测试

```bash
# 语法检查
npm run check        # 三个 JS 文件
npm run check:py     # sidecar.py

# 单元 / 集成测试（需要 sidecar 已在 127.0.0.1:8797 运行）
npm test
```

测试覆盖白名单与保护名单、`minChars` 阈值、spill 全文、WARN/ERROR 保留、diff/search 路由、熔断、path 白名单、retrieve 往返等。部分用例会真实写入 store，跑完可用 `GET /evict` 或调整 `HEADROOM_STORE_TTL_HOURS` 清理。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

---

## 贡献

欢迎提 issue 和 PR。提交前请确保：

1. `node --check` 通过所有 `.js` 文件
2. `python -m py_compile` 通过 `sidecar.py`
3. 新增压缩类型时附上实测 ratio 和耗时数据
4. **任何会让模型看到错误内容的改动都不接受** —— 宁可少压，也不造假
