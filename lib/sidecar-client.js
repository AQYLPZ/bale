/** sidecar 客户端：fetch + 超时；任何异常向上抛，由调用方 fail-open。 */
import { spawn } from "node:child_process";
import { dirname as pathDirname } from "node:path";
import { fileURLToPath } from "node:url";

let lastEnsureAt = 0;

/**
 * 熔断状态：sidecar 起不来时，避免每次工具调用都白等 8 秒。
 * - failStreak：连续失败次数
 * - openUntil：熔断窗口结束时间（在此之前不再尝试拉起，直接放行）
 * - 指数退避：30s → 60s → 120s，上限 10 分钟
 */
let failStreak = 0;
let openUntil = 0;
const BACKOFF_BASE_MS = 30_000;
const BACKOFF_MAX_MS = 600_000;

/** sidecar 当前是否处于熔断（暂停尝试）状态。 */
export function isCircuitOpen() {
  return Date.now() < openUntil;
}

/** 记录一次拉起失败，推进熔断窗口。 */
function tripCircuit() {
  failStreak += 1;
  const backoff = Math.min(BACKOFF_BASE_MS * 2 ** (failStreak - 1), BACKOFF_MAX_MS);
  openUntil = Date.now() + backoff;
  return backoff;
}

/** 成功联系到 sidecar 时重置熔断。 */
function resetCircuit() {
  failStreak = 0;
  openUntil = 0;
}

/**
 * 插件源码位置推导 sidecar 路径（lib/ 上两级 = 插件根）。
 * 只适用于「插件目录旁就是 venv/ 与 sidecar/」的隔离测试环境；
 * 真实部署（node_modules 内）必须由 config 的 sidecarPython / sidecarScript 覆盖，
 * 因为 pnpm 更新会整目录替换 node_modules/headroom-auto，venv 绝不能放在里面。
 */
function defaultSidecarPaths() {
  const taskPath = fileURLToPath(new URL("../../", import.meta.url));
  return {
    python: taskPath + "venv/Scripts/pythonw.exe",
    script: taskPath + "sidecar/sidecar.py",
  };
}

/**
 * 确保 sidecar 在运行：健康探测失败则用 pythonw 无窗口拉起（detached，独立常驻）。
 * - 30 秒节流，避免反复 spawn
 * - waitMs 内轮询等待就绪（python 启动约 1-2 秒）
 * - 返回是否就绪；任何失败返回 false（调用方 fail-open）
 */
export async function ensureSidecar(cfg, { waitMs = 15000, force = false } = {}) {
  const now = Date.now();
  // 熔断窗口内直接放弃（force 也不能绕过：拉起失败时反复重试只会拖慢调用）
  if (isCircuitOpen()) return false;
  if (!force && now - lastEnsureAt < 30000) return false;
  lastEnsureAt = now;
  try {
    const h = await health(cfg);
    if (h?.ok) { resetCircuit(); return true; }
  } catch { /* 未运行，继续拉起 */ }
  const fallback = defaultSidecarPaths();
  const python = cfg.sidecarPython || fallback.python;
  const script = cfg.sidecarScript || fallback.script;
  let port = "8797";
  try {
    port = new URL(cfg.sidecarUrl).port || "8797";
  } catch { /* 用默认端口 */ }
  // 运行时数据目录通过环境变量下发给 sidecar：
  //   HEADROOM_STORE_DIR      -> 原文仓库（不配则 sidecar 默认放到 sidecar/store/）
  //   HEADROOM_WORKSPACE_DIR  -> sidecar 工作区（不配则放到 sidecar/headroom_ws/）
  // 迁移后这两处都在 profile 的 .headroom/ 下，pnpm 更新 node_modules 时不会丢。
  const env = { ...process.env };
  if (cfg.storeDir) env.HEADROOM_STORE_DIR = cfg.storeDir;
  if (cfg.wsDir) env.HEADROOM_WORKSPACE_DIR = cfg.wsDir;
  // sidecar 自身日志（调试用）；不配则落在 sidecar/ 目录，pnpm 更新时会丢
  if (cfg.logFile) env.HEADROOM_LOG_DIR = pathDirname(cfg.logFile);
  try {
    const child = spawn(python, ["-I", script, "--port", port], {
      detached: true,
      stdio: "ignore",
      windowsHide: true,
      env,
    });
    child.unref();
  } catch {
    return false;
  }
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 500));
    try {
      const h = await health(cfg);
      if (h?.ok) { resetCircuit(); return true; }
    } catch { /* 还没就绪，继续等 */ }
  }
  const backoff = tripCircuit();
  console.warn(`headroom-auto: sidecar 拉起失败，熔断 ${Math.round(backoff / 1000)}s 后再试`);
  return false;
}

/** 压缩调用 + 连接失败时自动拉起 sidecar 重试一次。
 *
 * 失败处理要点：
 * 1. 熔断中直接抛错放行，不再每次白等；
 * 2. 去掉 force:true —— 它绕过了 30 秒节流，导致每次失败调用都完整等待；
 * 3. 等待上限从 8s 降到 3s（Python 冷启动实测 1-2s，3s 足够）。
 */
export async function compressWithEnsure(fn, cfg) {
  try {
    const r = await fn();
    resetCircuit();
    return r;
  } catch (e) {
    const isConnErr = /fetch failed|ECONNREFUSED|network|aborted/i.test(String(e));
    // 熔断窗口内：不再尝试拉起，立即放行（避免每次调用卡 8 秒）
    if (isConnErr && !isCircuitOpen()) {
      const ok = await ensureSidecar(cfg, { waitMs: 3000 });
      if (ok) {
        try { return await fn(); } catch (e2) { tripCircuit(); throw e2; }
      }
    } else if (isConnErr) {
      tripCircuit();
    }
    throw e;
  }
}

export async function compressText(text, cfg, kind) {
  return compressPayload(kind ? { text, kind } : { text }, cfg);
}

/** 按文件路径压缩（spill 全文场景）：sidecar 按原始字节存储，retrieve 字节保真。
 *  全文可能远大于截断版，超时放宽到 4 倍。 */
export async function compressPath(path, cfg) {
  return compressPayload({ path }, cfg, Math.max(cfg.timeoutMs * 4, 60000));
}

async function compressPayload(payload, cfg, timeoutMs = cfg.timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(cfg.sidecarUrl + "/compress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`sidecar /compress -> ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function retrieveText(hash, cfg) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), Math.max(cfg.timeoutMs * 4, 60000));
  try {
    const res = await fetch(cfg.sidecarUrl + "/retrieve/" + encodeURIComponent(hash), {
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`sidecar /retrieve -> ${res.status}`);
    // sidecar 按原始字节存储；当前调用方的输入都是 text，原始字节即 UTF-8
    const buf = await res.arrayBuffer();
    return new TextDecoder("utf-8").decode(buf);
  } finally {
    clearTimeout(timer);
  }
}

export async function health(cfg) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 3000);
  try {
    const res = await fetch(cfg.sidecarUrl + "/health", { signal: ctrl.signal });
    return res.ok ? await res.json() : null;
  } finally {
    clearTimeout(timer);
  }
}