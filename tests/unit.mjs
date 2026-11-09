/**
 * bale 单元 / 集成测试。
 * 前置：sidecar 在 127.0.0.1:8797 常驻（本脚本不会自动拉起）。
 * 用法：npm test
 */
import assert from "node:assert/strict";
import { writeFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdirSync } from "node:fs";
import { resolveConfig } from "../lib/config.js";
import { maybeCompress } from "../lib/index.js";
import { compressText, retrieveText, health } from "../lib/sidecar-client.js";

let passed = 0, failed = 0;
const failures = [];
async function t(name, fn) {
  try { await fn(); passed++; console.log("  ok  " + name); }
  catch (e) { failed++; failures.push([name, e]); console.log("  FAIL " + name + " -> " + (e?.message ?? e)); }
}

const bigLog = (n) => Array.from({ length: n }, (_, i) =>
  `2026-11-05 22:30:${String(i % 60).padStart(2, "0")} INFO worker step=${i} loss=0.${999 - (i % 900)} acc=0.${400 + (i % 600)} elapsed=${i * 7}ms`).join("\r\n");

const mkResult = (text) => ({ content: [{ type: "text", text }], isError: false, value: {}, meta: {} });

console.log("== shouldCompress 判定分支（不触网） ==");
await t("白名单外工具 -> 不压", async () => {
  const cfg = resolveConfig();
  assert.equal(await maybeCompress({ name: "web_search" }, mkResult(bigLog(100)), cfg), null);
});
await t("保护名单工具(read) -> 不压", async () => {
  const cfg = resolveConfig();
  assert.equal(await maybeCompress({ name: "read" }, mkResult(bigLog(100)), cfg), null);
});
await t("isError 结果 -> 不压", async () => {
  const cfg = resolveConfig();
  assert.equal(await maybeCompress({ name: "pwsh" }, { ...mkResult(bigLog(100)), isError: true }, cfg), null);
});
await t("低于 minChars -> 不压", async () => {
  const cfg = resolveConfig();
  assert.equal(await maybeCompress({ name: "pwsh" }, mkResult(bigLog(100)), cfg), null); // ~9K < 16384
});
await t("超过 maxChars -> 不压", async () => {
  const cfg = resolveConfig({ maxChars: 1000 });
  assert.equal(await maybeCompress({ name: "pwsh" }, mkResult(bigLog(100)), cfg), null);
});
await t("content 混入非 text 块 -> 不压", async () => {
  const cfg = resolveConfig({ minChars: 10 });
  const result = { content: [{ type: "text", text: bigLog(3000) }, { type: "image", data: "x" }], isError: false };
  assert.equal(await maybeCompress({ name: "pwsh" }, result, cfg), null);
});
await t("content 空数组 -> 不压", async () => {
  const cfg = resolveConfig();
  assert.equal(await maybeCompress({ name: "pwsh" }, { content: [], isError: false }, cfg), null);
});
await t("kinds 严格模式（kindsAreOpen=false）-> 不在列表不压", async () => {
  const cfg = resolveConfig({ kinds: ["json"], kindsAreOpen: false });
  assert.equal(await maybeCompress({ name: "pwsh" }, mkResult(bigLog(3000)), cfg), null);
});
await t("kinds 开放模式（默认）-> 以 sidecar 判定为准", async () => {
  const cfg = resolveConfig({ kinds: ["json"] });   // kindsAreOpen 默认 true
  const d = await maybeCompress({ name: "pwsh" }, mkResult(bigLog(3000)), cfg);
  assert.ok(d, "开放模式下 log 应被放行（sidecar 判定可压）");
  assert.match(d.content[0].text, /类型 log/);
});

console.log("== 压缩路径（真实 sidecar） ==");
await t("pwsh 大日志 -> accept decision（含压缩头+hash）", async () => {
  const cfg = resolveConfig();
  const d = await maybeCompress({ name: "pwsh" }, mkResult(bigLog(3000)), cfg);
  assert.ok(d, "decision should not be null");
  assert.equal(d.kind, "accept");
  const text = d.content[0].text;
  assert.match(text, /\[headroom 已压缩\]/);
  assert.match(text, /hash=[0-9a-f]{16}/);
  assert.ok(text.length < 16384, "替换文本应远小于 16KB");
});
await t("preview 模式 -> 只加标记保留原文", async () => {
  const cfg = resolveConfig({ replaceMode: "preview" });
  const src = bigLog(3000);
  const d = await maybeCompress({ name: "pwsh" }, mkResult(src), cfg);
  assert.ok(d);
  assert.ok(d.content[0].text.includes(src), "preview 应保留原文");
  assert.match(d.content[0].text, /\[headroom 已压缩\]/);
});

console.log("== spill 全文场景 ==");
await t("spill 截断+spillPath -> 压全文而非截断版", async () => {
  const src = bigLog(20000);              // ~1.4M 字符全文
  // path 模式只允许 temp 下的 dsh-subprocess-*/dsh-spill-*
  const spillDir = join(tmpdir(), "dsh-subprocess-unittest");
  mkdirSync(spillDir, { recursive: true });
  const spillPath = join(spillDir, "dsh-subprocess-unittest-1-stdout.log");
  writeFileSync(spillPath, src, "utf8");
  try {
    const truncatedTail = src.slice(-64000);
    const result = {
      content: [{ type: "text", text: truncatedTail + `\n[output truncated; full output: ${spillPath}]` }],
      isError: false,
      value: { stdout: { text: truncatedTail, truncated: true, spillPath }, stderr: { text: "", truncated: false } },
      meta: {},
    };
    const d = await maybeCompress({ name: "pwsh" }, result, resolveConfig());
    assert.ok(d, "decision should not be null");
    const text = d.content[0].text;
    assert.match(text, /完整原文 \d+ 字符（工具输出曾被截断，已取 spill 全文）/);
    assert.ok(!/Retrieve more: hash=[0-9a-f]+/.test(text), "CCR hash 提示应被改写");
    const m = text.match(/hash=([0-9a-f]{16})/);
    const back = await retrieveText(m[1], resolveConfig());
    assert.equal(back.length, src.length, "retrieve 应取回完整全文");
    assert.equal(back, src, "全文逐字一致");
  } finally {
    try { unlinkSync(spillPath); } catch {}
  }
});
await t("spill 文件已不存在 -> 退回压截断版", async () => {
  const tail = bigLog(3000);              // 截断版本身 >16K
  const result = {
    content: [{ type: "text", text: tail }],
    isError: false,
    value: { stdout: { text: tail, truncated: true, spillPath: join(tmpdir(), "dsh-spill-missing", "spill-gone.log") }, stderr: { text: "", truncated: false } },
    meta: {},
  };
  const d = await maybeCompress({ name: "pwsh" }, result, resolveConfig());
  assert.ok(d);
  assert.match(d.content[0].text, /原文 \d+ 字符（类型 log）/); // 普通头部（非 spill 全文）
});

console.log("== WARN 保留（补回被压缩器丢弃的告警行） ==");
const mixedLog = (n) => Array.from({ length: n }, (_, i) => {
  const step = i + 1;
  if (step % 97 === 0) return `2026-11-06 00:00:00 ERROR worker step=${step} failed reason=timeout code=504`;
  if (step % 53 === 0) return `2026-11-06 00:00:00 WARN worker step=${step} retry=2 latency=9871ms`;
  return `2026-11-06 00:00:00 INFO worker step=${step} loss=0.001 acc=0.999 latency=123ms queue=0 status=ok`;
}).join("\r\n");

await t("混合级别日志 -> WARN 全部保留", async () => {
  const cfg = resolveConfig();
  const src = mixedLog(800);
  const wantWarn = (src.match(/WARN worker/g) || []).length;
  const wantErr = (src.match(/ERROR worker/g) || []).length;
  const r = await compressText(src, cfg);
  assert.equal(r.compressed, true);
  const gotWarn = (r.text.match(/WARN worker/g) || []).length;
  const gotErr = (r.text.match(/ERROR worker/g) || []).length;
  assert.equal(gotErr, wantErr, "ERROR 行不应丢失");
  assert.equal(gotWarn, wantWarn, `WARN 应全部保留：期望 ${wantWarn}，实际 ${gotWarn}`);
  assert.match(r.text, /\[告警保留\]/, "应带自动补回标记");
});

await t("纯 INFO 日志 -> 不产生补回块", async () => {
  const cfg = resolveConfig();
  const src = bigLog(800);
  const r = await compressText(src, cfg);
  assert.equal(r.compressed, true);
  assert.ok(!r.text.includes("[WARN 保留]"), "没有告警行时不应出现补回块");
});

await t("JSON 类型 -> 不触发 WARN 补回", async () => {
  const cfg = resolveConfig();
  const src = Array.from({ length: 800 }, (_, i) =>
    JSON.stringify({ id: i, lvl: i % 50 === 0 ? "WARN" : "INFO" })).join("\n");
  const r = await compressText(src, cfg);
  assert.ok(!String(r.text).includes("[WARN 保留]"), "非 log 类型不应补回");
});

await t("text 类型已停用 -> kinds 不含 text", async () => {
  const cfg = resolveConfig();
  assert.equal(cfg.kinds.has("text"), false, "默认配置应已关闭 text 压缩");
  assert.equal(cfg.kinds.has("log"), true);
});

console.log("== minChars 阈值（8192） ==");
await t("超过 8192 的日志会被压缩", async () => {
  const cfg = resolveConfig();
  assert.equal(cfg.minChars, 8192, "默认阈值应为 8192");
  const base8k = (n) => Array.from({ length: 300 }, (_, i) =>
    `2026-11-06 00:00:${String(i % 60).padStart(2, "0")} INFO worker step=${i} status=ok`).join("\r\n");
  const long = base8k();
  assert.ok(long.length > 8192, "样例应超过阈值");
  const r = await compressText(long, cfg);
  assert.equal(r.compressed, true, "超过阈值应压缩");
});

await t("低于 8192 的日志不压", async () => {
  const cfg = resolveConfig();
  const small = Array.from({ length: 40 }, (_, i) => `2026-11-06 00:00:00 INFO step=${i} ok`).join("\n");
  assert.ok(small.length < 8192);
  const d = await maybeCompress({ name: "pwsh" }, mkResult(small), cfg);
  assert.equal(d, null);
});

console.log("== 熔断（sidecar 不可用时不拖慢调用） ==");
await t("sidecar 不可用 -> 首次有限等待，之后立即放行", async () => {
  const { compressWithEnsure, isCircuitOpen } = await import("../lib/sidecar-client.js");
  const deadCfg = resolveConfig({ sidecarUrl: "http://127.0.0.1:9", timeoutMs: 500 });
  const fn = async () => fetch("http://127.0.0.1:9/compress", { method: "POST", body: "{}" });
  const t0 = Date.now();
  try { await compressWithEnsure(fn, deadCfg); } catch {}
  const first = Date.now() - t0;
  assert.ok(first < 6000, `首次等待应 <6s，实际 ${first}ms`);
  assert.equal(isCircuitOpen(), true, "失败后应进入熔断");
  const t1 = Date.now();
  try { await compressWithEnsure(fn, deadCfg); } catch {}
  const second = Date.now() - t1;
  assert.ok(second < 200, `熔断后应 <200ms，实际 ${second}ms`);
});

console.log("== 告警保留：未补回数量显式交代 ==");
await t("超预算时明写未能补回条数", async () => {
  const cfg = resolveConfig();
  const lines = [];
  for (let i = 1; i <= 800; i++) {
    if (i % 100 === 0) lines.push(`2026-11-06 00:00:00 WARN worker step=${i} ${"Y".repeat(900)}`);
    else lines.push(`2026-11-06 00:00:00 INFO worker step=${i} ok`);
  }
  const r = await compressText(lines.join("\n"), cfg);
  assert.equal(r.compressed, true);
  assert.match(r.text, /未能补回/, "应明确告知未补回的条数");
});

await t("完全重复告警给出频次汇总", async () => {
  const cfg = resolveConfig();
  const lines = [];
  for (let i = 1; i <= 800; i++) {
    if (i % 5 === 0) lines.push("2026-11-06 00:00:00 WARN worker retry limit reached");
    else lines.push(`2026-11-06 00:00:00 INFO worker step=${i} ok`);
  }
  const r = await compressText(lines.join("\n"), cfg);
  assert.equal(r.compressed, true);
  assert.match(r.text, /×\d+/, "应出现频次标注");
});

console.log("== diff / search 路由（2026-11-06 新增） ==");
const mkDiff = (files = 15) => {
  let s = "";
  for (let f = 1; f <= files; f++) {
    s += `diff --git a/src/mod_${f}.py b/src/mod_${f}.py\n`;
    s += `index 1234567..89abcde 100644\n--- a/src/mod_${f}.py\n+++ b/src/mod_${f}.py\n`;
    s += `@@ -100,40 +100,41 @@ def process(self):\n`;
    for (let c = 1; c <= 29; c++) s += `     context_line_${c} = self.state.get('k${c}')\n`;
    s += `-    old_value = compute_legacy(input_data)\n`;
    s += `+    new_value = compute_v2(input_data)\n`;
    for (let c = 30; c <= 40; c++) s += `     more_context_${c} = self.state.get('k${c}')\n`;
  }
  return s;
};
const mkGrep = (files = 60) => {
  const out = [];
  for (let f = 1; f <= files; f++)
    for (let m = 1; m <= 8; m++) {
      out.push(`src/pkg${f}/module.py:${m * 17}:    def handler_${f}_${m}(self, request):`);
      out.push(`src/pkg${f}/module.py:${m * 17 + 1}:        return self.process(request.payload)`);
    }
  return out.join("\n");
};

await t("git diff 自动识别为 diff 并压缩", async () => {
  const cfg = resolveConfig();
  const src = mkDiff();
  const r = await compressText(src, cfg);
  assert.equal(r.compressed, true, "diff 应被压缩");
  assert.equal(r.kind, "diff", `应识别为 diff，实际 ${r.kind}`);
  assert.ok(r.ratio < 0.5, `压缩比应 <0.5，实际 ${r.ratio}`);
});

await t("grep 输出自动识别为 search 并压缩", async () => {
  const cfg = resolveConfig();
  const src = mkGrep();
  const r = await compressText(src, cfg);
  assert.equal(r.compressed, true, "search 应被压缩");
  assert.equal(r.kind, "search", `应识别为 search，实际 ${r.kind}`);
  assert.ok(r.ratio < 0.3, `压缩比应 <0.3，实际 ${r.ratio}`);
});

await t("普通日志不被误判为 search", async () => {
  const cfg = resolveConfig();
  const src = bigLog(800);
  const r = await compressText(src, cfg);
  assert.equal(r.kind, "log", `日志应仍识别为 log，实际 ${r.kind}`);
});

await t("散文不被误判为 diff/search", async () => {
  const cfg = resolveConfig();
  const src = ("这是一段普通的中文说明文字，不包含任何搜索结果或代码差异结构。" .repeat(300));
  const r = await compressText(src, cfg);
  assert.ok(r.kind !== "diff" && r.kind !== "search", `散文被误判为 ${r.kind}`);
});

console.log("== 安全与存储 ==");
await t("path 白名单：拒绝任意文件读取", async () => {
  const cfg = resolveConfig();
  const r = await fetch(cfg.sidecarUrl + "/compress", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: "C:/Windows/win.ini", kind: "log" }),
  });
  assert.equal(r.status, 403, "应拒绝非 spill 路径");
});

await t("非法 JSON body -> 400 而非 500", async () => {
  const cfg = resolveConfig();
  const r = await fetch(cfg.sidecarUrl + "/compress", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "not json at all",
  });
  assert.equal(r.status, 400);
});

await t("非对象 JSON body -> 400", async () => {
  const cfg = resolveConfig();
  const r = await fetch(cfg.sidecarUrl + "/compress", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "[1,2,3]",
  });
  assert.equal(r.status, 400);
});

await t("/store 返回用量统计", async () => {
  const cfg = resolveConfig();
  const r = await fetch(cfg.sidecarUrl + "/store");
  assert.equal(r.ok, true);
  const j = await r.json();
  assert.ok(typeof j.mb === "number");
  assert.ok(typeof j.max_mb === "number");
  assert.ok(j.max_mb > 0, "应配置容量上限");
});

await t("retrieve miss 带淘汰提示", async () => {
  const cfg = resolveConfig();
  const r = await fetch(cfg.sidecarUrl + "/retrieve/0000000000000000");
  assert.equal(r.status, 404);
  const j = await r.json();
  assert.ok(j.hint && j.hint.length > 0, "应给出 hint");
});

await t("插件输出正文不含 CCR 24 位死链，且真实 hash 可见", async () => {
  // 注意：compressText 返回 sidecar 原始响应（含 CCR hash），
  // CCR 改写发生在插件层 maybeCompress，因此这里必须测最终输出。
  const cfg = resolveConfig();
  const d = await maybeCompress({ name: "pwsh" }, mkResult(bigLog(800)), cfg);
  assert.ok(d, "应产生替换");
  const text = d.content[0].text;
  const has24 = /hash=[0-9a-f]{24}(?![0-9a-f])/i.test(text);
  assert.equal(has24, false, "正文不应残留 24 位 CCR hash");
  // 正文里应出现真实 16 位 hash，便于头部被裁后仍能找回
  const real = text.match(/hash=([0-9a-f]{16})(?![0-9a-f])/i);
  assert.ok(real, "正文应含真实 16 位 hash");
});

console.log("== 零冲突修复（钩子链 / 清零守卫 / 内容可用性） ==");

await t("钩子必须调用 next()：下游不被屏蔽", async () => {
  // 复现 cordis waterfall：不调 next() 会否决后续链
  const waterfall = async (finalNext, ...listeners) => {
    let idx = 0;
    const dispatch = async () => {
      const cb = listeners[idx++];
      if (!cb) return finalNext();
      let called = false;
      const n = async () => {
        if (called) throw new Error("next() called multiple times");
        called = true;
        return dispatch();
      };
      return cb(n);
    };
    return dispatch();
  };

  let downstreamRan = false;
  const dshOps = async (next) => {
    downstreamRan = true;
    const d = await next();
    return { ...d, additionalContexts: [{ note: "ops" }] };
  };
  const headroom = async (next) => {
    let downstream;
    try { downstream = await next(); } catch { downstream = { kind: "accept" }; }
    if (!downstream || downstream.kind === "block") return downstream;
    return { ...downstream, content: [{ type: "text", text: "COMPRESSED" }] };
  };

  const r = await waterfall(async () => ({ kind: "accept" }), headroom, dshOps);
  assert.equal(downstreamRan, true, "下游 dsh-ops 钩子必须执行");
  assert.ok(Array.isArray(r.additionalContexts), "下游 additionalContexts 必须保留");
  assert.equal(r.content[0].text, "COMPRESSED", "content 应被覆盖");
});

await t("block 决策必须原样透传", async () => {
  const waterfall = async (finalNext, ...listeners) => {
    let idx = 0;
    const dispatch = async () => {
      const cb = listeners[idx++];
      if (!cb) return finalNext();
      let called = false;
      const n = async () => { if (called) throw new Error("dup"); called = true; return dispatch(); };
      return cb(n);
    };
    return dispatch();
  };
  const blocking = async () => ({ kind: "block", feedback: "rejected" });
  const headroom = async (next) => {
    const downstream = await next();
    if (!downstream || downstream.kind === "block") return downstream;
    return { ...downstream, content: [{ type: "text", text: "X" }] };
  };
  const r = await waterfall(async () => ({ kind: "accept" }), headroom, blocking);
  assert.equal(r.kind, "block");
  assert.equal(r.content, undefined, "block 决策不能被覆盖 content");
});

await t("纯 INFO 日志不被清零（保留头尾）", async () => {
  const cfg = resolveConfig();
  const src = Array.from({ length: 400 }, (_, i) =>
    `2026-11-06 00:00:00 INFO step=${i + 1} ok`).join("\n");
  const r = await compressText(src, cfg);
  assert.equal(r.compressed, true);
  const kept = r.text.split("\n").filter((l) => l.trim()).length;
  assert.ok(kept >= 100, `保留行数应 >=100，实际 ${kept}`);
  assert.match(r.text, /折叠|omitted|compressed to/i, "应有省略标记");
  assert.match(r.text, /INFO step=1 /, "首行应保留");
  assert.match(r.text, /INFO step=400 /, "末行应保留");
});

await t("含 ERROR 的日志仍走 LogCompressor 且 ERROR 保留", async () => {
  const cfg = resolveConfig();
  const lines = [];
  for (let i = 1; i <= 400; i++) {
    if (i % 50 === 0) lines.push(`2026-11-06 00:00:00 ERROR step=${i} failed`);
    else if (i % 20 === 0) lines.push(`2026-11-06 00:00:00 WARN step=${i} retry=2`);
    else lines.push(`2026-11-06 00:00:00 INFO step=${i} ok`);
  }
  const r = await compressText(lines.join("\n"), cfg);
  assert.equal(r.compressed, true);
  const errs = (r.text.match(/ERROR/g) || []).length;
  assert.ok(errs >= 8, `ERROR 应保留至少 8 条，实际 ${errs}`);
});

await t("generic 行级抽样：头尾逐字保留", async () => {
  const cfg = resolveConfig();
  const src = Array.from({ length: 400 }, (_, i) =>
    `def func_${i + 1}(x):\n    return x + ${i + 1}`).join("\n");
  const r = await fetch(cfg.sidecarUrl + "/compress", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: src, kind: "generic" }),
  }).then((x) => x.json());
  assert.equal(r.compressed, true);
  assert.match(r.text, /压缩视图|折叠|omitted/i, "应有摘要头");
  const srcLines = src.split("\n");
  // 摘要头占前 2 行（说明行 + 分隔线），正文从第 3 行开始
  const bodyLines = r.text.split("\n").slice(2).filter((l, i, a) => !(i === a.length - 1 && l === ""));
  for (let i = 0; i < 80; i++) {
    assert.equal(bodyLines[i], srcLines[i], `头第 ${i} 行应逐字一致`);
  }
  assert.equal(bodyLines[bodyLines.length - 1], srcLines[srcLines.length - 1], "末行应逐字一致");
});

await t("内容过小的输出被拒绝替换", async () => {
  const cfg = resolveConfig();
  // 构造会让压缩器输出极短内容的输入
  const src = Array.from({ length: 400 }, (_, i) => `x${i}`).join("\n");
  const r = await fetch(cfg.sidecarUrl + "/compress", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: src, kind: "generic" }),
  }).then((x) => x.json());
  // 若输出 <200 字符且 <0.5%，必须 compressed=false
  if (r.out_chars < 200) {
    assert.equal(r.compressed, false, "过小输出应标记为未压缩");
  }
});

console.log("== sidecar-client 故障路径 ==");
await t("连接拒绝 -> 抛错（不静默）", async () => {
  const cfg = resolveConfig({ sidecarUrl: "http://127.0.0.1:9" });
  await assert.rejects(() => compressText(bigLog(100), cfg));
});
await t("超时(timeoutMs=1) -> 抛 AbortError", async () => {
  const cfg = resolveConfig({ timeoutMs: 1 });
  await assert.rejects(() => compressText(bigLog(3000), cfg), (e) => e.name === "AbortError" || /abort/i.test(String(e)));
});
await t("health 正常返回", async () => {
  const cfg = resolveConfig();
  const h = await health(cfg);
  assert.equal(h?.ok, true);
});
await t("retrieve 往返", async () => {
  const cfg = resolveConfig();
  const src = bigLog(500);
  const r = await compressText(src, cfg);
  assert.equal(r.compressed, true);
  const back = await retrieveText(r.hash, cfg);
  assert.equal(back, src, "retrieve 应与原文逐字一致");
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) { console.log("FAILURES:", failures.map(f => f[0]).join(", ")); process.exit(1); }