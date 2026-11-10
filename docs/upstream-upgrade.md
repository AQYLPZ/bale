# 上游更新适配流程

> 本文档描述 **headroom-ai（PyPI 上游包）发布新版本后，如何将其拉到本地并适配本仓库的精简 fork**。
>
> 适用对象：本仓库维护者。阅读前请先读 `docs/dependency-slimming-plan.md`，理解为什么要重打包。
>
> 编写时间：2026-11-09。文中的版本号、体积、sha256 均为 2026-11-08 实测值，上游更新后需替换。

---

## 目录

1. [为什么没有 git merge](#1-为什么没有-git-merge)
2. [当前 fork 结构](#2-当前-fork-结构)
3. [什么情况下需要升级](#3-什么情况下需要升级)
4. [升级前检查清单](#4-升级前检查清单)
5. [阶段 0：准备与基线记录](#阶段-0准备与基线记录)
6. [阶段 1：获取并分析新版](#阶段-1获取并分析新版)
7. [阶段 2：实测运行时真实依赖（最关键）](#阶段-2实测运行时真实依赖最关键)
8. [阶段 3：配置并运行重打包](#阶段-3配置并运行重打包)
9. [阶段 4：隔离环境验证](#阶段-4隔离环境验证)
10. [阶段 5：切换生产](#阶段-5切换生产)
11. [阶段 6：端到端验收](#阶段-6端到端验收)
12. [回滚方案](#12-回滚方案)
13. [特殊场景](#13-特殊场景)
14. [常见故障排查](#14-常见故障排查)
15. [附录](#15-附录)

---

## 1. 为什么没有 git merge

本仓库**不是** headroom-ai 的 GitHub fork：

- 上游是 **PyPI 上的 Python 包** `headroom-ai`，不是 Git 仓库
- `git remote` 只有本仓库自己的 `origin`
- 没有 `upstream` remote，`gh api` 查询确认 `fork:false, parent:null`

因此"合并上游更新"不是 `git merge upstream/main`，而是：

```
下载新 wheel → 分析依赖变化 → 重打包 → 隔离验证 → 替换 venv → 升插件版本
```

**每一次都是手工操作，没有自动化。** 这也是为什么本文档要写得足够细。

---

## 2. 当前 fork 结构

### 2.1 仓库内的相关文件

```
dsh_headroom/
├── requirements.txt              ← 指向本地 wheel
├── docs/
│   ├── dependency-slimming-plan.md   （精简方案，含基线 sha256）
│   └── upstream-upgrade.md           （本文档）
├── tools/repack/
│   ├── repack.py                 ← 重打包脚本
│   ├── SOURCE.txt                ← 原始 wheel 的 URL / 大小 / sha256
│   └── README.md                 ← Apache-2.0 修改声明
└── vendor/wheels/
    └── headroom_ai-0.42.0.post1-cp310-abi3-win_amd64.whl
```

### 2.2 requirements.txt 当前内容

```
--find-links ./vendor/wheels
headroom-ai[code]==0.42.0.post1
```

`--find-links` 让 pip/uv 优先从本地目录找 wheel；`[code]` extra 提供 tree-sitter 相关依赖。

### 2.3 运行时安装位置

```
venv           D:\DSH-Home\.dsh\profiles\desktop\.headroom\venv
site-packages  <venv>\Lib\site-packages
dist-info      headroom_ai-0.42.0.post1.dist-info
```

### 2.4 repack.py 的关键常量

| 常量 | 当前值 | 说明 |
|---|---|---|
| `OLD_VERSION` | `0.42.0` | 上游版本号，决定源 dist-info 目录名 |
| `NEW_VERSION` | `0.42.0.post1` | 重打包后的版本号 |
| `TAG` | `cp310-abi3-win_amd64` | wheel 平台标签，abi3 表示 3.10–3.14 通用 |
| `OUT_NAME` | `headroom_ai-0.42.0.post1-cp310-abi3-win_amd64.whl` | 输出文件名 |
| `REQUIRED` | 7 条（见下） | 最终 Requires-Dist 清单 |

当前 `REQUIRED`：

```python
REQUIRED = [
    "httpx>=0.24.0",
    "pydantic>=2.0.0",
    "click>=8.3.3",
    "rich>=13.0.0",
    "opentelemetry-api>=1.24.0",
    "tree-sitter-language-pack>=0.10.0,<1.0",
    "tree-sitter>=0.25.2,<0.27",
]
```

> ⚠ `httpx` 是必须显式保留的。上游 METADATA 里它只出现在 `dev` / `mcp` / `proxy` 三个 extra 分支，
> 原版能装上纯粹是因为 litellm 顺带带入。删掉 litellm 后若不明示声明，`httpx` 会一起消失，
> 而 `smart_crusher` → `ccr/batch_processor` 硬依赖它，**json 压缩会直接 ImportError**。

### 2.5 repack.py 的自检项

脚本最后的 `verify_output()` 会逐个断言以下条件，任一失败即中止：

- `testzip()` 通过（zip 完整）
- 无非法成员名（反斜杠、前缀斜杠、`..`）
- 旧 dist-info 目录已全部移除
- 新 dist-info 目录存在
- `Requires-Dist` 条数 == `len(REQUIRED)`
- 输出中不含 `extra ==` 限定
- `Version:` 字段等于 `NEW_VERSION`
- `RECORD` 覆盖的条目集合 == 归档条目集合
- `RECORD` 自身那一行为空 hash + 空 size
- 每个条目的 RECORD hash 与 size 与实际内容一致
- `_core.pyd` 字节与源 wheel 完全一致
- `WHEEL` 里的 `Tag:` 未变

---

## 3. 什么情况下需要升级

### 3.1 值得升级

| 情况 | 判断方式 |
|---|---|
| 上游修了压缩算法 bug | 看上游 release notes / commit |
| 安全漏洞 | 看依赖扫描或上游公告 |
| 需要新版本才有的功能（如新压缩类型） | 看上游 changelog |
| 上游把 litellm 移出了 base 依赖 | **最理想**，可以回归官方包，见 §13.1 |

### 3.2 不必升级

| 情况 | 说明 |
|---|---|
| 只是 extras 变了 | 我们只装 `[code]`，与其余 extra 无关 |
| 只是 litellm / openai / boto3 等被删依赖更新 | 这些包已不在我们环境里 |
| 只是文档或测试变更 | 打包产物里不含 `tests/` |
| 版本号跳了但压缩算法没动 | 无收益，徒增风险 |

### 3.3 查询最新版本

```bash
# 方式一：PyPI JSON API
curl -s https://pypi.org/pypi/headroom-ai/json \
  | python -c "import json,sys; d=json.load(sys.stdin); print(d['info']['version']); print(sorted(d['releases'].keys())[-8:])"

# 方式二：uv
uv pip index versions headroom-ai
```

---

## 4. 升级前检查清单

开始之前逐条确认，**任何一条不满足就先处理**：

- [ ] `git status` 干净（无未提交改动）。若有，先提交或 stash
- [ ] 已确认要升级到哪个具体版本号
- [ ] 磁盘剩余空间 > 3 GB（新旧 venv + 临时目录）
- [ ] 已经记录当前环境的基线数据（见阶段 0）
- [ ] 知道如何回滚（见 §12）
- [ ] 当前 sidecar 运行正常（`/health` 返回 `ok:true`）

> **为什么要求工作区干净**：升级会同时改动 `requirements.txt`、`vendor/wheels/`、
> `tools/repack/`。如果原本就有未提交改动，出问题时无法用 `git checkout` 干净回退，
> 也分不清是升级引入的问题还是原有改动引入的。

---

## 阶段 0：准备与基线记录

**目标**：把当前状态完整记录下来，作为升级后的对照基准。**耗时**：约 10 分钟。

### 0.1 记录环境基线

```bash
cd /e/AQYLPZ_WorkMenu/dsh_headroom

# 当前 wheel 的 sha256
sha256sum vendor/wheels/*.whl

# 当前 venv 体积
du -sm /d/DSH-Home/.dsh/profiles/desktop/.headroom/venv

# 当前 dist 数量
ls -d /d/DSH-Home/.dsh/profiles/desktop/.headroom/venv/Lib/site-packages/*.dist-info | wc -l

# 当前 headroom-ai 版本与依赖清单
/d/DSH-Home/.dsh/profiles/desktop/.headroom/venv/Scripts/python.exe -c \
  "import importlib.metadata as m; d=m.distribution('headroom-ai'); print(d.version); print(list(d.requires or []))"
```

> 注意：venv 里通常没有 pip（uv 创建的环境默认不装）。用 `importlib.metadata` 代替 `pip show`。

### 0.2 记录压缩基线（关键）

这一组 sha256 是判断"升级后压缩行为是否改变"的唯一依据。

```bash
# 注意：这些脚本在「工作脚本/headroom插件/依赖精简/」目录下，不在「工作脚本/headroom插件/迁移验证/」下
python "E:/AQYLPZ_WorkMenu/工作脚本/headroom插件/依赖精简/阶段C-基线对拍.py"
```

该脚本直接调用压缩器（不走 HTTP），输出五个 sha 并与内置基线比对。
0.42.0 的基线（`docs/dependency-slimming-plan.md` §1.4 有完整记录）：

| kind | in_chars | out_chars | sha256 |
|---|---|---|---|
| log | 23590 | 103 | `4fda7083b9f99b54` |
| json | 25817 | 15679 | `a3b9495cc7eec9c2` |
| code | 27380 | 32979 | `590d391c4c0b5cb3` |
| diff | 15300 | 2594 | `1445f42373acf799` |
| search | 21193 | 2429 | `09ee7a8639b2eab3` |

> `code` 行是合成输入（重复函数模板），输出大于输入是 tree_sitter 的正常行为，
> 不代表压缩质量；真实代码的压缩率见 4.7（21%～46%）。

> **这组数据必须在升级前重新采集一次**，因为它是用当前生产环境跑出来的，
> 而不仅仅是文档里的历史记录。把结果存到临时文件，阶段 4 要用它做逐项比对。

### 0.3 备份当前 wheel

```bash
cp -r vendor/wheels vendor/wheels.prev
```

---

## 阶段 1：获取并分析新版

**目标**：拿到新 wheel，搞清楚它的依赖声明长什么样。**耗时**：约 20 分钟。

### 1.1 下载

先从 PyPI JSON 拿到**准确的下载 URL**（不要拼接 URL，PyPI 的路径含随机 hash）：

```bash
python - <<'PY'
import json, urllib.request
V = "0.43.0"          # ← 改成目标版本
d = json.load(urllib.request.urlopen("https://pypi.org/pypi/headroom-ai/" + V + "/json"))
for f in d["urls"]:
    if f["filename"].endswith(".whl") and "win_amd64" in f["filename"]:
        print("URL   :", f["url"])
        print("FILE  :", f["filename"])
        print("SIZE  :", f["size"])
        print("SHA256:", f["digests"]["sha256"])
PY
```

然后下载到临时目录：

```bash
cd /tmp && mkdir -p hr_upstream && cd hr_upstream
curl -L -o headroom_ai-0.43.0-cp310-abi3-win_amd64.whl "<上一步输出的 URL>"
sha256sum headroom_ai-0.43.0-*.whl      # 必须与 PyPI 给的值一致
```

> **下载可能超时**。若前台卡住，改用后台任务，或分块下载。
> 15 MB 在正常网络下几秒就完成；若超过 60 秒说明是网络问题。

### 1.2 检查 wheel 完整性

```bash
python -c "
import zipfile
z = zipfile.ZipFile('headroom_ai-0.43.0-cp310-abi3-win_amd64.whl')
print('testzip:', z.testzip())
print('entries:', len(z.namelist()))
d = [n for n in z.namelist() if n.endswith('.dist-info/METADATA')]
print('metadata:', d)
"
```

**如果 testzip 返回非 None，说明文件损坏，重下。**

### 1.3 解压并分析依赖声明

```bash
mkdir -p extracted && cd extracted
python -c "import zipfile; zipfile.ZipFile('../headroom_ai-0.43.0-cp310-abi3-win_amd64.whl').extractall('.')"
cd headroom_ai-0.43.0.dist-info

# 全部 Requires-Dist
grep '^Requires-Dist:' METADATA

# 只看 base 依赖（不带 extra 标记）
grep '^Requires-Dist:' METADATA | grep -v 'extra =='

# 许可信息
grep -E '^(License|License-Expression|License-File):' METADATA

# Python 版本要求
grep '^Requires-Python:' METADATA
```

### 1.4 回答三个关键问题

| # | 问题 | 如果答案不同怎么办 |
|---|---|---|
| 1 | base 依赖（不带 extra）有哪些？ | 与当前 `REQUIRED` 对比，决定是否增删 |
| 2 | `litellm` 还在 base 里吗？ | **不在** → 走 §13.1，不需要重打包了 |
| 3 | 新增了哪些重量级依赖？ | 逐个用阶段 2 的方法确认是否真的被 import |

**比对方法**：

```bash
# 新版 base 依赖
grep '^Requires-Dist:' METADATA | grep -v 'extra ==' | sed 's/Requires-Dist: //' | cut -d';' -f1 > /tmp/base_new.txt

# 当前 REQUIRED（从 repack.py 提取）
python -c "
import re
src = open(r'E:/AQYLPZ_WorkMenu/dsh_headroom/tools/repack/repack.py', encoding='utf-8').read()
m = re.search(r'REQUIRED = \[(.*?)\]', src, re.S)
print(chr(10).join(x.strip().strip(chr(34)) for x in m.group(1).strip().splitlines() if x.strip()))
" > /tmp/base_cur.txt

diff /tmp/base_new.txt /tmp/base_cur.txt
```

### 1.5 检查 wheel 结构是否变化

```bash
# dist-info 目录名里的版本号格式是否仍为 X.Y.Z
ls -d *.dist-info

# METADATA 的换行风格（LF/CRLF），repack 依赖 LF
python -c "d=open('METADATA','rb').read(); print('CRLF:', d.count(b'\r\n'), 'LF:', d.count(b'\n'))"

# RECORD 是否仍存在且格式一致
head -3 RECORD
```

**如果 dist-info 命名格式变了、或 METADATA 变成 CRLF、或 RECORD 缺失，`repack.py` 需要先改。**

---

## 阶段 2：实测运行时真实依赖（最关键）

**目标**：用实测代替推断，确认新版本真正 import 了哪些包。**耗时**：约 30 分钟。

**为什么这一步不能跳过**：

0.42.0 的 `httpx` 就是反例——它在 METADATA 里只出现在 extra 分支，
但 `smart_crusher` 通过 `ccr` 硬依赖它。只看 METADATA 会漏掉，装出来才发现 json 压缩崩了。

### 2.1 建临时 venv 装上游原版

```bash
uv venv /tmp/hr_upstream_venv --python 3.12
uv pip install --python /tmp/hr_upstream_venv/Scripts/python.exe "headroom-ai[code]==0.43.0"
```

> **注意**：这里装的是**上游原版**，不加 `--find-links`、不用 post 版本。

### 2.2 跑全部启用的压缩类型，记录 import 增量

```bash
cat > /tmp/probe_imports.py <<'PY'
import sys, json
STD = set(sys.stdlib_module_names)
before = {m.split('.')[0] for m in list(sys.modules)} | STD

# 复刻 sidecar 的真实调用顺序，只跑启用的 kind
from headroom.transforms.log_compressor import LogCompressor
LogCompressor().compress("2026-11-07T15:00:00Z [info] worker=3 loss=0.91" * 300)

from headroom import SmartCrusher
SmartCrusher().crush(json.dumps({"items": [{"id": i, "v": i} for i in range(300)]}))

from headroom.transforms.code_compressor import CodeAwareCompressor
CodeAwareCompressor().compress(("def f(x):\n    return x+1\n") * 200)

from headroom.transforms.diff_compressor import DiffCompressor
DiffCompressor().compress("diff --git a/x b/x\n--- a/x\n+++ b/x\n" + "@@ -1 +1 @@\n-a\n+b\n" * 100)

from headroom.transforms.search_compressor import SearchCompressor
SearchCompressor().compress("\n".join("src/f%d.js:%d: const a=1;" % (i, i) for i in range(300)))

from headroom.transforms.content_detector import detect_content_type
detect_content_type("x")

after = {m.split('.')[0] for m in list(sys.modules)}
new = sorted(x for x in after - before if not x.startswith('_'))
print(json.dumps(new, ensure_ascii=False))
PY

/tmp/hr_upstream_venv/Scripts/python.exe -I /tmp/probe_imports.py
```

**输出示例（0.42.0 实测，仅供参考）**：

```
["annotated_types", "anyio", "click", "cryptography", "headroom", "httpx", "httpx2",
 "idna", "mcp", "mcp_types", "opentelemetry", "pydantic", "pydantic_core", "pygments",
 "python_multipart", "pywintypes", "rich", "sniffio", "sse_starlette", "starlette",
 "tree_sitter", "tree_sitter_c_sharp", "tree_sitter_embedded_template",
 "tree_sitter_language_pack", "tree_sitter_yaml", "typing_extensions",
 "typing_inspection", "uvicorn", "win32api", "win32con", "win32job", "zstandard"]
```

### 2.3 逐个屏蔽可移除候选包，验证容错

不要只看"加载了什么"，还要看"不装会怎样"。

```bash
cat > /tmp/block_test.py <<'PY'
import subprocess, sys, json

TEMPLATE = r'''
import sys, json
BLOCK = {block!r}
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError("blocked: " + name)
        return None
sys.meta_path.insert(0, Block())
ok = []
try:
    from headroom.transforms.log_compressor import LogCompressor
    LogCompressor().compress("2026-11-07T15:00:00Z [info] worker=3 loss=0.91" * 300); ok.append("log")
    from headroom import SmartCrusher
    SmartCrusher().crush(json.dumps({{"items": [{{"id": i, "v": i}} for i in range(300)]}})); ok.append("json")
    from headroom.transforms.code_compressor import CodeAwareCompressor
    CodeAwareCompressor().compress(("def f(x):" + chr(10) + "    return x+1" + chr(10)) * 200); ok.append("code")
    from headroom.transforms.diff_compressor import DiffCompressor
    DiffCompressor().compress("diff --git a/x b/x" + chr(10) + "@@ -1 +1 @@" + chr(10) + "-a" + chr(10) + "+b" + chr(10)); ok.append("diff")
    from headroom.transforms.search_compressor import SearchCompressor
    SearchCompressor().compress(chr(10).join("src/f%d.js:%d: const a=1;" % (i, i) for i in range(300))); ok.append("search")
    print(json.dumps({{"ok": ok, "err": None}}))
except Exception as e:
    print(json.dumps({{"ok": ok, "err": type(e).__name__ + ": " + str(e)[:100]}}))
'''

CANDIDATES = [
  ("litellm",       ["litellm"]),
  ("openai",        ["openai"]),
  ("onnxruntime",   ["onnxruntime"]),
  ("numpy",         ["numpy"]),
  ("transformers",  ["transformers"]),
  ("tokenizers",    ["tokenizers"]),
  ("huggingface_hub", ["huggingface_hub"]),
  ("boto3/botocore", ["boto3", "botocore"]),
  ("aiohttp",       ["aiohttp"]),
  ("mcp",           ["mcp"]),
  ("httpx",         ["httpx"]),
  ("tree_sitter_language_pack", ["tree_sitter_language_pack"]),
]

print("%-28s%-40s%s" % ("blocked", "result", "error"))
print("-" * 100)
for label, block in CANDIDATES:
    src = TEMPLATE.format(block=block)
    p = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        print("%-28s%-40s%s" % (label, "CRASH", (p.stderr.strip().splitlines() or [''])[-1][:44]))
        continue
    try:
        d = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        print("%-28s%-40s%s" % (label, "?", p.stdout[:44])); continue
    print("%-28s%-40s%s" % (label, "OK %d/5 [%s]" % (len(d["ok"]), ",".join(d["ok"])), (d["err"] or "")[:44]))
PY

/tmp/hr_upstream_venv/Scripts/python.exe -I /tmp/block_test.py
```

**判读规则**：

| 结果 | 含义 | 处理 |
|---|---|---|
| 5/5 | 这个包完全用不上 | 可以从 `REQUIRED` 里排除 |
| 0/5 且 ImportError | **硬必需** | 必须写进 `REQUIRED` |
| 部分 kind 失败 | 特定类型依赖它 | 看该 kind 是否启用 |

### 2.4 生成新的 REQUIRED 清单

综合 2.2 和 2.3 的结果，确定最终清单。判断原则：

1. **硬必需**（屏蔽即 0/5）→ 必须包含
2. **实测加载**（出现在 2.2 输出中）→ 应包含
3. **完全未加载且屏蔽无影响** → 排除
4. **不确定的** → 保守起见包含（体积代价远低于功能失效）

> ⚠ 特别注意 `httpx`：即使它没有出现在 2.2 的加载列表里（因为 json 压缩才触发），
> 只要 2.3 显示屏蔽后 json 失败，就必须包含。

---

## 阶段 3：配置并运行重打包

**目标**：产出精简后的 wheel。**耗时**：约 20 分钟。

### 3.1 更新 repack.py 的常量

必须改的地方（共 6 处）：

| 位置 | 行（约） | 内容 |
|---|---|---|
| `OLD_VERSION` | 15 | 新版上游版本号，如 `0.43.0` |
| `NEW_VERSION` | 16 | `0.43.0.post1` |
| `TAG` | 17 | 通常不变（`cp310-abi3-win_amd64`），需按阶段 1.5 确认 |
| `REQUIRED` | 22–30 | 按阶段 2.4 的结果更新 |
| docstring | 3 | 描述里的版本号 |
| `--src` help 文本 | 239 | 帮助信息里的版本号 |

> 行号以 0.42.0 版本的脚本为准。改之前先 `grep -n 'OLD_VERSION\|NEW_VERSION\|TAG =' tools/repack/repack.py` 确认实际位置。

> `OLD_DIST` / `NEW_DIST` / `OUT_NAME` 是从上述常量派生的，不用单独改。

### 3.2 更新 SOURCE.txt

在文件**顶部新增一段**，不要删除历史记录：

```
headroom-ai 0.43.0 原始产物信息（本次重打包的输入）

wheel URL:
  <从阶段 1.1 得到>
wheel 文件名:
  headroom_ai-0.43.0-cp310-abi3-win_amd64.whl
wheel 大小:
  <字节数>
wheel sha256:
  <从 PyPI 得到>

下载时间:
  2026-XX-XX

────────────────────────────────────────────
（以下为历史记录，保留备查）

headroom-ai 0.42.0 原始产物信息
...
```

### 3.3 运行重打包

```bash
cd /e/AQYLPZ_WorkMenu/dsh_headroom
python tools/repack/repack.py \
  --src /tmp/hr_upstream/headroom_ai-0.43.0-cp310-abi3-win_amd64.whl \
  --out-dir vendor/wheels
```

**脚本参数**：

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--src` | 是 | — | 上游 wheel 路径 |
| `--out-dir` | 否 | `<repo>/vendor/wheels` | 输出目录 |

**成功输出示例**：

```
output: .../vendor/wheels/headroom_ai-0.43.0.post1-cp310-abi3-win_amd64.whl
entries: 601
requires: 7
size: 15453601
sha256: <64 位十六进制>
```

### 3.4 如果脚本报错

| 报错信息 | 原因 | 处理 |
|---|---|---|
| source wheel is missing ... dist-info/METADATA | 上游改了 dist-info 命名 | 改 `OLD_DIST` 的构造逻辑 |
| source RECORD does not cover exactly the archive entries | 上游 wheel 结构变化 | 检查 RECORD 是否仍存在、格式是否一致 |
| source _core.pyd missing | 二进制改名或移除 | 需人工确认新结构 |
| unsafe zip member name | 上游用了反斜杠或绝对路径 | 需人工检查打包方式 |
| output Requires-Dist count is N, expected 7 | `REQUIRED` 与输出不一致 | 检查 `rewrite_metadata` 逻辑 |
| testzip failed | 输出损坏 | 重跑；若持续失败说明写 zip 的逻辑有问题 |

**不要绕过自检**。脚本里的每一条断言都对应一次真实踩坑。

---

## 阶段 4：隔离环境验证

**目标**：在不影响生产的前提下，确认新 wheel 功能正常。**耗时**：约 30 分钟。

> **这一步是闸门。不通过就不要进阶段 5。**

### 4.1 建干净 venv 装精简 wheel

```bash
uv venv /tmp/hr_slim_test --python 3.12
uv pip install --python /tmp/hr_slim_test/Scripts/python.exe \
  --find-links /e/AQYLPZ_WorkMenu/dsh_headroom/vendor/wheels \
  "headroom-ai[code]==0.43.0.post1"
```

### 4.2 依赖一致性检查

```bash
uv pip check --python /tmp/hr_slim_test/Scripts/python.exe
```

**必须输出 All installed packages are compatible。** 若有缺失或冲突，说明 `REQUIRED` 写错了。

### 4.3 确认实际安装的包

```bash
ls -d /tmp/hr_slim_test/Lib/site-packages/*.dist-info | wc -l
ls -d /tmp/hr_slim_test/Lib/site-packages/*.dist-info | xargs -n1 basename
```

**预期**：约 23–25 个（0.42.0 精简后是 23 个）。若数量明显偏多，说明有依赖没被裁掉。

### 4.4 起临时 sidecar（换端口）

```bash
mkdir -p /tmp/hr_test_store /tmp/hr_test_ws

HEADROOM_STORE_DIR=/tmp/hr_test_store \
HEADROOM_WORKSPACE_DIR=/tmp/hr_test_ws \
HEADROOM_LOG_DIR=/tmp \
  /tmp/hr_slim_test/Scripts/python.exe -I \
  /e/AQYLPZ_WorkMenu/dsh_headroom/sidecar/sidecar.py --port 8799 &

sleep 2
curl -s http://127.0.0.1:8799/health
```

应返回 ok:true。

> `compressors_loaded` 初始为空，因为压缩器懒加载。
> 跑一次压缩后会填充，这不是异常。

### 4.5 五种类型压缩 + 与基线比对

**推荐做法：直接复用仓库里已有的对拍脚本**，避免手写输入导致口径不一致。

```bash
# 用临时 venv 的 Python 跑，指向临时 venv 的 site-packages
/tmp/hr_slim_test/Scripts/python.exe \
  "E:/AQYLPZ_WorkMenu/工作脚本/headroom插件/依赖精简/阶段C-基线对拍.py"
```

该脚本内置了 0.42.0 的五个期望值，直接调用压缩器（不走 HTTP），
输出形如：

```
kind     expected                     actual                       match
log      in=23590  out=103  4fda7083b9f99b54 | in=23590  out=103  4fda7083b9f99b54 | YES
json     in=25817  out=15679 a3b9495cc7eec9c2 | in=25817  out=15679 a3b9495cc7eec9c2 | YES
code     in=27380  out=32979 590d391c4c0b5cb3 | in=27380  out=32979 590d391c4c0b5cb3 | YES
diff     in=15300  out=2594  1445f42373acf799 | in=15300  out=2594  1445f42373acf799 | YES
search   in=21193  out=2429  09ee7a8639b2eab3 | in=21193  out=2429  09ee7a8639b2eab3 | YES
RESULT: ALL PASS
```

**若要手工跑（例如脚本已随上游变化失效）**，输入必须与脚本内的构造逐字一致，
否则 sha 比对无意义。可直接从 `阶段C-基线对拍.py` 复制构造逻辑。

**判读**：

| 结果 | 含义 | 处理 |
|---|---|---|
| 全部 YES | 压缩算法未变 | 可以直接切换 |
| 部分 NO，但压缩率相近 | 上游改了算法或标记文字 | 人工检查输出可读性后决定 |
| **压缩率明显下降** | 算法退化 | 停止，回退 |
| **输出含乱码或截断错误** | 严重问题 | 停止，回退 |
| **retrieve 往返不一致** | 存储层问题 | 停止，回退 |

> **压缩率相近但 sha 不同是正常的**，不要只盯 sha。上游可能只改了头部提示文字。
> 关键看：压缩率是否相近、输出是否可读、原文能否完整取回。

### 4.6 retrieve 往返验证

```bash
python - <<'PY'
import json, urllib.request

API = "http://127.0.0.1:8799"
original = "..."  # 与压缩时相同的大文本

body = json.dumps({"text": original, "kind": "log"}).encode()
req = urllib.request.Request(API + "/compress", data=body, headers={"Content-Type": "application/json"})
res = json.loads(urllib.request.urlopen(req).read())
h = res["hash"]

back = urllib.request.urlopen(API + "/retrieve/" + h).read()
print("original bytes:", len(original.encode()))
print("retrieved bytes:", len(back))
print("byte-identical :", back == original.encode("utf-8"))
PY
```

**必须 byte-identical: True。**

### 4.7 真实代码文件验证（不能只用合成数据）

```bash
python - <<'PY'
import json, urllib.request, pathlib

API = "http://127.0.0.1:8799/compress"
FILES = [
    "/path/to/real_file_1.py",
    "/path/to/real_file_2.py",
    "/path/to/real_file_3.js",
]
for f in FILES:
    text = pathlib.Path(f).read_text(encoding="utf-8", errors="ignore")
    if len(text) < 8192:
        print(pathlib.Path(f).name + ": 太小跳过"); continue
    body = json.dumps({"text": text, "kind": "code"}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    res = json.loads(urllib.request.urlopen(req, timeout=120).read())
    print("%-34s %7d -> %7d  %.1f%%" % (
        pathlib.Path(f).name, res["in_chars"], res["out_chars"],
        100.0 * res["out_chars"] / res["in_chars"]))
PY
```

**为什么必须用真实代码**：合成数据（如重复的 def f(x): return x+1）会被 tree_sitter
判定为"无压缩价值"而原样返回，压缩率接近 100%。真实代码的压缩率是 **21%~46%**。
用合成数据会得出完全错误的结论（历史上因此误判过 tree_sitter 的作用）。

**判定**：真实代码压缩率应落在 15%~60% 区间。若接近 100%，说明 `tree_sitter_language_pack` 没生效。

### 4.8 清理临时环境

```bash
# 停临时 sidecar
netstat -ano | grep ":8799.*LISTENING"
taskkill /PID <PID> /F

# 删除临时 venv 和目录
rm -rf /tmp/hr_slim_test /tmp/hr_test_store /tmp/hr_test_ws /tmp/hr_upstream_venv
```

> **验证通过后再清理**。失败了还要用这些环境复现问题。

---

## 阶段 5：切换生产

**目标**：把生产环境换成新 wheel。**耗时**：约 20 分钟。

> 开始前先跑一次自检。`npm test` 需要 8797 上的 sidecar，必须在 5.1 停掉它之前执行：
>
> ```bash
> cd /e/AQYLPZ_WorkMenu/dsh_headroom
> npm run check && npm run check:py && npm test
> ```
>
> 另外建议先退出 DSH，再执行 5.1–5.8。DSH 运行期间插件可能在 venv 被移走/重建时
> 尝试拉起 sidecar，造成文件占用或拉起失败。

### 5.1 停 sidecar（必须杀进程树）

Windows 上 venv 的 `pythonw.exe` 是个启动器，会再拉起一个基础解释器。
**只杀父进程会留下子进程继续占着 8797 端口。**

```bash
# 找到监听 8797 的 PID
netstat -ano | grep ":8797.*LISTENING"

# 杀整棵进程树（/T 是关键）
taskkill /PID <PID> /T /F

# 确认端口释放
netstat -ano | grep ":8797.*LISTENING"     # 应无输出
```

### 5.2 备份当前 venv（留回滚能力）

```bash
cd /d/DSH-Home/.dsh/profiles/desktop/.headroom
mv venv venv-prev
```

> 若已有旧的 `venv-prev`，先删掉或改名（如 `venv-prev2`）。保留两份备份会占 1 GB 以上。

### 5.3 更新 requirements.txt

```diff
 --find-links ./vendor/wheels
-headroom-ai[code]==0.42.0.post1
+headroom-ai[code]==0.43.0.post1
```

### 5.4 重建 venv

```bash
cd /d/DSH-Home/.dsh/profiles/desktop/.headroom
uv venv venv --python 3.12
uv pip install --python venv/Scripts/python.exe \
  --find-links /e/AQYLPZ_WorkMenu/dsh_headroom/vendor/wheels \
  "headroom-ai[code]==0.43.0.post1"
```

> `requirements.txt` 里的 `--find-links ./vendor/wheels` 是**相对路径**（相对于该文件所在目录）。
> 从别的目录跑会找不到 wheel。显式传 `--find-links` 绝对路径最稳妥。

### 5.5 升插件版本号

插件版本与 Python 依赖版本是**两套独立编号**，都要升：

| 文件 | 字段 | 当前 |
|---|---|---|
| `package.json` | `version` | `0.1.3` |
| `sidecar/sidecar.py` | `_VERSION` | `0.1.3` |

> 若只是升级 Python 依赖、插件代码未变，可跳过 5.5–5.7（见 5.6 的情况 1），不需要升插件版本。

> 为什么 `sidecar.py` 也要改：`/health` 返回的 `version` 字段读这个常量。
> 不改的话无法从健康检查区分新旧环境，验收会受阻。

### 5.6 重新打包并安装插件

```bash
cd /e/AQYLPZ_WorkMenu/dsh_headroom

# 1. 静态检查（npm test 已在阶段 5 开始前跑过）
npm run check && npm run check:py

# 2. 打包（产物名取 package.json 的 name 字段，当前为 bale）
npm pack
# 产出 bale-0.1.7.tgz

# 3. 复制到 profile 的 vendor 目录
cp bale-0.1.7.tgz /d/DSH-Home/.dsh/profiles/desktop/vendor/

# 4. 按下面「包名与 profile 安装键」的说明修改 profile
```

> **包名与 profile 安装键（改名后必须二选一）**
>
> 仓库的 `package.json` name 已是 `bale`，`npm pack` 产出 `bale-0.1.7.tgz`。按本次是否改插件代码分两种情况：
>
> 1. **只升级 Python wheel，插件代码不变**：不需要执行 5.5–5.7，也不需要重新打包。
>    保留现有 headroom-auto 0.1.3 安装，只重建 venv（5.4）并重启 DSH。是否升级成功用
>    `python -c "import importlib.metadata as m; print(m.version('headroom-ai'))"` 确认。
> 2. **改了插件代码或要升插件版本**：推荐同时落地 bale 改名——把 profile `package.json`
>    的依赖键与 `dsh.profile.bundles`、`cordis.patch.yml` 的 `id`/`name`、`sidecarScript`
>    路径一起改为 `bale`，使 tgz 名与安装目录一致。
>
> **不建议只把 `package.json` 的 name 临时改回 `headroom-auto` 再打包**：tgz 内还带有
> `cordis.patch.yml` 的 id 和 `lib/index.js` 的导出名（均为 `bale`），与 profile 的
> `headroom-auto` id 不一致，可能导致插件重复加载或配置不生效。若确实要保留旧身份，
> 应使用改名前的 `1e13aa3` 提交打包，而不是从当前工作区临时改名。

### 5.7 pnpm install 有陷阱

```bash
cd /d/DSH-Home/.dsh/profiles/desktop
pnpm install
```

> **绝对不要在 ops_bash / 任何 dsh-ops 提供的 shell 里执行这条命令。**
>
> 原因：`ops_bash` 运行在 `node_modules/dsh-ops/runtime/winuxcmd/bin/bash.exe` 上。
> `pnpm install` 需要替换 `node_modules/`，会尝试删除正在运行的 `dsh-ops` 目录，
> Windows 拒绝后中途失败，**dsh-ops 会被删坏**（历史上真实发生过）。
>
> 正确做法：用系统的 PowerShell / cmd，或从普通终端执行。

### 5.8 起 sidecar 并确认

```bash
# 重启 DSH，让插件自动拉起 sidecar；或手动临时起
/d/DSH-Home/.dsh/profiles/desktop/.headroom/venv/Scripts/pythonw.exe -I \
  /e/AQYLPZ_WorkMenu/dsh_headroom/sidecar/sidecar.py --port 8797 &

sleep 3
curl -s http://127.0.0.1:8797/health
```

**必须看到 `ok: true`、正确的 `version`、`disabled_kinds: ["text"]`、以及 `store` 保留了旧数据。**

`compressors_loaded` 为空是正常的（懒加载）。

> 上面的手动命令用的是仓库内 `sidecar.py`（内容与安装包一致）；正式部署后插件会按 profile 的
> `sidecarScript` 路径自动拉起，以安装目录中的脚本为准。

---

## 阶段 6：端到端验收

**目标**：确认生产环境完全正常。**耗时**：约 10 分钟。

### 6.1 逐项检查

生产 venv 路径记为 `VENV=D:/DSH-Home/.dsh/profiles/desktop/.headroom/venv`。

| # | 检查项 | 命令 | 通过标准 |
|---|---|---|---|
| 1 | 健康 | `curl -s http://127.0.0.1:8797/health` | ok:true，version 为新插件版本 |
| 2 | 压缩器 | 跑一次各类型压缩后再查 /health | 6 个全在 compressors_loaded |
| 3 | 依赖一致 | `uv pip check --python $VENV/Scripts/python.exe` | All installed packages are compatible |
| 4 | 五类型压缩 | `$VENV/Scripts/python.exe 阶段C-基线对拍.py` | RESULT: ALL PASS（或 MISMATCH 但压缩率相近，需人工判断） |
| 5 | retrieve 往返 | 阶段 4.6 | byte-identical: True |
| 6 | 真实代码 | 阶段 4.7 | 压缩率 15%~60% |
| 7 | 旧 store 兼容 | 阶段 6.2 | 全部 match=True |
| 8 | 插件加载 | 重启 DSH 后看日志 | 有 apply 与 ensure-sidecar: ok:true |
| 9 | 端到端脚本 | `$VENV/Scripts/python.exe 阶段D-端到端验证.py` | RESULT: ALL PASS |

两个验证脚本的完整路径：
```
E:/AQYLPZ_WorkMenu/工作脚本/headroom插件/依赖精简/阶段C-基线对拍.py
E:/AQYLPZ_WorkMenu/工作脚本/headroom插件/依赖精简/阶段D-端到端验证.py
```

> `阶段C-基线对拍.py` 直接 import `headroom` 调压缩器，所以**必须用目标 venv 的 python 跑**，
> 用它来验证哪个环境，就验的是哪个环境。`阶段D-端到端验证.py` 走 HTTP，验的是 8797 上的 sidecar。

### 6.2 旧 store 兼容性验证

升级不应影响已有原文仓库。验证方法：

```bash
python - <<'PY'
import urllib.request, pathlib

STORE = pathlib.Path(r"D:/DSH-Home/.dsh/profiles/desktop/.headroom/store")
bins = sorted(STORE.glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]

for p in bins:
    h = p.stem
    try:
        got = urllib.request.urlopen("http://127.0.0.1:8797/retrieve/" + h, timeout=30).read()
        exp = p.read_bytes()
        print("%s  %8d bytes  match=%s" % (h, len(got), got == exp))
    except Exception as e:
        print("%s  ERROR %s" % (h, e))
PY
```

**全部 match=True 才算通过。**

### 6.3 观察期

升级后建议观察 **1–3 天**，重点关注：

```bash
python "E:/AQYLPZ_WorkMenu/工作脚本/headroom插件/迁移验证/监控headroom效果.py" --days 1
```

| 指标 | 正常范围 | 异常信号 |
|---|---|---|
| 净节省率 | > 30% | 持续 < 30% → 取回成本过高 |
| 取回/压缩 | < 0.5 | > 0.5 → 压缩幅度过大 |
| 错误事件数 | 0 | 任何 downstream-error / passthrough-error |
| 平均耗时 | < 100 ms | 明显变慢 → 算法或加载出问题 |

---

## 12. 回滚方案

### 12.1 快速回滚（环境层）

```bash
# 1. 停 sidecar
netstat -ano | grep ":8797.*LISTENING"
taskkill /PID <PID> /T /F

# 2. 换回旧 venv
cd /d/DSH-Home/.dsh/profiles/desktop/.headroom
rm -rf venv
mv venv-prev venv

# 3. 重启 DSH
```

### 12.2 完整回滚（含代码）

```bash
cd /e/AQYLPZ_WorkMenu/dsh_headroom

# 恢复 requirements.txt 与 wheels
git checkout requirements.txt
rm -rf vendor/wheels
mv vendor/wheels.prev vendor/wheels

# 恢复 repack.py（如果改过）
git checkout tools/repack/

# 恢复插件版本号
git checkout package.json sidecar/sidecar.py

# 重新打包并安装
npm pack && cp *.tgz /d/DSH-Home/.dsh/profiles/desktop/vendor/
# 然后在系统终端里 pnpm install
```

### 12.3 回滚时机

| 阶段 | 是否已影响生产 | 回滚动作 |
|---|---|---|
| 0–4 | 否 | 直接删临时目录，git checkout 恢复文件 |
| 5.1–5.4 | venv 已换 | mv venv-prev venv |
| 5.5–5.7 | 插件已换 | 上述 + 恢复 tgz + pnpm install |
| 6 之后 | 已上线 | 上述 + 观察日志确认回滚成功 |

**保留 `venv-prev` 至少一周**，确认稳定后再删。

---

## 13. 特殊场景

### 13.1 上游把 litellm 移出了 base 依赖（最佳情况）

这说明上游采纳了合理建议。此时**不需要重打包**。

判断方法（阶段 1.4 的问题 2）：

```bash
grep '^Requires-Dist:' METADATA | grep -v 'extra ==' | grep litellm
# 无输出 = litellm 已不在 base 依赖里
```

处理步骤：

1. `requirements.txt` 改为 `headroom-ai[code]==X.Y.Z`（去掉 `--find-links`）
2. 删掉 `vendor/wheels/`（或保留作历史）
3. `tools/repack/` 可保留但标记为"已停用"并写明原因
4. 更新 `README.md` 的「依赖精简说明」章节，改为"上游已修复，本项目回归官方包"
5. 更新本文档，在开头加一段说明"本流程已不再需要"

**仍需执行阶段 2 和阶段 4（跳过阶段 3 的重打包）**，验证官方包的压缩行为与当前是否一致。
阶段 4.1 的安装命令改为：

```bash
uv pip install --python /tmp/hr_slim_test/Scripts/python.exe "headroom-ai[code]==X.Y.Z"
```

不加 `--find-links`，也不用 `.post1` 版本；其余验证步骤不变。

### 13.2 上游改了 wheel 结构

症状：`repack.py` 报 `source RECORD does not cover exactly the archive entries` 或 dist-info 命名找不到。

处理：

1. 解压新 wheel，对比目录结构
2. 检查 dist-info 目录名格式（是否仍是 {name}-{version}.dist-info）
3. 检查 RECORD 是否存在、格式是否仍为 path,hash,size
4. 检查 METADATA 换行风格（脚本按 LF 处理）
5. 相应修改 `OLD_DIST`、`parse_record()`、`split_lf_keepends()` 的逻辑

**改脚本后必须重跑 verify_output 的全部断言。**

### 13.3 上游新增了重量级依赖

症状：阶段 2.2 的加载列表里出现新的包，或安装后 dist 数量明显增多。

处理：

1. 用阶段 2.3 的方法测试能否屏蔽
2. 能屏蔽 → 不加进 `REQUIRED`
3. 不能屏蔽 → 加进 `REQUIRED`，并在 `docs/dependency-slimming-plan.md` 记录原因

### 13.4 上游的压缩算法变了

症状：阶段 4.5 显示部分或全部 MISMATCH。

处理：

1. **先看压缩率**，不看 sha。压缩率相近就是可接受的
2. 检查输出可读性：头部标记是否完整、内容是否被破坏
3. 用真实代码文件复验（阶段 4.7）
4. 如果压缩率变差但仍可接受 → 记录差异，继续
5. 如果压缩率明显变差或输出有误 → 不升级，等上游修复

### 13.5 只想升级插件而不升级 Python 依赖

两者独立。直接在 `dsh_headroom` 改代码、升 `package.json` 版本、`npm pack`、装即可，**不需要动 venv**。

---

## 14. 常见故障排查

| 症状 | 可能原因 | 排查方法 | 处理 |
|---|---|---|---|
| uv pip check 报缺包 | REQUIRED 漏了某包 | 看报错里的包名 | 加进 REQUIRED，重新打包 |
| json 压缩 ImportError | httpx 缺失 | grep httpx REQUIRED | 补上 httpx>=0.24.0 |
| code 压缩率接近 100% | tree_sitter 未生效 | 检查 dist 里有无 tree_sitter_language_pack | 补进 REQUIRED |
| sidecar 起不来 | 端口占用 / Python 路径错 | 看 /health、查 logs/sidecar.log | 杀进程树重启 |
| /health version 不对 | _VERSION 没改 | grep _VERSION sidecar.py | 改常量并重启 |
| 旧 hash 取回 404 | store 被清或路径变了 | 检查 storeDir 配置与目录 | 恢复目录或调整配置 |
| pnpm install 失败且 dsh-ops 损坏 | 在 ops_bash 里执行 | 检查 node_modules/dsh-ops 结构 | 用系统 shell 重装；dsh-ops 需从 registry 重下 |
| 压缩超时 | timeoutMs 太短或算法变慢 | 看日志里的 ms 字段 | 调大 timeoutMs（默认 15000） |
| 插件加载后无 apply 日志 | 插件没被 DSH 加载 | 查 profile 的 cordis.patch.yml | 检查 id/name 是否匹配 |

---

## 15. 附录

### 15.1 repack.py 命令行参考

```bash
# 基本用法
python tools/repack/repack.py --src <上游 wheel 路径>

# 指定输出目录
python tools/repack/repack.py --src <wheel> --out-dir /tmp/output

# 查看帮助
python tools/repack/repack.py --help
```

### 15.2 关键路径速查

| 项目 | 路径 |
|---|---|
| 仓库 | E:\AQYLPZ_WorkMenu\dsh_headroom |
| venv | D:\DSH-Home\.dsh\profiles\desktop\.headroom\venv |
| store（原文仓库） | D:\DSH-Home\.dsh\profiles\desktop\.headroom\store |
| 日志 | D:\DSH-Home\.dsh\profiles\desktop\.headroom\logs\headroom-auto.log |
| profile 配置 | D:\DSH-Home\.dsh\profiles\desktop\cordis.patch.yml |
| profile 依赖 | D:\DSH-Home\.dsh\profiles\desktop\package.json |
| 插件安装目录 | D:\DSH-Home\.dsh\profiles\desktop\node_modules\headroom-auto |
| sidecar 端口 | 127.0.0.1:8797 |

> **注意：仓库已改名为 bale（2026-11-09），但部署侧尚未跟进。**
> 上表中的日志文件名、插件安装目录、profile 依赖名仍是 headroom-auto，
> 因为数据目录与安装目录的迁移被有意推迟（见任务文档）。
> 若你正在部署 bale（改名第一步落地），插件安装目录与 profile 依赖名会同步变更；
> 数据目录与工具名的第二步另见交接文档。日志文件名仍保持 headroom-auto.log。

### 15.3 相关文档

| 文档 | 位置 | 内容 |
|---|---|---|
| 依赖精简方案 | docs/dependency-slimming-plan.md | 为什么重打包、基线、五个陷阱 |
| 重打包说明 | tools/repack/README.md | Apache-2.0 修改声明、操作要点 |
| 项目总览与交接 | 工作脚本 目录 | 整体状态、已知陷阱、运维手册 |
| 上游接口说明 | headroom-auto-项目介绍.md | HTTP 端点、配置项详解（文件名沿用旧名） |

### 15.4 版本历史

| 上游版本 | 本地 post 版本 | 日期 | 备注 |
|---|---|---|---|
| 0.42.0 | 0.42.0.post1 | 2026-11-08 | 首次重打包，venv 632 → 240 MB |

---

*本文档编写于 2026-11-09，基于 0.42.0 的实测经验。上游更新后请同步更新本文档中的版本号与体积数据。*
