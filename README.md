# RF EDA · AI 原理图设计助手

版本 **0.2.6**。面向 KiCad 的紧凑型 AI 设计辅助窗口，支持技术协议上传、文字需求、连续设计反馈、指标与报告查看，以及一键更新原理图。

## 从需求到原理图

1. **描述设计目标**：上传 Word 技术协议、输入文字，或将两者结合提交同一项设计任务。
2. **查看设计进展**：实时显示 AI 分析阶段、子任务、运行时间与服务事件，长任务可停止等待并稍后恢复。
3. **检查设计结果**：通过“设计指标要求”表格、HTML 报告与版本记录查看提取的指标、设计方案和修改结果。
4. **持续完善方案**：在反馈入口提出修改要求、咨询设计问题，或补充上一轮信息。
5. **应用到 KiCad**：点击 **更新原理图**，确认后将当前设计结果应用到活动图页；首轮与反馈轮使用同一个入口。

更新会替换当前图页的全部原有内容，执行前请保存需要保留的修改。尚无待应用结果时，主操作显示 **清空当前图页**，确认后清空活动图页。

## 页面与主要功能

主窗口默认 500×740，支持 420px 宽度，采用顶部页签和非模态交互，便于与 KiCad 画布同时使用。

| 页面 | 功能 |
| --- | --- |
| 设计助手 | Word/文字输入、对话与反馈、实时状态与日志、更新或清空当前图页 |
| 任务与版本 | 恢复任务、设计版本记录、HTML 报告、设计指标要求、Graph IR 导入/导出 |
| 服务连接 | 本机/局域网服务地址、访问令牌、连接检测、符号知识库入口 |
| 设置 | 服务配置、模型厂商与 API Key、模型测试、符号库构建与 Neo4j 发布 |

“设计指标要求”按表格显示要求编号、指标名称、原始值、单位、比较条件、来源、采用状态与依据，支持筛选、原文详情和复制。来源和假设分别标识，便于评审。

生成的设计版本与画布已应用版本分别记录。可先讨论和查看结果，再选择应用时机；本地 Graph IR 导入同样通过更新按钮应用。“结束本轮”保存本轮状态，“继续反馈”重新开启输入。

## 安装与连接

需要 Python 3.10+、匹配的 KiCad 原理图 IPC 构建、AI 服务 API 1.x，以及 Graph IR `ai-eda-graph-ir/v2.0`。

```powershell
python tools/package_plugin.py
```

生成 `dist/rf-eda-ai-schematic-0.2.6.zip`，通过 KiCad PCM 安装。插件标识为 `org.rfeda.ai-schematic-generator`。升级后关闭旧插件窗口，再重新打开；源码修改需重新打包安装。依赖由 KiCad 插件环境安装，亦可在对应 Python 环境执行：

```powershell
python -m pip install -r plugins/requirements.txt
```

在“服务连接”填写服务根地址，例如 `http://127.0.0.1:8000`；按需填写访问令牌并检测连接。服务令牌、模型厂商 API Key 与管理令牌各自独立。`KICAD_API_SOCKET`、`KICAD_API_TOKEN` 由 KiCad 编辑器注入。

AI 服务通过 `/api/v1/capabilities` 声明能力，当前反馈要求 `revision.canvas_policies` 包含 `backend_only`。AI 服务是独立部署的项目，升级后按需重启服务。

## 原理图接口与事务

清空和更新功能沿用定制 KiCad 已提供的原理图 IPC 接口，本次版本不新增宿主接口。

| 现有接口 | 插件用途 |
| --- | --- |
| `GetActiveSchematicContext` | 确认活动图页、网格与坐标约定 |
| `SearchSymbolLibrary` / `GetSymbolLibraryDefinition` | 匹配真实符号、单元、引脚与几何 |
| `ClearSchematicPage` | 在当前事务内清空指定图页 |
| `CreateItems` | 创建符号与导线，返回逐项结果和实际对象 |
| `BeginCommit` / `EndCommit` | 将清空与创建作为一次操作提交，失败整体回滚 |

```text
设计结果就绪 → 用户点击更新并确认
→ 核对目标图页、符号与引脚 → BeginCommit
→ ClearSchematicPage → CreateItems（符号）→ CreateItems（导线）
→ 全部成功提交；发生失败则回滚
```

清空仅作用于目标图页内容，不删除工程或其他图页文件。插件保留 `GetItems + DeleteItems` 删除回退适配；主流程不依赖图纸快照。Graph IR 的解析、布局与布线由插件完成。

开发维护约定、消息字段及已有 `.proto` 见 [手动更新与清空图页接口](docs/手动更新与清空图页接口.md)。坐标适配为 Eeschema **10,000 IU/mm**，识别 `schematic_internal_unit`；宿主的上下文、库几何和创建响应需使用同一约定。

## 会话与记录

| 操作 | 清理范围 |
| --- | --- |
| 清空日志 | 当前运行日志 |
| 清空对话 | 当前会话对话，保留任务和画布 |
| 清除任务记录 | 本会话的对话、输入、任务与版本展示；保留原理图和服务端文件 |
| 新设计 | 清空当前对话和输入，历史任务保留在版本记录中 |

会话自动保存在 `%LOCALAPPDATA%/RFEDA/AI Schematic Generator/sessions`，按服务和工作区隔离。网络重试保留原请求及 ID；停止等待或关闭窗口后，服务端任务继续，可恢复查询。

更新操作保留本地操作日志。若提交响应未确认，先在 KiCad 检查并处理上次操作，再使用“处理未确认操作”恢复更新入口。配置保存/应用、模型测试和符号库发布保留各自确认与结果状态。

## 文件用途

以下模块位于 `plugins/ai_schematic/`：

| 文件 | 用途 |
| --- | --- |
| `ui.py` / `ui_workspace.py` / `ui_theme.py` | 窗口控制、布局、操作引导与主题 |
| `requirements_ui.py` | 设计指标表、筛选、原文详情与复制 |
| `workflow.py` | 生成、反馈、结果应用、任务恢复和会话状态 |
| `ai_service.py` | HTTP 通信、鉴权、请求校验和结果下载 |
| `session_store.py` / `session.py` | 请求保存、缓存、原子写入、锁与会话兼容 |
| `api_client.py` | 现有 KiCad IPC 接口调用及清空回退 |
| `generator.py` | 图页清空、Graph IR 绘图、事务和操作日志 |
| `graph_ir.py` / `symbol_matching.py` | 图结构、型号绑定、引脚与单元匹配 |
| `layout.py` / `routing.py` | 符号布局与导线路由 |
| `snapshot.py` | 几何与连通性工具、旧版回读兼容 |
| `management_ui.py` / `management_controller.py` | 服务配置、模型及符号库管理 |

其他目录：`plugins/generate_schematic.py` 为启动入口；`plugins/generated_proto/` 为通信消息；`plugins/protocol/` 为已有协议源文件；`plugins/tests/` 为离线回归；`tools/package_plugin.py` 生成安装 ZIP，并排除测试和缓存。

## 开发验证

在已安装依赖的插件 Python 环境运行：

```powershell
$env:PYTHONPATH = "$PWD/plugins;$PWD/plugins/generated_proto"
python -B -m unittest discover -s plugins/tests -v
```

离线测试使用 HTTP/IPC 替身和 Qt offscreen。发布验收另检查实际符号库、创建结果、整页替换、失败回滚及一次撤销恢复。结构与连接校验范围、RF 数值性能评估须分别确认。
