# RF EDA · AI 原理图设计助手

版本 **0.2.5**。Python 3.10+；AI 服务 API 1.x、Graph IR `ai-eda-graph-ir/v2.0`。

本版采用 **仅后端反馈 + 手动更新原理图**。首轮和反馈成功后先保存设计结果，不操作画布；用户点击统一“更新原理图”按钮后，才清空当前页并重新解析 Graph IR、创建符号和导线。**不读取图纸快照、不审阅或合并人工改图、不要求画布与后端对齐。**

## 给 KiCad 宿主开发者：本次需要实现哪些接口

**本仓库是 Python 插件，不包含 KiCad C++ 源码。本次建议新增一个 `ClearSchematicPage` 处理器，复用并核对已有的绘图接口。** 如果定制宿主已完整支持当前页 `GetItems + DeleteItems`，插件也可走删除回退，无需重复增加清空接口；只返回部分对象的枚举不满足要求。

### 1. 开发清单

| 接口 | 本次开发要求 | 插件实际依赖的行为 |
| --- | --- | --- |
| **`kiapi.schematic.commands.ClearSchematicPage`** | **推荐新增并注册原理图 IPC 处理器** | 在当前客户端事务内清空请求指定页的全部旧设计对象，包含人工新增的符号、线、标签等；保留图页、工程及其他图页文件 |
| `BeginCommit / EndCommit` | 复用并验证；不满足时补齐 | 清空与随后所有创建属于同一事务；`CMA_DROP` 整体回滚，`CMA_COMMIT` 整体提交，提交后可一次撤销恢复旧图 |
| `GetActiveSchematicContext` | 复用并验证 | 返回有效文档、当前图页、网格及坐标约定；插件仅在手动更新时核对目标，不读取画布内容 |
| `SearchSymbolLibrary / GetSymbolLibraryDefinition` | 复用并验证 | 精确库标识、单元/样式、真实引脚及符号几何，供插件匹配和布局 |
| `CreateItems` | 复用并验证 | 支持符号与导线；逐项返回结果及实际创建对象，保留请求 UUID，符号响应包含真实引脚位置和几何，不能只返回成功布尔值 |
| `GetItems + DeleteItems` | 可选替代路径 | 仅当清空命令返回 `AS_UNHANDLED` 时回退：完整枚举当前页全部对象 UUID，再逐项删除；同样加入上述事务 |

“复用并验证”不表示当前安装的 KiCad 已全部通过联调。插件针对定制原理图 IPC，不能仅凭 KiCad 版本号判断兼容性；宿主需与仓库中的 Protobuf 消息及实际响应字段匹配。

### 2. 新增清空命令的协议和实现约束

直接使用仓库里的 **[schematic_clear.proto](plugins/protocol/schematic/schematic_clear.proto)**，与宿主原有 `common/types/base_types.proto` 一起生成 C++ 消息，接入原理图 IPC 命令分发并重新编译宿主。Python 端消息与调用已接好，安装插件 ZIP 本身不会给 KiCad 增加处理器。

- 请求全名 `kiapi.schematic.commands.ClearSchematicPage`：`document = 1`（`DocumentSpecifier`）、`request_id = 2`（`string`）。
- 响应全名 `kiapi.schematic.commands.ClearSchematicPageResponse`：`success = 1`（`bool`）、`deleted_count = 2`（`uint32`）、`message = 3`（`string`）。保持包名、字段编号及 `Any` 类型全名一致。
- 通过现有 `ApiRequest.header.client_name` 关联该客户端已开启的事务。核对目标图页和事务归属；没有有效事务、目标失效或不一致时拒绝操作，不能清理别的当前页。
- 删除全部旧对象必须加入同一个事务，处理器不能自行保存或提交。空图页返回 `success=true, deleted_count=0`；失败不能报成功，已发生的删除必须能由事务回滚恢复。
- 保持现有 `ApiResponse.status` 及鉴权约定。只有未实现命令返回 `AS_UNHANDLED`；实际执行失败不得冒充“未实现”触发第二条删除路径。

插件调用顺序（首轮与反馈轮相同）：

```text
生成并缓存 Graph IR → 用户点击“更新原理图”并确认
→ 核对目标页、坐标、新图及符号库 → 保存本地操作日志
→ BeginCommit → ClearSchematicPage → CreateItems（符号）→ CreateItems（导线）
→ 全部成功：EndCommit(CMA_COMMIT)；任一步失败：EndCommit(CMA_DROP)
```

Graph IR 的解析、布局、布线由插件完成，KiCad 无需新增“接收 Graph IR”接口。本版无需新增图纸快照、`GetItemsById`、选中对象回读、画布反向转 JSON 或向 LLM 同步人工改图的接口；`GetItems` 只可能用于上述清空回退。

坐标适配为 Eeschema **10,000 IU/mm**，识别宿主 `schematic_internal_unit`。请核对上下文、库几何、创建响应中的坐标均符合此约定；不能因字段名含 `x_nm` 就按 PCB 纳米单位填值。未知尺度会阻止写入。`RF_EDA_SCHEMATIC_UNITS_PER_MM=10000` 仅供核实实际比例后显式配置，不是任意缩放开关。

### 3. 编译后的联调验收与交付

1. 首轮/反馈生成成功时画布保持原状；点击同一个更新按钮后，空图正常生成，有旧图则整页替换，人工新增对象也被清除，其他图页保持原状。
2. 故意让清空后某个创建失败：回滚后完整恢复旧图；成功更新后，用户一次撤销能恢复更新前内容。
3. 缺少清空能力、错误目标页、事务无效时保留旧图并明确报错。连接中断或提交结果未知时不能自动重放清空。
4. 符号型号/单元/引脚、坐标比例及导线连接与创建响应一致；不能用模拟成功响应代替真实创建结果。

交付时注明 KiCad 源码分支/提交号、构建版本、支持的接口和上述实机验收结果，并提供可运行的定制构建及相应依赖。这里的离线测试不代表宿主已实现或实机联调已通过。完整事务说明见 [手动更新与清空图页接口](docs/手动更新与清空图页接口.md)；定位插件调用看 [api_client.py](plugins/ai_schematic/api_client.py) 和 [generator.py](plugins/ai_schematic/generator.py)。

## 安装与运行条件

运行 `python tools/package_plugin.py`，生成 `dist/rf-eda-ai-schematic-0.2.5.zip`，通过 KiCad PCM 安装。插件标识仍为 `org.rfeda.ai-schematic-generator`；升级后关闭旧插件窗口再重新打开，源码更新不会自动替换已安装副本。旧的其他标识安装应卸载，避免运行错误副本。

依赖由 KiCad 插件环境安装，亦可在对应 Python 环境执行 `python -m pip install -r plugins/requirements.txt`。本版没有新增 WebView 依赖。`KICAD_API_SOCKET`、`KICAD_API_TOKEN` 由编辑器注入，不是 AI 服务令牌。

AI 服务需要支持 `/api/v1/capabilities` 的 `revision.canvas_policies` 包含 `backend_only`。此扩展在独立的 AI 服务项目中，**不随本仓库一起发布**；请同步更新服务代码并重启，仅安装插件不够。旧客户端 verify_local/dirty 协议继续兼容；本插件反馈不发送 client_graph_dirty，不假装检查过画布。

## 使用流程

主窗口默认 500×740、支持 420px 宽度，采用顶部页签、石墨深色和绿色强调，是独立非模态辅助窗口。RF EDA 标题只出现一次。

1. 在“服务连接”填写根地址，例如 `http://127.0.0.1:8000`，按需输入服务令牌并检测连接。模型厂商 KEY 和管理令牌另行管理。
2. 在设计页选择 Word、输入文字或同时提供两者，点“开始设计”。只有一个首轮请求，附件与文字共同用于同一任务；支持移除附件、保留文字和 Ctrl+Enter 发送。
3. 运行阶段、子任务、执行/阶段/排队/等待时间及心跳随真实事件显示，日志可展开。任务运行时不估算整体百分比。
4. 结果成功后可查看报告和“设计指标要求”、继续反馈，或点击 **更新原理图**。生成成功不等于已画到画布。
5. 反馈基于服务端最新成功版本、需求及设计状态，生成新图后仍等待用户点击同一个更新按钮。人工改动不会进入 AI，下一次整页更新也不保留它。
6. 点击更新并确认后，在当前活动原理图页清空旧内容、创建新图，全部成功才提交。只清当前页，不删除整个工程或其他页文件。
7. “结束本轮”只保存在本地，不调用模型或不存在的 finish 接口。“继续反馈”重新开启输入；不要求先更新画布。

本地 Graph IR 从任务页导入，也先保留为待应用结果，不立即写图。后端当前版本和画布已应用版本分别显示；可在尚未绘图时进行多轮讨论。清空失败或绘图失败保留后端结果，可修复接口后再次应用，不需要重跑 AI。

尚无待应用 Graph IR 时，主操作显示 **清空当前图页**。确认后只删除当前活动图页的全部对象，不生成新图；此操作同样需要宿主支持整页清空及事务回滚。

## 记录如何清除

| 操作 | 清理范围 |
| --- | --- |
| 清空日志 | 当前运行日志文本 |
| 清空对话 | 持久清除本会话对话，重开窗口不恢复；保留任务和画布 |
| 任务页顶部“清除任务记录” | 本会话对话、输入、任务/版本列表；不删除服务端任务文件，不操作原理图，保留必要绘图收据 |
| 新设计 | 清空当前对话和输入，开始独立设计；历史索引仍可查看或另行清除 |

待确认网络请求、运行任务和结果未知的绘图不能静默丢弃跟踪。“清空日志”以前没有清掉对话，是因为二者独立保存；本版分别提供入口。

## 设计指标要求

从当前后端成功任务的 `/requirements` 读取，显示与 HTML 报告对应的要求编号、指标名称、要求值、单位、比较、来源、采用状态、依据。重点呈现原文提取指标，保留零值与待定值，区分协议、用户输入、AI 建议和假设。

支持关键词筛选、选中行查看提取原文/位置/条件，以及复制表格。默认不再弹出整段 JSON。采用要求并不表示 RF 数值已验证。

## 报告、管理与恢复

- 任务页保留本轮报告、当前后端版本报告、指标、Graph IR 导出、历史只读预览和按任务 ID 恢复。历史预览不替换当前结果，不改变原请求跟踪。
- 网络响应未知时保存原 request_id、不可变 Word/文字/反馈载荷；恢复使用同一请求，不新建付费任务。停止等待或关闭不取消后端任务。
- 本地会话在 `%LOCALAPPDATA%/RFEDA/AI Schematic Generator/sessions`，按服务和工作区分文件，采用原子写入、落盘、跨进程锁/CAS；损坏或并发冲突不静默重置。旧图纸会话可通过“切换图纸会话”显式打开，不会在生成时自动切换画布。
- 更新前保存绘图操作日志及 UUID，不保存图纸快照。事务确认回滚后可重试；提交响应未知时不会自动再清图。用户先在 KiCad 处理旧事务或重新打开编辑器，再通过“处理未确认操作”明确允许重新整页更新；这不是图纸校验，且不会立即绘图。
- 管理窗口保留配置校验/保存/应用/恢复、厂商及 KEY 保留/替换/清除、可能计费的模型测试确认、符号库暂存构建/Neo4j 发布及报告。路径均属于服务端电脑。
- 服务令牌只留窗口内存，切换服务清除旧令牌；模型 KEY、独立管理令牌、Neo4j 密码各自独立，不进入设计缓存。

绘图预先核对 Graph IR、精确型号、单元和引脚，布局/路由失败会取消操作。更新模式只核对创建响应中的几何与连接，不是当前图回读验证或完整 KiCad ERC，也不求解 RF 数值性能。

## 文件用途

以下 Python 模块位于 `plugins/ai_schematic/`：

| 文件 | 用途 |
| --- | --- |
| `ui.py` / `ui_workspace.py` / `ui_theme.py` | 辅助窗口控制、布局和主题；手动更新、清记录等入口 |
| `requirements_ui.py` | 设计指标表、筛选、提取原文详情与复制 |
| `workflow.py` | AI 成功版本与画布应用分离，首轮/反馈/恢复/清理状态机 |
| `ai_service.py` | HTTP、鉴权、同源/重定向保护、接口载荷验证及下载 |
| `session_store.py` / `session.py` | 冻结请求、隔离缓存、原子写入、锁/CAS及旧格式读取 |
| `api_client.py` | KiCad IPC、整页清空命令与枚举 UUID 删除回退 |
| `generator.py` | Graph IR 到符号/导线，用户手动整页更新、事务与操作日志 |
| `graph_ir.py` / `symbol_matching.py` | 图完整性、绑定型号、引脚及单元匹配 |
| `layout.py` / `routing.py` | 布局与导线路由 |
| `snapshot.py` | 几何/连通性工具及旧回读兼容代码；本版正常流程不采集画布快照 |
| `management_ui.py` / `management_controller.py` | 服务配置、厂商、模型和符号库管理 |

其他目录：`plugins/generate_schematic.py` 为入口与中文字体初始化；`plugins/generated_proto/` 为通信消息；`plugins/protocol/schematic/schematic_clear.proto` 是最小清空扩展；`plugins/tests/` 为离线回归；`tools/package_plugin.py` 打包白名单安装 ZIP。

## 验证

在已安装依赖的插件 Python 环境运行：

```powershell
$env:PYTHONPATH = "$PWD/plugins;$PWD/plugins/generated_proto"
python -B -m unittest discover -s plugins/tests -v
```

离线测试使用 HTTP/IPC 替身、临时目录和 Qt offscreen，不连接真实 AI、KiCad 或 Neo4j。真实部署需核对宿主整页清空与事务回滚、实际符号库及新版本服务协议。
