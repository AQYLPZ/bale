# headroom-ai 依赖精简重打包工具

## 修改说明

本包衍生自 headroom-ai 0.42.0（Apache-2.0），**仅**修改了 METADATA 中的
Requires-Dist 声明，移除了与本项目无关的 LLM 代理相关依赖。
Python 源码与 _core.pyd 二进制未做任何修改。

版本号改为 `0.42.0.post1`，以区分上游原版。`sboms/headroom-py.cyclonedx.json`
（440 KB）未修改，其内容对应上游 0.42.0 原版。

## 为什么需要重打包

headroom-ai 0.42.0 的 base 依赖包含 litellm 全家桶（litellm、openai、
tokenizers、huggingface-hub、hf-xet、boto3/botocore 等），另有 ast-grep-cli
会往 Scripts/ 写两个各约 50 MB 的二进制。这些包只服务于 proxy / ml / text 等
未启用的功能，在 log/json/code/diff/search 压缩路径上从不被 import，却使 venv
多占约 355 MB（其中 onnxruntime/numpy 另有 81 MB 来自原 requirements.txt 显式声明）。

重打包不修改任何 Python 源码，只收缩依赖闭包。

## 必须保留的依赖（勿删）

- `httpx>=0.24.0`：METADATA 原版只在 extra 中出现，但压缩路径硬依赖
  `smart_crusher.py -> ccr.tool_injection -> ccr/__init__.py ->
  ccr/batch_processor.py -> import httpx`。删掉 litellm 后必须显式保留，
  否则 json 压缩直接 ImportError（连带保留 anyio/certifi/httpcore/idna）。
- `opentelemetry-api`：`headroom/observability/metrics.py` 无条件导入。
- `tree-sitter` / `tree-sitter-language-pack`：code 压缩类型必需。
- `typing_extensions`：由 opentelemetry-api 传递引入，无需手写。

## 使用

```bash
python tools/repack/repack.py \
  --src /path/to/headroom_ai-0.42.0-cp310-abi3-win_amd64.whl \
  --out-dir vendor/wheels
```

输出：`vendor/wheels/headroom_ai-0.42.0.post1-cp310-abi3-win_amd64.whl`

脚本会：

1. 解包原始 wheel，按 `tools/repack/README.md` 的清单重写 METADATA 的
   Requires-Dist（删除全部 extra 分支，写入 7 条无条件依赖）；
2. 将 Version 改为 `0.42.0.post1`，同步重命名 dist-info 目录；
3. 逐文件重算 RECORD（METADATA 行含 sha256，必须重算）；
4. 重新打包为 zip（路径分隔符 `/`），并做 testzip、RECORD、METADATA 自检。

## 验收基线

重打包后应在隔离 venv 中跑 5 种压缩，与上游 0.42.0 的基线逐项比对
（详见 `docs/dependency-slimming-plan.md` §1.4）：

```
log      in=23590   out=103     sha=4fda7083b9f99b54
json     in=25817   out=15679   sha=a3b9495cc7eec9c2
code     in=27380   out=32979   sha=590d391c4c0b5cb3
diff     in=15300   out=2594    sha=1445f42373acf799
search   in=21193   out=2429    sha=09ee7a8639b2eab3
```

`code` 输出大于输入是正常现象（高度重复的合成代码，tree_sitter 判定
无可压空间后原样返回），按 sha256 比对即可。

## 上游升级时的重打靶流程

1. 从 PyPI 下载新版本 wheel，记录 URL / 大小 / sha256 到 `SOURCE.txt`；
2. 检查新版本 METADATA 的 base 依赖是否仍包含 litellm 全家桶；
3. 用新版本号更新 `repack.py` 中的 `OLD_VERSION` / `NEW_VERSION`；
4. 重新确认运行时真实依赖（至少覆盖 httpx、opentelemetry-api、
   tree-sitter、tree-sitter-language-pack、pydantic、click、rich）；
5. 重打包并在隔离 venv 中跑 5 种压缩回归，确认输出与旧版一致或明确
   记录了算法变更；
6. 更新 `requirements.txt` 的版本锁定。

## 许可

上游 headroom-ai 采用 Apache-2.0。本重打包产物同样遵循 Apache-2.0；
修改声明见本文件开头。
