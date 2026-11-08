# headroom-ai 依赖精简（重打包 wheel）执行计划

> **执行者须知**：本文件是自包含的。所有路径、URL、sha256 基线、判定阈值都已在文中给出，不需要重新探测。
> 若某一步的结果与文中"已核实事实"不符，**停止并回报**，不要自行修正后继续。

---

## 0. 目标与边界

```
现状：venv 632 MB / 78 个 dist（du 口径）
目标：venv < 450 MB（预期 ~400 MB）/ dist < 40（预期 ~30）
方式：重打包 headroom_ai wheel，从 METADATA 的 Requires-Dist 移除无关依赖
```

> **执行结果（2026-11-08 实测）**：实际达成 venv 632 MB → 240 MB、dist 78 → 23，
> 优于预期的 ~400 MB。原因是 litellm 之外的 `ast-grep-cli` 在 `Scripts/` 下留了
> 两个各 50 MB 的二进制（`sg.exe` / `ast-grep.exe`），随依赖移除一并消失。
> 本文档下文的「预期 ~400 MB」等数值保留为制定计划时的原始估算，未回改。

**要改的**：仅 `headroom_ai-0.42.0.dist-info/METADATA` 中的 `Requires-Dist` 与 `Version`，以及随之更新的 `RECORD`。

**绝对不要改的**：

| 对象 | 原因 |
|---|---|
| `headroom/**/*.py` | Python 源码与依赖声明无关 |
| `headroom/_core.pyd`（27.9 MB） | 压缩算法本体，Rust 编译产物 |
| `headroom/ccr/` | 虽名为 CCR（代理相关），但 `smart_crusher.py` 通过 `ccr.tool_injection` 依赖它 |
| `lib/*.js`、`sidecar/sidecar.py` | 属于 headroom-auto 插件，本次不动 |
| `sboms/*.cyclonedx.json`（440 KB） | 改造不现实，在 README 声明即可 |

---

## 1. 已核实事实（可直接依赖，无需重查）

### 1.1 上游产物

| 产物 | URL | 大小 |
|---|---|---|
| wheel (win_amd64) | `https://files.pythonhosted.org/packages/21/21/233710bc313c07cf652ed4234574f8906becdbe22b559413394e775fd8eb/headroom_ai-0.42.0-cp310-abi3-win_amd64.whl` | 15.0 MB |
| sdist（备用） | `https://files.pythonhosted.org/packages/c7/8c/90733323fd1f65cc815e3bc0722823b263fcd04cb8ae1f4b3faab1e4dc5a/headroom_ai-0.42.0.tar.gz` | 3.31 MB |

wheel tag = `cp310-abi3-win_amd64`，**abi3，Python 3.10–3.14 通用**，只需打一次包。

### 1.2 运行时真实依赖（实测 `sys.modules` 增量）

启用 log/json/code/diff/search 全流程后，实际加载的第三方顶层包仅 13 个：

```
click, headroom, httpx, idna, opentelemetry, pygments, rich,
tree_sitter, tree_sitter_c_sharp, tree_sitter_embedded_template,
tree_sitter_language_pack, tree_sitter_yaml, typing_extensions
```

### 1.3 硬性阻断项（屏蔽即失败）

| 包 | 屏蔽后果 |
|---|---|
| `opentelemetry` | 100% ImportError（`headroom/observability/metrics.py`） |
| `typing_extensions` | 100% ImportError |
| `httpx` | **json 压缩 ImportError** —— 见 §2 陷阱 1 |

### 1.4 验收基线 sha256

```
log      in=23590   out=103     sha=4fda7083b9f99b54
json     in=25817   out=15679   sha=a3b9495cc7eec9c2
code     in=27380   out=32979   sha=590d391c4c0b5cb3   ← 见 §7.1，输出大于输入是正常现象
diff     in=15300   out=2594    sha=1445f42373acf799
search   in=21193   out=2429    sha=09ee7a8639b2eab3
```

### 1.5 可移除清单（site-packages 实测 527 MB）

| 包 | 体积 | 移除理由 |
|---|---|---|
| litellm | 111.9 MB | LLM 网关，压缩路径零引用 |
| onnxruntime | 41.6 MB | 仅 text 类型使用（已禁用） |
| numpy + numpy.libs | 39.8 MB | 仅被 onnxruntime 依赖 |
| botocore | 19.6 MB | 仅 boto3 → litellm |
| hf_xet | 9.1 MB | HF 传输，压缩路径零引用 |
| openai | 9.0 MB | 仅 litellm |
| tokenizers | 7.7 MB | 仅 litellm |
| huggingface_hub | 3.1 MB | 仅 litellm/tokenizers |
| tiktoken | 2.4 MB | `kompress_compressor.py:1684` 有 try/except 保护 |
| aiohttp, regex, fsspec, jinja2 | 3.9 MB | 仅 litellm |
| boto3, s3transfer, jmespath | 1.3 MB | 仅 litellm |
| magika, sqlite_vec, orjson, zstandard, jiter, httpx2, httpcore2 | 2.4 MB | 仅 proxy/mcp |
| **合计（含 dist-info）** | **254.1 MB** | |

### 1.6 阻塞验证已完成

屏蔽上述全部 19 个包后，5 种压缩类型 **5/5 全部成功**。

---

## 2. 五个必须知道的陷阱

### 陷阱 1：`httpx` 会随 litellm 一起消失（最关键）

METADATA 原文中 `httpx` **只出现在 extra 分支**：

```
Requires-Dist: httpx[http2,socks]>=0.24.0 ; extra == 'dev'
Requires-Dist: httpx>=0.24.0 ; extra == 'mcp'
Requires-Dist: httpx[http2]>=0.24.0 ; extra == 'proxy'
```

它当前能装上，是因为 `litellm` 和 `huggingface_hub` 把它带进来。而压缩路径**硬依赖**它：

```
smart_crusher.py → ccr.tool_injection → ccr/__init__.py → ccr/batch_processor.py → import httpx
```

**实测：屏蔽 httpx 后 json 压缩直接 ImportError。**

**所以新的 Requires-Dist 必须显式加回 `httpx`**，并连带保留 `anyio` / `certifi` / `httpcore` / `idna`（由 httpx 自身声明，无需手写）。

### 陷阱 2：RECORD 必须重算

RECORD 中 METADATA 那一行含 sha256：

```
headroom_ai-0.42.0.dist-info/METADATA,sha256=nxQ7...,45722
```

改了 METADATA 必须重算该行，否则 `pip install --force-reinstall` 与完整性检查会失败。

### 陷阱 3：SBOM 会不一致

`headroom_ai-0.42.0.dist-info/sboms/headroom-py.cyclonedx.json`（440 KB）列出全部依赖。
**不要改它**。在 fork README 注明"SBOM 对应上游原版"即可。

### 陷阱 4：`pnpm install` 不能用 `ops_bash` 运行

`ops_bash` 运行在 `node_modules/dsh-ops/runtime/winuxcmd/bin/bash.exe` 上。`pnpm install` 需要替换 `node_modules/`，会因"正在使用的 exe 无法删除"而自锁，**导致 dsh-ops 被删坏**（上次已实际发生并修复）。

**必须用 Node 直接 `spawn` 或系统 shell 执行 pnpm。**

### 陷阱 5：Apache-2.0 要求标注修改

`License-Expression: Apache-2.0`，第 4(b) 条要求修改后的分发文件带显著修改声明。fork 的 README 开头必须包含：

```markdown
## 修改说明

本包衍生自 headroom-ai 0.42.0（Apache-2.0），**仅**修改了 METADATA 中的
Requires-Dist 声明，移除了与本项目无关的 LLM 代理相关依赖。
Python 源码与 _core.pyd 二进制未做任何修改。
```

---

## 3. 最终 Requires-Dist 清单

```python
REQUIRED = [
    # 运行时实测必需
    "httpx>=0.24.0",              # 陷阱 1：ccr → batch_processor 硬依赖
    "pydantic>=2.0.0",
    "click>=8.3.3",
    "rich>=13.0.0",
    "opentelemetry-api>=1.24.0",
    # code 压缩类型必需
    "tree-sitter-language-pack>=0.10.0,<1.0",
    "tree-sitter>=0.25.2,<0.27",
]
# typing_extensions 由 opentelemetry-api 传递引入，无需手写
```

**其余全部删除**：`litellm`、`tiktoken`、`hf-xet`、`ast-grep-cli`、`pyyaml`、`tomli`、`tomlkit`、`truststore`、`filelock`。

> 可选：`pyyaml`（1 MB）建议保留，`yaml` 常被间接引用，代价极低。执行者可在阶段 C 验证后按需加回。

---

## 4. 执行步骤

### 阶段 A：准备 fork 工程（约 20 分钟）

```
A1. 在 dsh_headroom 仓库下新建：

    dsh_headroom/
    └── tools/
        └── repack/
            ├── repack.py       ← 重打包脚本
            ├── SOURCE.txt      ← 原始 wheel 的 sha256 与 URL
            └── README.md       ← 修改声明（内容见陷阱 5）

A2. 下载原始 wheel 到临时目录。
    注意：不要用 curl -L 前台同步下载，实测会超时。
    用后台 job，或 python urllib 带 timeout + 重试。

A3. 记录原始 wheel 的 sha256 写入 SOURCE.txt。
```

### 阶段 B：编写 repack.py（约 40 分钟）

脚本需完成：

```
B1. 解压 wheel 到临时目录

B2. 解析 METADATA：
    - 删除所有含 "extra ==" 的 Requires-Dist 行
    - 删除不含 extra 标记、且不在 REQUIRED 清单中的 Requires-Dist 行
    - 保留 §3 清单中的行
    - Version: 0.42.0 → 0.42.0.post1
      注意：必须同时改 METADATA 的 Version 和 RECORD 里的目录/文件名前缀

B3. 重写 METADATA
    - 保持原有换行风格（LF/CRLF 与原文一致）
    - 保持头部字段顺序不变

B4. 重算 RECORD：
    - 遍历 wheel 内所有文件，计算 sha256 与大小
    - 更新 METADATA 行
    - RECORD 自身那一行必须是 "path,,"（空 hash、空 size）

B5. 重新打包 zip，文件名：
    headroom_ai-0.42.0.post1-cp310-abi3-win_amd64.whl
    注意：
      - zip 内路径分隔符必须是 "/"，不能是 "\\"
      - zip 内顶层目录需改名匹配新版本号

B6. 输出到 dsh_headroom/vendor/wheels/
```

**B5 完成后立即自检**：

```python
import zipfile
zipfile.ZipFile(path).testzip()          # 必须返回 None
import importlib.metadata as md
d = md.distribution("headroom-ai")        # 在测试 venv 中
len(list(d.requires or []))               # 必须等于 7（或含 pyyaml 时 8）
```

### 阶段 C：隔离环境验证（约 30 分钟，**必须做**）

```
C1. 建临时 venv（Python 3.12，与生产一致）
C2. uv pip install --find-links dsh_headroom/vendor/wheels "headroom-ai[code]==0.42.0.post1"
C3. uv pip check            → 必须输出 "All installed packages are compatible"
C4. 跑 5 种压缩，与 §1.4 基线 sha256 逐项比对
C5. 检查 dist 数量           → 预期约 30 个
C6. du -sh venv             → 记录实际体积
```

**C3 或 C4 不通过 → 停止，回报，不要进入阶段 D。**

### 阶段 D：接入 DSH（约 20 分钟）

```
D1. 停 sidecar：
    netstat -ano | findstr :8797      → 取 PID
    杀进程树（父 pythonw.exe + 其拉起的子解释器）
    只杀父进程会留下子进程继续占用 8797

D2. 备份现有 venv：
    mv .headroom/venv .headroom/venv-062-backup

D3. 新建 venv 并安装重打包 wheel

D4. 起 sidecar，验证 /health：
    version 必须为 0.1.3
    compressors_loaded 应含 log/generic（其余按需加载）

D5. 端到端验证：
    - 用 ≥3 个真实 .py 文件跑 code 压缩
    - 跑一次 retrieve 往返，比对 byte-identical
```

### 阶段 E：固化与文档（约 20 分钟）

```
E1. dsh_headroom/requirements.txt 改为：

    --find-links ./vendor/wheels
    headroom-ai[code]==0.42.0.post1

E2. 完成 tools/repack/README.md（修改声明 + 上游升级时的重打靶步骤）

E3. dsh_headroom/README.md 增加一节「依赖精简说明」，写清：
    - 为什么要重打包
    - httpx 必须显式保留的原因（陷阱 1）
    - 上游发新版时的重打靶流程
```

---

## 5. 验收标准（全部满足才算完成）

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | `uv pip check` | `All installed packages are compatible` |
| 2 | 5 种压缩 sha256 | 与 §1.4 基线**逐项一致** |
| 3 | `retrieve` 往返 | byte-identical |
| 4 | `/health` | `ok:true`，`version:0.1.3`，压缩器在线 |
| 5 | venv 体积 | < 450 MB（预期 ~400 MB） |
| 6 | dist 数量 | < 40（预期 ~30） |
| 7 | 真实代码压缩 | ≥3 个真实 .py，输出非空且可读 |
| 8 | 旧 store 兼容 | 迁移前 11 个 hash 全部可正常取回 |
| 9 | sidecar 重启 | 由插件自动拉起成功（非手动） |

---

## 6. 回滚方案

任一阶段失败均可秒级回退：

```bash
# 恢复到本次操作前的环境
mv .headroom/venv-062-backup .headroom/venv

# 或回到官方原版
uv pip install "headroom-ai==0.42.0"
```

备份约 632 MB，动手前确认磁盘余量。

---

## 7. 注意事项

### 7.1 code 类型基线 sha 的"异常"

§1.4 中 `code in=27380 out=32979`，**输出比输入大**。
这不是 bug：测试输入是高度重复的合成代码，`tree_sitter` 判定无压缩价值后原样返回并加头部标记。
**执行者不要以为这是失败，按 sha256 比对即可。**

### 7.2 不要动 `_core.pyd`

27.9 MB 的 Rust 二进制是压缩算法本体，任何修改都会破坏功能。

### 7.3 不要试图删除 `headroom/ccr/`

它虽名为 CCR（Cache-Control-Routing，代理相关），但 `smart_crusher.py` 通过 `ccr.tool_injection` 依赖它。

### 7.4 上游版本选择：继续用 0.42.0

PyPI 最新是 `0.43.1`，但**仍在 base 依赖中带 litellm**（已核实 12 项 base 依赖，litellm 在列）。
因此**继续基于 0.42.0 打靶**，与当前生产环境一致，避免引入算法变更。

### 7.5 若要同时升级到 0.43.1

需先跑完整回归（压缩输出可能与 0.42.0 不同），**不属于本计划范围**。

---

## 8. 执行者需回报的内容

1. 重打包 wheel 的 sha256 与体积
2. 阶段 C 的 5 项 sha256 比对结果（逐项 yes/no）
3. 精简后 venv 实际体积与 dist 数量
4. 阶段 D 的端到端验证结果
5. 任何计划外的异常及处理方式

---

## 9. 工作量预估

| 阶段 | 耗时 | 主要风险 |
|---|---|---|
| A 准备 | 20 min | 下载超时（需重试） |
| B 脚本 | 40 min | **RECORD 重算易错** |
| C 验证 | 30 min | 低 |
| D 接入 | 20 min | 进程树 kill 不彻底 |
| E 文档 | 20 min | 低 |
| **合计** | **约 2.5 小时** | |

**最大风险点是阶段 B 的 RECORD 重算。** 建议脚本内加断言：重打包后立刻读回 `importlib.metadata`，确认 `requires` 长度等于 §3 清单项数。

---

## 附录 A：关键路径速查

```
插件源码仓库        E:\AQYLPZ_WorkMenu\dsh_headroom
插件安装位置        D:\DSH-Home\.dsh\profiles\desktop\node_modules\headroom-auto
Python venv         D:\DSH-Home\.dsh\profiles\desktop\.headroom\venv
venv 备份           D:\DSH-Home\.dsh\profiles\desktop\.headroom\venv-062-backup
原文仓库 store      D:\DSH-Home\.dsh\profiles\desktop\.headroom\store
插件日志            D:\DSH-Home\.dsh\profiles\desktop\.headroom\logs\headroom-auto.log
sidecar 日志        D:\DSH-Home\.dsh\profiles\desktop\.headroom\logs\sidecar.log
DSH 配置            D:\DSH-Home\.dsh\profiles\desktop\cordis.patch.yml
sidecar 端口        127.0.0.1:8797
```

## 附录 B：常用验证命令

```bash
# sidecar 健康
curl -s --noproxy '*' http://127.0.0.1:8797/health

# 5 种压缩对拍
curl -s --noproxy '*' -X POST http://127.0.0.1:8797/compress \
     -H 'Content-Type: application/json' \
     -d '{"text":"<内容>","kind":"log"}'

# retrieve 往返
curl -s --noproxy '*' http://127.0.0.1:8797/retrieve/<hash>

# 依赖一致性
uv pip check

# 已安装 dist 数
ls .headroom/venv/Lib/site-packages/*.dist-info -d | wc -l
```
