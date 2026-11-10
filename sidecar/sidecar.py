"""headroom sidecar：本机工具输出压缩服务（只绑 127.0.0.1:8797）。

接口：
  POST /compress  {"text": str} 或 {"path": str}
                   可选 {"kind": "log|json|code|text", "language": str}
        -> {"hash","kind","in_chars","out_chars","ms","ratio","compressed","text"}
  POST /retrieve  {"hash": str}   或   GET /retrieve/<hash>
        -> 原始字节 (application/octet-stream)；miss 时 404
  GET  /health    -> {"ok","version","uptime_s","compressors_loaded","text_model_ready"}

设计要点：
  - 单进程常驻，压缩器懒加载 + 常驻；全局锁串行压缩（CPU 密集，避免并发争抢）
  - 原文按【原始字节】存 store/<sha256[:16]>.bin —— UTF-16/GBK 日志取回字节一致
  - 无收益（out >= in）不替换，返回 compressed:false
  - kind 自动判定：JSON 可解析 -> json；时间戳行占比 >= 30% -> log；否则 text
  - Kompress(text) 需要 ONNX 模型；缺模型时该类型直接放行（compressed:false），不报错
  - 任何内部异常 -> 500 + message，调用方插件负责 fail-open
  - 输入上限 32MB（防巨量输出卡死），超出 -> 413

用法：venv/Scripts/python.exe -I sidecar/sidecar.py [--port 8797]
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent      # sidecar/（真实部署时在 node_modules 内）
# 运行时数据目录可用环境变量覆盖（隔离测试 / 迁移到其他盘）。
# 迁移后在 profile 的 cordis.patch.yml 里通过 storeDir / wsDir 下发，
# 真实位置是 <profile>/.headroom/store 与 <profile>/.headroom/headroom_ws。
STORE = pathlib.Path(os.environ.get("HEADROOM_STORE_DIR") or (ROOT / "store"))
WS = pathlib.Path(os.environ.get("HEADROOM_WORKSPACE_DIR") or (ROOT / "headroom_ws"))
LOGF = pathlib.Path(os.environ.get("HEADROOM_LOG_DIR") or ROOT) / "sidecar.log"
# HF 模型缓存：真实部署时必须落在 .headroom/ 下，绝不能默认到 node_modules
# （pnpm 更新会整目录替换，264MB 模型会丢；且会污染受版本控制的插件目录）。
# 优先级：HEADROOM_HF_HOME > STORE 同级 hf_home（真实部署）. > 插件根 hf_home（测试环境）
HF_HOME = pathlib.Path(
    os.environ.get("HEADROOM_HF_HOME")
    or (STORE.parent / "hf_home" if os.environ.get("HEADROOM_STORE_DIR") else ROOT.parent / "hf_home")
)
MAX_INPUT = 33554432  # 32MB

# 已停用的压缩类型（逗号分隔）。text 型 Kompress 实测压缩比 ~97%（几乎无收益），
# 单次耗时 8-27 秒且频繁撞上插件 15s 超时，纯粹贡献延迟。默认停用 text。
# 置空字符串即可恢复：HEADROOM_DISABLED_KINDS=""
DISABLED_KINDS = {
    k.strip().lower()
    for k in os.environ.get("HEADROOM_DISABLED_KINDS", "text").split(",")
    if k.strip()
}

# headroom 运行环境 —— 必须在 import headroom 之前设置（离线变量与镜像）
os.environ.update({
    "HF_HOME": str(HF_HOME),
    "HF_HUB_CACHE": str(HF_HOME / "hub"),
    "HF_ENDPOINT": "https://hf-mirror.com",
    "HF_HUB_OFFLINE": "0",
    "TRANSFORMERS_OFFLINE": "0",
    "KOMPRESS_EXECUTION_TIMEOUT_MS": "180000",   # 默认 20s 会中断长文本
    "HEADROOM_WORKSPACE_DIR": str(WS),
})
STORE.mkdir(parents=True, exist_ok=True)
WS.mkdir(parents=True, exist_ok=True)

_compressors = {}      # kind -> callable(text, language) -> str
_comp_lock = threading.RLock()   # 可重入：do_compress 的压缩临界区内会再进 _get_compressor
_started = time.time()
_VERSION = "0.1.6"

_TS_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}|\d{2}:\d{2}:\d{2}")


def log(evt):
    line = json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **evt}, ensure_ascii=False, default=str)
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_SCRIPT_PATH = os.path.abspath(__file__)
_SCRIPT_HASH = ""
_WATCH_SECS = 10.0


def script_hash():
    try:
        with open(_SCRIPT_PATH, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def watch_script():
    """脚本被 pnpm 更新替换后主动退出，让插件拉起新代码。"""
    while True:
        time.sleep(_WATCH_SECS)
        current = script_hash()
        if not current or current == _SCRIPT_HASH:
            continue
        # 避开解压到一半的中间状态：1 秒后仍然不同才认定更新完成
        time.sleep(1)
        again = script_hash()
        if again and again != _SCRIPT_HASH:
            log({"event": "self-exit", "reason": "script-changed", "pid": os.getpid()})
            os._exit(0)


def _get_compressor(kind):
    """懒加载并常驻一个压缩器，返回 callable(text, language) -> str。"""
    if kind in _compressors:
        return _compressors[kind]
    with _comp_lock:
        if kind in _compressors:
            return _compressors[kind]
        if kind == "log":
            from headroom.transforms.log_compressor import LogCompressor
            c = LogCompressor()
            fn = lambda text, language=None: c.compress(text).compressed
        elif kind == "json":
            from headroom import SmartCrusher
            c = SmartCrusher()
            fn = lambda text, language=None: c.crush(text).compressed
        elif kind == "code":
            from headroom.transforms.code_compressor import CodeAwareCompressor
            c = CodeAwareCompressor()
            fn = lambda text, language=None: c.compress(text, language=language).compressed
        elif kind == "text":
            from headroom.transforms.kompress_compressor import KompressCompressor
            c = KompressCompressor()
            fn = lambda text, language=None: c.compress(content=text).compressed
        elif kind == "diff":
            # git diff 压缩（Rust 实现，实测 ratio 0.72）
            from headroom.transforms.diff_compressor import DiffCompressor
            c = DiffCompressor()
            fn = lambda text, language=None: c.compress(text).compressed
        elif kind == "search":
            # grep / 代码搜索结果压缩（Rust 实现，实测 ratio 0.88）
            from headroom.transforms.search_compressor import SearchCompressor
            c = SearchCompressor()
            fn = lambda text, language=None: c.compress(text).compressed
        elif kind == "generic":
            # 结构化无关的大文本（文件列表、代码块、文档、混合输出）。
            # 少行超长内容走字符级切片，其余走行级抽样。
            fn = lambda text, language=None: compress_generic(text)
        else:
            raise ValueError(f"unknown kind: {kind}")
        _compressors[kind] = fn
        log({"event": "compressor-loaded", "kind": kind})
        return fn


def kompress_model_ready():
    """探测 Kompress ONNX 模型是否已缓存（不触发加载）。"""
    base = HF_HOME / "hub" / "models--chopratejas--kompress-v2-base" / "snapshots"
    if not base.exists():
        return False
    return any(list(snap.glob("onnx/*.onnx")) for snap in base.iterdir())


def decode_bytes(raw: bytes) -> str:
    """按 BOM/UTF-8/GBK 嗅探解码（Windows 日志常是 UTF-16 LE / GBK）。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# 项目自带的轻量检测器（纯正则，0.1-2ms），用于识别 diff / search 这两类
# 我们原来完全没有覆盖、但实际高频的工具输出。刻意不用 ContentRouter 整体：
# 它在这台 Windows 上只能走纯 Python 后端，且 mixed 分段实测几乎无收益。
try:
    from headroom.transforms.content_detector import detect_content_type as _detect_content_type
    from headroom.transforms.content_detector import ContentType as _ContentType
    _DETECTOR_AVAILABLE = True
except Exception:  # 检测器不可用则退回原启发式
    _DETECTOR_AVAILABLE = False


def _detect_special(text: str) -> str | None:
    """用项目的正则检测器识别 diff / search，识别不出返回 None。"""
    if not _DETECTOR_AVAILABLE:
        return None
    try:
        d = _detect_content_type(text)
    except Exception:
        return None
    if d.confidence < 0.8:      # 置信度不足不冒险
        return None
    if d.content_type is _ContentType.GIT_DIFF:
        return "diff"
    if d.content_type is _ContentType.SEARCH_RESULTS:
        return "search"
    return None


_WEB_FETCH_MARKER = "External web content follows."


def _strip_web_fetch_wrapper(text: str) -> str:
    """剥离 web_fetch 的已知头部，只用于类型检测；压缩输入仍是完整原文。"""
    if not text.lstrip().startswith("Fetched http"):
        return text
    idx = text.find(_WEB_FETCH_MARKER)
    if idx < 0:
        return text
    rest = text[idx + len(_WEB_FETCH_MARKER):]
    nl = rest.find("\n")
    if nl >= 0:
        rest = rest[nl + 1:]
    return rest.lstrip("\r\n")


def detect_kind(text: str) -> str:
    probe = _strip_web_fetch_wrapper(text)
    stripped = probe.strip()
    if not stripped:
        return "generic"

    # 先看是不是 git diff / grep 搜索结果（这是我们原先漏掉的两类高频输出）
    special = _detect_special(probe)
    if special:
        return special

    if stripped[0] in "[{":
        try:
            json.loads(stripped)
            return "json"
        except Exception:
            pass
    lines = [ln for ln in probe.splitlines() if ln.strip()]
    if len(lines) >= 5:
        hits = sum(1 for ln in lines if _TS_RE.search(ln))
        if hits / len(lines) >= 0.3:
            return "log"
    # 不是 log/json/diff/search 的大文本 → generic（行级抽样），
    # 而不是 text（text 走 Kompress，已停用；且 Kompress 在本机压不动）。
    return "generic"


# ── 关键行识别（保证折叠不丢关键信息）───────────────────────────────────
# 纯头尾抽样会丢掉中间的关键行：实测 630 行 git diff 压成 122 行后，
# 15 个文件的改动只剩 2 个；100 条 ValueError 只剩 30 条。
# 因此折叠前先扫一遍"必须保留"的行：
_CRITICAL_PATTERNS = (
    # diff 的改动行("-内容"/"+内容")只在 diff 上下文内才算关键行。
    # 纯正则无法区分：ls 的 "-rwxr-xr-x ..." 也是 "-" 开头，实测 300 行 ls
    # 全部被误判为改动行，把关键行预算吃光。因此这条规则改为**上下文相关**，
    # 由 extract_critical_line_nums 内部判断"是否处在 diff 块中"。
    (re.compile(r"^[-+]"), "diff-change"),
    (re.compile(r"^diff --git"), "diff-file"),               # 文件起始
    (re.compile(r"^@@\s"), "diff-hunk"),                     # hunk 头
    (re.compile(r"\b(ERROR|FATAL|CRITICAL|SEVERE|PANIC)\b", re.I), "error"),
    (re.compile(r"\b(FAIL|FAILED|FAILURE|BROKEN)\b", re.I), "fail"),
    (re.compile(r"\b(Traceback|Exception|Error:|AssertionError)\b"), "exception"),
    (re.compile(r'^\s*at\s+[\w.$]+\(|^\s*File "'), "stack-frame"),
    (re.compile(r"\b(warning|WARN)\b", re.I), "warn"),
    (re.compile(r"\bexit code:\s*[1-9]"), "nonzero-exit"),
)

_CRIT_BUDGET = int(os.environ.get("HEADROOM_CRIT_BUDGET", "200"))
_CRIT_MAX_LINE_CHARS = int(os.environ.get("HEADROOM_CRIT_MAX_LINE", "2000"))


_DIFF_START_RE = re.compile(r"^(diff --git|diff --combined|diff --cc|Index: )")
_HUNK_RE = re.compile(r"^@@\s")


def extract_critical_line_nums(lines: list[str], budget: int | None = None) -> list[int]:
    """返回"关键行"的行号（0-based），最多 budget 条。

    对 diff 改动行做**上下文判定**：只有当此前出现过 diff 头（`diff --git` 或
    `@@`）且当前仍在 diff 块内时，`-`/`+` 开头的行才算关键行。这样
    `ls -la` 的 `-rwxr-xr-x ...` 不会被误判（实测 300 行 ls 曾全部误判）。
    """
    budget = _CRIT_BUDGET if budget is None else budget
    picked: list[int] = []
    in_diff = False

    for idx, ln in enumerate(lines):
        if not ln.strip():
            continue
        if len(ln) > _CRIT_MAX_LINE_CHARS:
            continue

        # 维护 diff 上下文状态
        if _DIFF_START_RE.match(ln):
            in_diff = True
            picked.append(idx)
            if len(picked) >= budget:
                break
            continue
        if _HUNK_RE.match(ln):
            in_diff = True
            picked.append(idx)
            if len(picked) >= budget:
                break
            continue

        matched = None
        for pat, kind in _CRITICAL_PATTERNS:
            if kind == "diff-change":
                # 只有处在 diff 块中才算
                if in_diff and pat.search(ln):
                    matched = kind
                continue
            if pat.search(ln):
                matched = kind
                break

        if matched:
            picked.append(idx)
            if len(picked) >= budget:
                break

    return picked


# ── 行级抽样（保结构压缩，用于 generic / log 兜底）───────────────────────
# 为什么不用 LogCompressor：
#   实测 LogCompressor 对纯 INFO 日志、文件列表、代码、CSV 都会把内容清零，
#   只输出 "[400 lines omitted]\n[400 lines compressed to 0...]"（93 字符），
#   模型拿到后完全看不到内容 —— 压缩比好看但信息全丢。
# 为什么不用 TextCrusher：
#   抽句式压缩会把代码、Markdown 结构打散（实测 seq=1112 的代码被切成碎片）。
# 行级抽样只做结构裁剪：保留头 N 行 + 尾 M 行 + 一条明确的折叠标记，
# 不做任何语义判断，因此不会改变保留下来的每一行的字节。
_DEFAULT_HEAD_LINES = int(os.environ.get("HEADROOM_SAMPLE_HEAD", "80"))
_DEFAULT_TAIL_LINES = int(os.environ.get("HEADROOM_SAMPLE_TAIL", "40"))
_SAMPLE_MIN_OMIT = int(os.environ.get("HEADROOM_SAMPLE_MIN_OMIT", "20"))
_SAMPLE_MIN_CHARS = int(os.environ.get("HEADROOM_SAMPLE_MIN_CHARS", "8192"))


def sample_lines(
    text: str,
    head: int | None = None,
    tail: int | None = None,
    min_omit: int | None = None,
    min_chars: int | None = None,
    crit_budget: int | None = None,
) -> tuple[str, int]:
    """保留【头 N 行 + 中间关键行 + 尾 M 行】，折叠其余。返回 (输出文本, 省略行数)。

    为什么不是纯头尾：
        实测 630 行 git diff 压成 122 行后，15 个文件的改动只剩 2 个；
        100 条 ValueError 只剩 30 条。纯头尾会丢中间的关键信息。

    安全性约定：
    * 不做任何语义改写，保留的行字节不变（含 CRLF）；
    * 内容短于 min_chars 不动；
    * 行数不够 head+tail+min_omit 不动（避免"压完还加标记，反而更长"）；
    * 关键行按原顺序插入，且标注其原始行号，便于模型定位；
    * 折叠标记显式写明省略行数与原文规模。
    """
    head = _DEFAULT_HEAD_LINES if head is None else head
    tail = _DEFAULT_TAIL_LINES if tail is None else tail
    min_omit = _SAMPLE_MIN_OMIT if min_omit is None else min_omit
    min_chars = _SAMPLE_MIN_CHARS if min_chars is None else min_chars

    if len(text) < min_chars:
        return text, 0

    lines = text.splitlines(keepends=True)
    n = len(lines)
    if n <= head + tail + min_omit:
        return text, 0

    head_range = set(range(min(head, n)))
    tail_range = set(range(max(n - tail, 0), n))

    # 中间区域里"必须保留"的关键行
    mid_lines = [ln.rstrip("\r\n") for ln in lines]
    crit = [
        i for i in extract_critical_line_nums(mid_lines, crit_budget)
        if i not in head_range and i not in tail_range
    ]

    kept = head_range | set(crit) | tail_range
    omitted = n - len(kept)
    if omitted <= 0:
        return text, 0

    out_parts: list[str] = []
    last_idx = -2
    pending_gap = 0
    for i in sorted(kept):
        if i != last_idx + 1:
            gap = i - last_idx - 1
            if last_idx >= 0 and gap > 0:
                pending_gap += gap
            # 头段与后面的内容之间不一定有 gap
        if pending_gap:
            out_parts.append(
                "[headroom] …省略 %d 行…\n" % pending_gap
            )
            pending_gap = 0
        ln = lines[i]
        # 【不改动行内容】关键行必须逐字保留，不能加行号前缀：
        # diff 的 "-"/"+" 必须在行首，加任何前缀都会破坏语义；
        # 日志行加了前缀也可能被模型当成内容的一部分。
        out_parts.append(ln if ln.endswith(("\n", "\r")) else ln + "\n")
        last_idx = i

    # 尾部剩余省略
    if last_idx < n - 1:
        pending_gap += n - 1 - last_idx
    if pending_gap:
        out_parts.append("[headroom] …省略 %d 行…\n" % pending_gap)

    # summary 放在**开头**而不是末尾：保证输出最后一行就是原文的最后一行，
    # 模型才能可靠地从末尾读取 [exit code: N] / 堆栈结尾等关键信息。
    summary = (
        "[headroom] 压缩视图：原文 %d 行 / %d 字符，保留 %d 行"
        "（头 %d + 关键行 %d + 尾 %d，其余 %d 行已折叠）。"
        "保留行与原文逐字一致；需要完整内容请调用 headroom_retrieve。\n"
        "%s"
        % (n, len(text), len(kept), len(head_range), len(crit), len(tail_range),
           omitted, "-" * 60 + "\n")
    )

    # 去掉尾部多余空行：输出最后一行必须是原文的最后一行（模型靠此读 [exit code: N]）
    return summary + "".join(out_parts).rstrip("\r\n"), omitted


def _char_slice(text: str) -> str:
    """字符级切片：保留首 30% + 尾 20%，中间显式写明省略字符数。"""
    head = int(len(text) * 0.3)
    tail = int(len(text) * 0.2)
    if head <= 0 or tail <= 0 or head + tail >= len(text):
        return text
    omitted = len(text) - head - tail
    return text[:head] + f"\n…已省略 {omitted} 字符…\n" + text[-tail:]


def compress_generic(text: str) -> str:
    """generic 入口：少行超长内容不走行级抽样，改字符级切片。

    单行代码/压缩 bundle 的 ;{}() 密度高，切片会切断 token，退回行级抽样。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    max_line = max((len(ln) for ln in lines), default=0)
    if (len(lines) < _DEFAULT_HEAD_LINES + _DEFAULT_TAIL_LINES + _SAMPLE_MIN_OMIT
            and max_line > 2000):
        longest = max(lines, key=len)
        code_chars = sum(longest.count(c) for c in ";{}()")
        if code_chars / max(len(longest), 1) > 0.01:
            return sample_lines(text)[0]
        return _char_slice(text)
    return sample_lines(text)[0]


# ── 告警行保留（补回被 dedupe/截断吃掉的疑似有问题行）────────────────────
# 告警补回：
#  - 预算不够时**显式说明**还有几条没补回（原实现静默 break，信息无声消失）
#  - 文案区分"计划补回"与"实际补回"（原实现把计划数写成已补回）
#  - 完全重复的告警行单独给出频次汇总（原实现只留 1 条且不提频次）
_NOTABLE_RE = re.compile(r"\b(ERROR|FATAL|CRITICAL|FAIL(?:ED|URE)?|WARN(?:ING)?)\b")
_MAX_RESTORE = 40          # 单次最多补回的行数
_MAX_RESTORE_CHARS = 6000  # 补回总字符上限（含标记行）


def _restore_dropped_notables(src: str, out: str) -> str:
    """把源文本中带 WARN/ERROR 等告警级别、但压缩输出里已丢失的行补回末尾。

    设计要点：
    * 只补"级别行"，INFO 等噪声仍由压缩器裁掉；
    * 去重后补回，完全重复的行按频次汇总（不再无声丢弃）；
    * 任何因预算/条数限制未能补回的行，都在结尾**明写数量**，不静默吃掉。
    """
    src_lines = src.splitlines()
    out_set = set(out.splitlines())

    dropped: list[str] = []          # 需要补回的（不同内容，首次出现）
    repeat_counts: dict[str, int] = {}   # 已在正文出现、但源里还有更多次的
    seen: set[str] = set()

    for ln in src_lines:
        s = ln.strip()
        if not s:
            continue
        if not _NOTABLE_RE.search(s):
            continue
        if s in seen:
            # 完全重复的行：如果是被压缩掉的，记频次；正文里已有的不必再记
            if s in repeat_counts:
                repeat_counts[s] += 1
            elif s not in out_set:
                repeat_counts[s] = 2   # 首次在内已计入，这里是第 2 次
            continue
        seen.add(s)
        if s in out_set:
            # 正文里已经有这条一次，记录源里额外的次数
            continue
        dropped.append(ln)

    # 统计完全重复且被省略的次数（源里出现次数 - 正文保留次数）
    src_counter: dict[str, int] = {}
    for ln in src_lines:
        s = ln.strip()
        if s and _NOTABLE_RE.search(s):
            src_counter[s] = src_counter.get(s, 0) + 1
    out_counter: dict[str, int] = {}
    for ln in out_set:
        s = ln.strip()
        if s and _NOTABLE_RE.search(s):
            out_counter[s] = out_counter.get(s, 0) + 1
    omitted_repeats = {
        s: (src_counter[s] - out_counter.get(s, 0))
        for s in src_counter
        if src_counter[s] - out_counter.get(s, 0) > 0 and s not in {d.strip() for d in dropped}
    }

    if not dropped and not omitted_repeats:
        return out

    # ── 预算分配 ────────────────────────────────────────────────
    block: list[str] = []
    used = 0
    cut_by_chars = 0
    for ln in dropped[:_MAX_RESTORE]:
        if used + len(ln) + 1 > _MAX_RESTORE_CHARS:
            cut_by_chars += 1
            continue
        block.append(ln)
        used += len(ln) + 1
    cut_by_count = max(0, len(dropped) - _MAX_RESTORE)

    lines_out: list[str] = []
    if block:
        lines_out.append(
            "[告警保留] 以下 %d 行 WARN/ERROR 在压缩中被省略，已从原文补回（非原文顺序）:"
            % len(block)
        )
        lines_out.extend(block)

    # 明确交代"没补回来的"
    not_restored = cut_by_chars + cut_by_count
    if not_restored:
        reasons = []
        if cut_by_chars:
            reasons.append("超出 [告警保留] 字符预算")
        if cut_by_count:
            reasons.append("超出单次 %d 条上限" % _MAX_RESTORE)
        lines_out.append(
            "[告警保留] 另有 %d 行告警/错误**未能补回**（%s），如需完整内容请用 headroom_retrieve 取原文。"
            % (not_restored, "；".join(reasons))
        )

    # 完全重复的告警行给出频次
    if omitted_repeats:
        top = sorted(omitted_repeats.items(), key=lambda kv: -kv[1])[:10]
        lines_out.append("[告警保留] 以下完全重复的告警行被合并，标注为省略次数:")
        for s, cnt in top:
            preview = s if len(s) <= 160 else s[:157] + "..."
            lines_out.append("  ×%d  %s" % (cnt, preview))
        if len(omitted_repeats) > 10:
            lines_out.append("  …另有 %d 种重复告警未列出" % (len(omitted_repeats) - 10))

    if not lines_out:
        return out
    return (out.rstrip("\n") + "\n\n" + "\n".join(lines_out)) if out else "\n".join(lines_out)


# ── store 淘汰──────────────────────────────────────
# store 之前只增不删：一次 5MB 日志就永久占 5MB。现在按"总量上限 + TTL"双约束清理。
# 语义取舍：
#   * 淘汰后对应 hash 的 /retrieve 会 404，模型侧表现为"原文已过期"；
#   * 因此 TTL 默认给得很宽松（7 天），容量上限也留足（256MB）；
#   * 手动置 HEADROOM_STORE_MAX_MB=0 可完全关闭淘汰（恢复旧行为）。
_STORE_MAX_MB = int(os.environ.get("HEADROOM_STORE_MAX_MB", "256"))
_STORE_TTL_HOURS = int(os.environ.get("HEADROOM_STORE_TTL_HOURS", "168"))  # 7 天
_evict_lock = threading.Lock()
_last_evict_at = 0.0
_EVICT_MIN_INTERVAL_S = 60  # 定期扫描的最小间隔（被动触发）
# 突发保护：维护"自上次扫描以来新增的字节数"，累计超过上限的一定比例就强制扫描一次。
# 这样短时间内连续压入多个大文件也能及时淘汰，而不必等 60 秒节流窗口。
_pending_bytes = 0
_EVICT_FORCE_RATIO = 0.1   # 新增量超过上限的 10% 就强制扫描


def store_usage() -> dict:
    """返回 store 当前用量统计。"""
    count = 0
    total = 0
    try:
        for p in STORE.glob("*.bin"):
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                continue
    except Exception:
        pass
    return {
        "files": count,
        "bytes": total,
        "mb": round(total / 1048576, 2),
        "max_mb": _STORE_MAX_MB,
        "ttl_hours": _STORE_TTL_HOURS,
    }


def evict_store(force: bool = False) -> dict:
    """按 TTL + 总量上限清理 store/*.bin。返回 {removed, freed_bytes, kept}。"""
    global _last_evict_at
    if _STORE_MAX_MB <= 0 and _STORE_TTL_HOURS <= 0:
        return {"removed": 0, "freed_bytes": 0, "kept": 0, "skipped": "disabled"}
    global _pending_bytes
    now = time.time()
    with _evict_lock:
        if not force:
            over_budget = _STORE_MAX_MB > 0 and _pending_bytes > (
                _STORE_MAX_MB * 1024 * 1024 * _EVICT_FORCE_RATIO
            )
            if not over_budget and now - _last_evict_at < _EVICT_MIN_INTERVAL_S:
                return {"removed": 0, "freed_bytes": 0, "kept": 0, "skipped": "throttled"}
        _last_evict_at = now
        _pending_bytes = 0

        entries: list[tuple[float, int, pathlib.Path]] = []
        for p in STORE.glob("*.bin"):
            try:
                st = p.stat()
                entries.append((st.st_mtime, st.st_size, p))
            except OSError:
                continue

        removed = 0
        freed = 0

        # 1) TTL：过期先删
        if _STORE_TTL_HOURS > 0:
            cutoff = now - _STORE_TTL_HOURS * 3600
            survivors = []
            for mtime, size, p in entries:
                if mtime < cutoff:
                    try:
                        p.unlink()
                        removed += 1
                        freed += size
                    except OSError:
                        survivors.append((mtime, size, p))
                else:
                    survivors.append((mtime, size, p))
            entries = survivors

        # 2) 容量：超上限时从最旧的开始删
        if _STORE_MAX_MB > 0:
            limit = _STORE_MAX_MB * 1024 * 1024
            total = sum(size for _, size, _ in entries)
            if total > limit:
                entries.sort(key=lambda t: t[0])  # 最旧优先
                i = 0
                while total > limit and i < len(entries):
                    mtime, size, p = entries[i]
                    try:
                        p.unlink()
                        removed += 1
                        freed += size
                        total -= size
                    except OSError:
                        pass
                    i += 1
                entries = entries[i:]

        if removed:
            log({"event": "evict", "removed": removed, "freed_bytes": freed,
                 "kept": len(entries), "total_bytes": sum(s for _, s, _ in entries)})
        return {"removed": removed, "freed_bytes": freed, "kept": len(entries)}


def store_bytes(raw: bytes) -> str:
    h = hashlib.sha256(raw).hexdigest()[:16]
    dst = STORE / f"{h}.bin"
    created = False
    if not dst.exists():
        tmp = STORE / f"{h}.tmp"
        tmp.write_bytes(raw)
        os.replace(tmp, dst)   # 原子落盘
        created = True
    if created:
        global _pending_bytes
        try:
            _pending_bytes += len(raw)
        except Exception:
            pass
        # 写入新文件后才可能超限，顺带触发一次淘汰
        # （evict_store 内部按"新增字节数 + 时间节流"决定是否真的扫描）
        try:
            evict_store()
        except Exception:
            pass
    return h


# ── path 模式白名单───────────────────────────────
# 原实现允许读取任意路径并把内容永久存入 store，任何本机进程都能借
# /compress{"path":...} + /retrieve 组合读出 C:\Windows\win.ini 之类的文件。
# 插件实际只用于传 DSH 的 spill 文件，因此默认把可读范围收紧到：
#   1. 系统临时目录下的 dsh-subprocess-* / dsh-spill-* 路径
#   2. 显式允许的额外根目录（HEADROOM_ALLOWED_PATHS，分号分隔）
from pathlib import Path as _Path

_SPILL_NAME_RE = re.compile(r"(dsh-subprocess|dsh-spill)", re.IGNORECASE)

def _allowed_path_roots() -> list[_Path]:
    roots: list[_Path] = []
    for p in os.environ.get("HEADROOM_ALLOWED_PATHS", "").split(os.pathsep):
        p = p.strip()
        if p:
            try:
                roots.append(_Path(p).resolve())
            except Exception:
                pass
    return roots


def check_path_allowed(path: str) -> str | None:
    """检查 path 是否在允许范围内。允许返回 None，否则返回拒绝原因。"""
    try:
        p = _Path(path).resolve()
    except Exception as e:
        return f"path resolve failed: {e}"
    # 1) 系统临时目录 + 名字像 spill 文件
    try:
        tmp = _Path(os.environ.get("TEMP") or os.environ.get("TMP") or "").resolve()
        if tmp and tmp in p.parents and _SPILL_NAME_RE.search(p.name):
            return None
    except Exception:
        pass
    # 2) 显式配置的允许根
    for root in _allowed_path_roots():
        if root == p or root in p.parents:
            return None
    return "path not allowed (仅允许系统临时目录下的 dsh-subprocess-*/dsh-spill-* 或 HEADROOM_ALLOWED_PATHS 配置的目录)"


def do_compress(payload: dict) -> tuple[int, dict]:
    no_store = bool(payload.get("no_store"))
    text = payload.get("text")
    path = payload.get("path")
    if not (isinstance(text, str) ^ isinstance(path, str)):
        return 400, {"error": "exactly one of text|path required"}
    if path is not None:
        reason = check_path_allowed(path)
        if reason is not None:
            return 403, {"error": reason}
        try:
            raw = pathlib.Path(path).read_bytes()
        except OSError as e:
            return 400, {"error": f"read path failed: {e}"}
    else:
        raw = text.encode("utf-8")
    if len(raw) > MAX_INPUT:
        return 413, {"error": f"input too large: {len(raw)} > {MAX_INPUT}"}
    if not raw.strip():
        return 400, {"error": "empty input"}

    decoded = decode_bytes(raw)
    kind = payload.get("kind") or detect_kind(decoded)
    if kind not in ("log", "json", "code", "text", "diff", "search", "generic"):
        return 400, {"error": f"invalid kind: {kind}"}

    if kind in DISABLED_KINDS:
        # 停用类型：立刻返回原文，不做任何压缩（避免无收益的长耗时）
        return 200, {"hash": None, "kind": kind, "in_chars": len(decoded), "out_chars": len(decoded),
                     "ms": 0, "ratio": 1.0, "compressed": False, "text": decoded,
                     "note": f"kind '{kind}' disabled by HEADROOM_DISABLED_KINDS"}

    language = payload.get("language")

    if kind == "text" and not kompress_model_ready():
        # 无模型：不压、不报错，原文照常返回（fail-open）
        return 200, {"hash": None, "kind": kind, "in_chars": len(decoded), "out_chars": len(decoded),
                     "ms": 0, "ratio": 1.0, "compressed": False, "text": decoded,
                     "note": "kompress model not cached"}

    wrapper_prefix = ""
    _probe = _strip_web_fetch_wrapper(decoded)
    if len(_probe) < len(decoded):
        wrapper_prefix = decoded[: len(decoded) - len(_probe)]

    t0 = time.perf_counter()
    with _comp_lock:
        fn = _get_compressor(kind)
        out = fn(decoded, language)
        # web_fetch wrapper 会破坏 json 解析：改压剥离后的 body 再拼回头部。
        if (isinstance(out, str) and wrapper_prefix
                and len(out) >= len(_probe)):
            body = fn(_probe, language)
            if isinstance(body, str) and len(body) < len(_probe):
                out = wrapper_prefix + body
    ms = round((time.perf_counter() - t0) * 1000, 1)

    # 【信息保真守卫】LogCompressor 在纯 INFO 日志 / 文件列表 / 代码 / CSV 上
    # 会把内容清零（实测 400 行 → 93 字符 "[400 lines omitted]..."），模型此后
    # 什么都看不到。这里检测"几乎清空"的情形并退化到行级抽样，保证模型至少能
    # 看到头尾各若干行 + 明确的省略规模。
    if kind in ("log", "json", "diff", "search") and isinstance(out, str):
        kept_lines = sum(1 for ln in out.splitlines() if ln.strip())
        src_lines = sum(1 for ln in decoded.splitlines() if ln.strip())
        # 源内容有超过 20 行，但输出保留不到 5 行 → 判为"内容被清空"
        # 加体积条件：JSON 压缩输出天然少行，避免把正常压缩误判成清空。
        if src_lines > 20 and kept_lines < 5 and len(out) < 0.1 * len(decoded):
            fallback, omitted = sample_lines(decoded)
            if omitted > 0 and len(fallback) < len(decoded):
                log({"event": "degenerate-fallback", "kind": kind,
                     "src_lines": src_lines, "kept_before_fallback": kept_lines,
                     "kept_lines_legacy": kept_lines, "fallback_omitted": omitted,
                     "kept_final": src_lines - omitted})
                out = fallback

    # 日志压缩默认会 dedupe 掉重复 WARN。这里把"源里有、输出里没有"的
    # WARN/ERROR 级别行补齐到正文末尾，避免警告静默消失（ERROR 原本就保留，
    # 此项主要用于 WARN）。补回的行去重、限量，且不计入优势判断。
    if kind == "log" and isinstance(out, str) and len(out) < len(decoded):
        try:
            out = _restore_dropped_notables(decoded, out)
        except Exception:
            pass  # 任何异常都不影响主流程

    if not isinstance(out, str) or len(out) >= len(decoded):
        return 200, {"hash": None, "kind": kind, "in_chars": len(decoded),
                     "out_chars": len(decoded), "ms": ms, "ratio": 1.0,
                     "compressed": False, "text": decoded}

    # 【信息保真守卫】"压缩比好看"不等于"内容可用"。若输出相对原文短到
    # 离谱（< 0.5% 且不足 200 字符），模型基本读不到有效信息，此时**不替换**，
    # 原样返回原文并标记 compressed=false，让插件跳过替换、模型看到完整内容。
    out_ratio = len(out) / max(len(decoded), 1)
    if out_ratio < 0.005 and len(out) < 200:
        log({"event": "reject-too-small", "kind": kind,
             "in_chars": len(decoded), "out_chars": len(out), "ratio": out_ratio})
        return 200, {"hash": None, "kind": kind, "in_chars": len(decoded), "out_chars": len(decoded),
                     "ms": ms, "ratio": 1.0, "compressed": False, "text": decoded,
                     "note": f"压缩输出过小（{len(out)} 字符 / {out_ratio:.4%}），已拒绝替换"}

    h = "" if no_store else store_bytes(raw)
    return 200, {"hash": h, "kind": kind, "in_chars": len(decoded), "out_chars": len(out),
                 "ms": ms, "ratio": round(out_ratio, 4),
                 "compressed": True, "text": out}


class Handler(BaseHTTPRequestHandler):
    server_version = f"headroom-sidecar/{_VERSION}"

    def log_message(self, fmt, *args):  # 静默默认访问日志，走自己的 log()
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {
                "ok": True, "version": _VERSION,
                "uptime_s": round(time.time() - _started, 1),
                "compressors_loaded": {k: True for k in _compressors},
                "text_model_ready": kompress_model_ready(),
                "disabled_kinds": sorted(DISABLED_KINDS),
                "store": store_usage(),
            })
            return
        m = re.fullmatch(r"/retrieve/([0-9a-f]{16})", self.path)
        if m:
            self._serve_retrieve(m.group(1))
            return
        if self.path == "/evict":
            # 手动触发淘汰（调试/运维用）
            self._json(200, evict_store(force=True))
            return
        if self.path == "/store":
            self._json(200, store_usage())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        # 请求体不是合法 JSON / 不是对象时返回 400（客户端错误），
        # 不再统一落到 500 —— 那会把调用方的参数问题误报成服务端故障，
        # 也会在 sidecar.log 里刷出无意义的 error 事件。
        try:
            body = self._read_body()
        except Exception as e:
            self._json(400, {"error": f"read body failed: {e}"})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            log({"event": "bad-request", "path": self.path, "error": repr(e)})
            self._json(400, {"error": f"invalid json body: {e}"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "json body must be an object"})
            return

        try:
            if self.path == "/compress":
                code, obj = do_compress(payload)
                log({"event": "compress", "code": code, **{k: obj.get(k) for k in
                     ("hash", "kind", "in_chars", "out_chars", "ms", "compressed")}})
                self._json(code, obj)
                return
            if self.path == "/retrieve":
                self._serve_retrieve(str(payload.get("hash") or ""))
                return
            self._json(404, {"error": "not found"})
        except Exception as e:
            log({"event": "error", "path": self.path, "error": repr(e)})
            self._json(500, {"error": repr(e)})

    def _serve_retrieve(self, h: str):
        f = STORE / f"{h}.bin"
        if h and f.exists():
            self._send(200, f.read_bytes(), "application/octet-stream")
        else:
            self._json(404, {
                "error": f"hash not found: {h}",
                "hint": "可能从未存储，或因 store 达到容量/TTL 上限被淘汰（见 /store）",
            })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8797)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    global _SCRIPT_HASH
    _SCRIPT_HASH = script_hash()
    threading.Thread(target=watch_script, daemon=True).start()
    log({"event": "start", "port": args.port, "pid": os.getpid(), "version": _VERSION})
    print(f"headroom sidecar listening on 127.0.0.1:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log({"event": "stop"})
        srv.server_close()


if __name__ == "__main__":
    main()