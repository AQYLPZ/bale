/** bale 配置解析：默认值保守，允许 profile patch 层覆盖。 */
export function resolveConfig(config = {}) {
  const cfg = {
    enabled: true,
    minChars: 8192,           // 小于不压（实测 60 行/2570 字符起压缩就有收益，8192 安全）
    maxChars: 33554432,        // 超过不压（32MB，防巨量输出卡死）
    // 只压这些工具的输出；run_code 覆盖 PTC 模式（desktop 默认）：程序打印/返回值的
    // 渲染结果就是模型所见，在它的 post-execute 替换 content 才能命中该链路。
    // 内层 pwsh/bash 等保留：native 模式（如 headless 实例）content 即模型所见。
    tools: ["ops_bash", "bash", "pwsh", "web_fetch", "run_code"],
    protectTools: ["read", "edit", "write", "grep", "glob"], // 显式保护（读代码要精确行）
    // text 已关闭（Kompress 在本机几乎无收益且耗时 8-27s）；
    // diff / search 于 2026-11-06 新增，覆盖 git diff 与 grep 输出。
    kinds: ["log", "json", "code", "diff", "search"],
    sidecarUrl: "http://127.0.0.1:8797",
    timeoutMs: 15000,         // sidecar 超时即放行原文
    replaceMode: "replace",   // replace | preview（preview 只加标记不替换）
    // 是否放行 kinds 列表之外、但 sidecar 成功压缩的类型。
    // true（默认）：以 sidecar 的判断为准，新增压缩器无需重启 DSH；
    // false：严格按 kinds 白名单过滤。
    kindsAreOpen: true,
    // sidecar 自动拉起（DSH 启动时检测，不在则用 pythonw 无窗口拉起）。
    // 默认从插件源码位置推导（测试环境）；desktop 正式部署时在 profile config 里写绝对路径。
    sidecarPython: null,      // 例 "<PROFILE>/.headroom/venv/Scripts/pythonw.exe"
    sidecarScript: null,      // 例 "<PROFILE>/node_modules/bale/sidecar/sidecar.py"
    // 运行时数据目录（迁移后全部在 profile 的 .headroom/ 下，pnpm 不碰）：
    //   storeDir  -> sidecar 原文仓库（HEADROOM_STORE_DIR）
    //   wsDir     -> sidecar 工作区（HEADROOM_WORKSPACE_DIR）
    //   logFile   -> 插件日志（不配置则落到插件根下的 headroom-auto.log）
    // null 表示由 sidecar-client 按插件源码位置推导（测试环境）。
    storeDir: null,           // 例 "<PROFILE>/.headroom/store"
    wsDir: null,              // 例 "<PROFILE>/.headroom/headroom_ws"
    logFile: null,            // 例 "<PROFILE>/.headroom/logs/headroom-auto.log"
    ...config,
  };
  cfg.tools = new Set(Array.isArray(cfg.tools) ? cfg.tools : [cfg.tools]);
  cfg.protectTools = new Set(Array.isArray(cfg.protectTools) ? cfg.protectTools : [cfg.protectTools]);
  cfg.kinds = new Set(Array.isArray(cfg.kinds) ? cfg.kinds : [cfg.kinds]);
  return cfg;
}