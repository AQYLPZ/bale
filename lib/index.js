/**
 * bale：把符合条件的工具输出在进入模型上下文前自动压缩。
 *
 * 集成点：ctx.on("tools/post-execute", (exec, result, next))，
 * 返回 { kind: "accept", content: [...] } 只替换模型看到的 content，
 * canonical value 原样保留（会话日志记录的即替换后内容，回放一致）。
 *
 * fail-open 是铁律：sidecar 不可用 / 超时 / 任何异常 -> 原样放行。
 *
 * 本插件不依赖 @deepseek-ai/dsh-tools —— headroom_retrieve 用纯对象定义走
 * ctx.tools.register（与 dsh-ops 相同的做法），因此不需要 dshcli 那份依赖树。
 */
import { createHash } from "node:crypto";
import { appendFileSync, mkdirSync, statSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveConfig } from "./config.js";
import { compressText, compressPath, retrieveText, ensureSidecar, compressWithEnsure } from "./sidecar-client.js";

export const name = "bale";
export const inject = ["tools"];

// 日志路径：优先 config.logFile（profile patch 里写绝对路径）；
// 未配置时退回到插件根下的 headroom-auto.log（lib/ 上两级 = 插件根）。
function defaultLogFile() {
  try {
    return fileURLToPath(new URL("../headroom-auto.log", import.meta.url));
  } catch {
    return "headroom-auto.log";
  }
}

let LOG = defaultLogFile();

const recentRetrieved = new Map();
const RETRIEVE_TTL_MS = 10 * 60 * 1000;

/** 由 apply() 在 resolveConfig 之后调用，把配置里的 logFile 装进来。 */
function setLogFile(logFile) {
  if (typeof logFile === "string" && logFile.length > 0) LOG = logFile;
}

function log(evt) {
  try {
    mkdirSync(dirname(LOG), { recursive: true });
    appendFileSync(LOG, JSON.stringify({ t: new Date().toISOString(), ...evt }) + "\n", "utf8");
  } catch {}
}

function extractText(result) {
  /** content 全部为 text 块时返回拼接文本，否则 null（图片/混合块不动）。 */
  const blocks = Array.isArray(result?.content) ? result.content : null;
  if (!blocks || blocks.length === 0) return null;
  const texts = [];
  for (const b of blocks) {
    if (!b || b.type !== "text" || typeof b.text !== "string") return null;
    texts.push(b.text);
  }
  return texts.length === 1 ? texts[0] : texts.join("\n\n");
}

/** 检测截断+spill：pwsh/bash 等大输出先落盘（spillPath）再截断，全文在 spill 文件里。 */
function pickSpill(result) {
  const v = result?.value;
  if (!v || typeof v !== "object") return null;
  for (const key of ["stdout", "stderr"]) {
    const s = v[key];
    if (s && typeof s === "object" && s.truncated === true && typeof s.spillPath === "string") {
      return { spillPath: s.spillPath, stream: key };
    }
  }
  return null;
}

export function looksLikeDocumentDump(text) {
  if (typeof text !== "string" || text.length === 0) return false;
  const stripped = text.replace(/^[ \t]*\d+\t/gm, "");
  const headings = (stripped.match(/^#{1,6}\s/gm) || []).length;
  if (headings <= 5) return false;
  const fences = (stripped.match(/^```/gm) || []).length;
  const h2 = (stripped.match(/^#{2,6}\s/gm) || []).length;
  if (fences < 2 && h2 <= 5) return false;
  const lines = stripped.split(/\r?\n/);
  if (lines.length === 0) return false;
  const tsLines = lines.filter((ln) => /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(ln)).length;
  return tsLines / lines.length < 0.05;
}

export async function maybeCompress(exec, result, cfg, skipInfo) {
  /** 返回 accept decision 或 null（null = 不替换）。导出供单元测试。
   *  spill 场景：输出被截断且带 spillPath 时压 spill 全文，hash 对应完整原文；
   *  spill 全文超过 maxChars 时不压。 */
  if (!exec?.name || !result || result.isError) return null;
  if (cfg.protectTools.has(exec.name)) return null;
  if (!cfg.tools.has(exec.name)) return null;
  const text = extractText(result);
  if (text == null) return null;
  if (text.length < cfg.minChars || text.length > cfg.maxChars) return null;

  if (looksLikeDocumentDump(text)) {
    if (skipInfo) skipInfo.reason = "doc-dump";
    return null;
  }

  const h = createHash("sha256").update(Buffer.from(text, "utf8")).digest("hex").slice(0, 16);
  const seenAt = recentRetrieved.get(h);
  if (seenAt !== undefined && Date.now() - seenAt <= RETRIEVE_TTL_MS) {
    if (skipInfo) skipInfo.reason = "just-retrieved";
    return null;
  }
  if (seenAt !== undefined) recentRetrieved.delete(h);

  let spill = null;
  try { spill = pickSpill(result); } catch { /* 形状异常按无 spill 处理 */ }

  let r;
  if (spill) {
    try {
      const st = statSync(spill.spillPath);
      if (st.size > cfg.maxChars) return null;
    } catch {
      spill = null; // spill 文件已被清理：退回压截断版
    }
  }
  if (spill) {
    r = await compressWithEnsure(() => compressPath(spill.spillPath, cfg), cfg);
  } else {
    r = await compressWithEnsure(() => compressText(text, cfg), cfg);
  }
  if (!r || r.compressed !== true) return null;

  // kinds 闸门：sidecar 才是权威（它按 DISABLED_KINDS 决定压不压）。
  // kindsAreOpen 默认 true —— 新增压缩器不用重启；仍可从配置关掉。
  if (!cfg.kindsAreOpen && !cfg.kinds.has(r.kind)) return null;

  // 原项目自带的 CCR 提示是 24 位死链（指向 sidecar 不认识的 hash），
  // 统一改写为我们的 16 位真实 hash，避免模型拿到不可用线索。
  const body = r.text
    .replace(/Retrieve (?:more|full [\w -]+):\s*hash=[0-9a-f]+/gi,
             "完整原文请调用 headroom_retrieve，hash=" + r.hash)
    .replace(/hash=[0-9a-f]{24}(?![0-9a-f])/gi, "hash=" + r.hash);

  const origin = spill
    ? "完整原文 " + r.in_chars + " 字符（工具输出曾被截断，已取 spill 全文）"
    : "原文 " + r.in_chars + " 字符";
  const header =
    "[headroom 已压缩] " + origin + "（类型 " + r.kind + "）压缩至 " + (r.ratio * 100).toFixed(2) + "%，" +
    "原文 hash=" + r.hash + "。如需完整原文，调用 headroom_retrieve 工具并传入该 hash。\n\n";

  if (cfg.replaceMode === "preview") {
    return { kind: "accept", content: [{ type: "text", text: header + text }] };
  }
  const newText = header + body;
  if (newText.length >= text.length) return null; // 无收益就不替换
  log({ event: "compress", tool: exec.name, kind: r.kind, in_chars: r.in_chars,
        out_chars: r.out_chars, ratio: r.ratio, hash: r.hash, ms: r.ms,
        spill: spill ? spill.spillPath : undefined });
  return { kind: "accept", content: [{ type: "text", text: newText }] };
}

export function apply(ctx, config) {
  const cfg = resolveConfig(config);
  setLogFile(cfg.logFile);
  log({ event: "apply", enabled: cfg.enabled, sidecarUrl: cfg.sidecarUrl, replaceMode: cfg.replaceMode,
        logFile: LOG, storeDir: cfg.storeDir, wsDir: cfg.wsDir,
        sidecarPython: cfg.sidecarPython, sidecarScript: cfg.sidecarScript });

  ctx.on("tools/post-execute", async (exec, result, next) => {
    // 必须先调 next()：cordis waterfall 里不调 next 会否决整条链，
    // 下游（dsh-ops 等）的 post-execute 监听器将完全不执行。
    let downstream;
    try {
      downstream = await next();
    } catch (e) {
      log({ event: "downstream-error", tool: exec?.name, error: String(e && e.message || e) });
      downstream = { kind: "accept" };
    }

    try {
      if (!cfg.enabled) return downstream;
      if (!downstream || downstream.kind === "block") return downstream;

      const skipInfo = {};
      const decision = await maybeCompress(exec, result, cfg, skipInfo);
      if (!decision) {
        if (cfg.tools.has(exec?.name)) {
          const blocks = Array.isArray(result?.content) ? result.content : [];
          const len = blocks.reduce((s, b) => s + (typeof b?.text === "string" ? b.text.length : 0), 0);
          log({ event: "skip", tool: exec.name, isError: !!result?.isError, contentLen: len,
                downstream: downstream.kind,
                ...(skipInfo.reason ? { reason: skipInfo.reason } : {}) });
        }
        return downstream;
      }

      // 只覆盖 content，保留下游的 additionalContexts 等其他字段
      return { ...downstream, content: decision.content };
    } catch (e) {
      // fail-open：压缩链路任何异常都原样放行
      log({ event: "passthrough-error", tool: exec?.name, error: String(e && e.message || e) });
      try { ctx.logger?.warn?.("bale: compress failed, passthrough: " + (e?.message ?? e)); } catch {}
      return downstream;
    }
  });

  // 纯对象工具定义（与 dsh-ops 相同做法）：不 import @deepseek-ai/dsh-tools。
  // 注意 ctx.tools.register 这条路径只校验 output.schema，参数原样进 execute，
  // 所以这里自己校验 hash，避免非法值打到 sidecar。
  ctx.tools.register({
    name: "headroom_retrieve",
    description:
      "取回被 headroom 自动压缩掉的原始工具输出（完整原文）。当压缩摘要信息不足时使用；参数 hash 见压缩结果头部标注。",
    parameters: {
      type: "object",
      properties: {
        hash: { type: "string", description: "压缩结果头部标注的原文 hash（16 位十六进制）" },
      },
      required: ["hash"],
      additionalProperties: false,
    },
    output: {
      schema: { type: "string" },
      render: (_args, value) => [{ type: "text", text: value }],
    },
    async execute(args) {
      const hash = args?.hash;
      if (typeof hash !== "string" || !/^[0-9a-f]{16}$/i.test(hash)) {
        throw new Error("headroom_retrieve: hash 必须是 16 位十六进制字符串");
      }
      const text = await retrieveText(hash, cfg);
      recentRetrieved.set(hash, Date.now());
      for (const [k, ts] of recentRetrieved) {
        if (Date.now() - ts > RETRIEVE_TTL_MS) recentRetrieved.delete(k);
      }
      log({ event: "retrieve", hash, chars: text.length });
      return text;
    },
  });

  // DSH 启动时确保 sidecar 在运行（不在则 pythonw 无窗口拉起；失败只记日志不影响加载）
  ensureSidecar(cfg, { waitMs: 15000 })
    .then((ok) => log({ event: "ensure-sidecar", ok }))
    .catch((e) => log({ event: "ensure-sidecar", ok: false, error: String(e) }));
}
