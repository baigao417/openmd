# OpenCLI 升级手册

OpenCLI 是 Openmd 的内部组件。更新先做离线契约与隔离金丝雀，再评审 pin；
金丝雀本身只出报告。维护时不向上游提 PR，不原地改写托管 npm 文件。

## 1. 确认依赖和现场

```sh
python scripts/opencli_inventory.py --check
python scripts/opencli_inventory.py --contracts
python -m openmd.routing.generate --check
python -m openmd doctor --profile web --json
```

命令清单从 backends、声明、owned adapters 和 dispatcher 调用点静态提取，
产出 `docs/opencli-inventory.generated.{md,json}`。`declarations.json` 的
`opencli_contracts` 登记上游必要列，自带适配器列来自其 `cli({...})`。
新增调用或分支参数后重新生成，并检查生成差异；不把规划中的 provider 当成实际调用。
运行时按 browser metadata 注入的全局参数也单独登记和校验。

契约只读 `managed_tools.managed_package_root()` 解析的包，或显式传入的
`--package-root <fixture-or-candidate>`。它静态读取 `clis/<site>/<command>.js`
中的 literal `cli({...})`，核对站点、命令、位置参数、选项和必要列；browser 内置
命令核对其 Commander namespace。未知或动态 metadata 失败，不执行 JS、不联网、不登录。
synthetic fixtures 和“改坏 Text 列”回归只证明检查器能发现漂移。

## 2. 补丁的唯一来源

`src/openmd/runtime/opencli_patches.py` 保存目标、精确原文/哈希、替换、版本、
出现次数、描述和校验方式：

- bridge 顺序补丁由 `scripts/vendor_opencli_bridge.py` 消费；生成文件禁止手改。
- AI 对话 loader 在单个 Node 进程内替换源码，不落盘修改 npm 文件；版本或任一标记不符即报错。
- Xiaoe/Tingwu dispatcher 的上游 exports/执行签名和 bundled overlay 哈希同表检查。

doctor JSON 的 `profile_diagnostics.opencli_patches` 显示总数、版本、bridge 检查范围和
逐项结果；人话视图显示未命中的 patch ID。bundled bridge 检查不能证明 Chrome 已加载该副本。
新增 detail loader 时把新站点编辑加入清单，保留相应业务回归，不再维护第二份内联补丁。

## 3. 运行隔离金丝雀

```sh
python scripts/opencli_canary.py 2.0.3 --probe-config <private-samples.json> --out <private-run>/canary-2.0.3
python scripts/opencli_canary.py <exact-candidate-version> --probe-config <private-samples.json> --out <private-run>/canary-candidate
```

本机维护运行先设置组名，再用当轮 `heavy.py --lock browser -- <command>` 包住这条命令。
完整 pytest、npm 重任务用 heavy；所有真实 Chrome 操作同时占 browser。不要在锁内再套同一把锁。
`OPENMD_NPM_REGISTRY` 可选镜像；不接受 latest、范围或 URL 作为版本。

执行顺序：

1. 在 task-owned 临时目录 npm 安装精确版本，独立 cache，禁用 lifecycle scripts；退出自动回收。
2. 核对候选包身份，执行全部命令契约。
3. 在内存中重放全部补丁/ABI/overlay 校验。版本不适用或任一匹配失败，停止候选真实 probe。
4. 复用已经运行的 pinned daemon 和已经准备好的 OpenCLI 配置，只读跑 baseline/candidate
   两次 `openmd probe`。各自临时 location config 选择相应 host；进程内 resolver 让插件引用候选包。
   canary 模式禁止重指共享 Node host、准备/安装插件或创建用户配置目录。
5. 对比 configured platforms、状态、真实分支、来源、字数、条数和结构化错误。
   结果不全、基线失败、候选失败、分支变化或计数回退均阻止直接升级。

`--probe-config` 缺省时找现有 probe 配置（也查显式 location config 的同目录）；
样本缺失、daemon 未准备或网络不通均报告 BLOCKED，不自动补登录、改账号、启动替代 daemon。
用户排除的 claude.ai 对话不跑。probe 永不下载媒体、运行 ASR/OCR 或上传云端；
依赖这些能力的样本可能如实失败，不能为了绿灯打开权限或改成 metadata 成功。

输出：`report.md` 一页摘要、`report.json` 详细契约与相关命令 metadata 差异、
`baseline-probe.json` / `candidate-probe.json` 脱敏计数回执。它们不含原链接、
对话/消息正文、凭证、第三方 stdout/stderr。样本 URL 只进入私有配置和短命任务目录。
报告 PASS 的范围仅限当次配置的样本，不代表所有平台或每个 fallback 都已覆盖。

## 4. 评审并准备升级

金丝雀不改 pin。确认可升级后，在获得维护授权的分支里：

1. 对比相关命令 metadata 和补丁失败 ID，新增先红后绿的漂移测试。
2. 在 `managed_tools.OPENCLI_VERSION` 与统一补丁清单中同步 npm tag、支持版本、
   bridge 版本/发行包 SHA256；重新核对上游原文与次数，不放宽成“匹配不上跳过”。
3. 用生成器生成 bridge，并更新清单内生成 bridge/overlay 的 SHA256。
   `BRIDGE_GENERATED_SHA256` 是生成文件的 LF 文本 SHA256；dispatcher overlay 同理。
4. 运行 `vendor_opencli_bridge.py --check`，重新生成声明和 inventory，再跑候选金丝雀。
5. 普通 shell 中完整 `python -m pytest`（重任务加锁），审核差异和真实产物回执。
6. 合并后联合验收扩展重载与实际浏览器行为。安装切换、用户配置迁移、发布分别按明确授权执行。

不要重启/停止用户自己的 Chrome 或其他版本 daemon，也不把候选版本直接安装进共享托管目录。
失败时保留脱敏回执、修正候选分支；当前 pin 和运行时继续使用已有版本。

## 5. 路线验证日期

每条 bundled route 的 `last_verified` 为有证据的最近真实成功日期，格式 `YYYY-MM-DD`；
没有证据则显式 `null`。2026-11-18 初始日期来自维护验收中成功提取且已读回的路线，
证据指针留在私有运行目录，不复制用户产物或消息正文到公开包。

日期表示该 source/capability 至少一条真实通路在该日取得过产物；不宣称 chain 里的所有
backends、同平台所有子站点、或今天仍然可用。失败不更新成功日期，fixture 通过也不更新。
生成路由表显示日期；doctor 对严格超过 30 天的路线提醒复验，未知与未来日期单独报告，
不把“日期新”转换成可用性或完整性等级。更新日期后运行声明生成器并提交投影。
