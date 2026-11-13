# Token Meter · 本地 Token 看板

在一张本地看板中查看多个 AI Agent 的 Token 用量、缓存命中率、每日趋势、模型明细与响应表现，并核对 Codex 官方账户用量与本机记录的差额。

只读本机留存日志，页面运行在 `127.0.0.1`。缺失用量保留未知，不通过估算填平差额。

## 下载与启动

适用 Windows 10/11 64 位。便携包内置 Python，解压后即可启动。

1. [下载最新 Windows 便携包](https://github.com/liu123268/token-meter/releases/latest/download/TokenMeter-Windows-x64.zip)，完整解压到有写权限的文件夹。
2. 双击 `启动看板.cmd`，浏览器会打开本机看板 `http://127.0.0.1:18741/`。
3. 看板会自动查找当前 Windows 用户已安装软件的本机用量记录。首次扫描完成后，在概览里选择软件、模型和日期。
4. 结束使用时双击 `停止看板.cmd`。关闭网页后后台仍会采集。

## 配置

- **默认设置**：解压即用。看板将索引和缓存保存在便携包根目录的 `data/`，默认端口是 `18741`。无需申请 API Key。
- **Codex 官方对账**：用自己的账号登录本机官方 Codex，再打开看板里的「Codex 对账」。本机用量和官方账户用量分别显示。
- **自定义端口**：在解压后的 `TokenMeter` 文件夹打开 PowerShell，运行下方命令。`-DataDirectory` 指定数据目录，`-Open` 打开浏览器。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\app\launch_dashboard.ps1 -Port 18742 -DataDirectory .\data -Open
```

Codex 日志放在自定义位置时，可以直接用内置 Python 启动。在 PowerShell 里把 `你的日志目录` 换成自己的路径，该目录应包含 `sessions` 或 `archived_sessions`。这个命令在当前窗口运行，按 Ctrl+C 停止。

```powershell
.\app\runtime\python.exe -B .\app\server.py --codex-home "你的日志目录" --database .\data\token-meter.sqlite3 --open
```

其他软件按下方「支持的数据来源」所列位置自动发现。找不到用量时，先确认该软件已在这台电脑产生本机日志，再在看板的「数据来源」里检查状态。下载其他版本、源码包或校验清单可进入 [Releases](https://github.com/liu123268/token-meter/releases/latest)。

## 整轮输出速率

Codex、WorkBuddy、ZCode、MiMo Desktop 和 DeepSeek Harness 均展示“整轮输出速率”：**已核对主任务的输出 Token 合计 ÷ 对应整轮执行时间合计**，单位 tok/s。时间包含模型处理、工具执行和等待；输入与缓存 Token 不进入分子。输出沿用原始 usage 中的定义，包含其中已计入的推理或工具调用输出。不对不同任务的完成时间取平均来比较效率，也不将每条 tok/s 简单平均。

仅纳入起止完整、已完成且用量可核对的主任务；排除独立子 Agent、后台标题、失败和取消任务。各软件使用原生边界：Codex 任务事件和可核对的逐请求输出、WorkBuddy 主 CLI Agent 与其直属生成请求、ZCode 的 `turn_usage` 与逐请求用量、MiMo 主 Actor 的用户消息至最终回复和关联 step-finish，以及 DSH 主会话的 `turn/start` / `turn/end` 与本轮原始 usage。输出缺失、计数冲突、无法核对的任务保留为未核对样本，不估算速率。

界面展示各软件的整轮输出速率，模型明细使用同一口径。遵循软件、模型和北京时间日期筛选；速率使用历史参考时以“历史”标记注明日期。混合模型任务不向单一模型分摊耗时。历史任务、模型分词、推理和工具环境仍不同，该指标反映实际工作流中的输出吞吐量，无法衡量任务质量或单独证明模型性能倍数。


## Codex 用量对账：逐日查看差额和来源

把官方账户接口的每日用量与本机日志计数放在同一页核对，保留各自原值、读取时间与来源，显示每天的未解释差额。可以切换 UTC / 北京时间的本机日界线，展开 OpenAI 未分类请求，并导出逐日对账 CSV。

本机合计拆成“明确 GPT + 有原始用量的 OpenAI 未分类请求”，便于检查覆盖范围与去重。官方日数据延迟或未返回时明确提示；缺失部分保持未知，不靠猜测补齐。

当前差额提示显示在对账页面，尚未提供系统弹窗或阈值推送。

## 功能

- 按软件、模型、今天、近 7 天、近 30 天或自定义日期筛选。
- 显示总 Token、输入、输出、缓存命中率和未缓存输入。
- 每日趋势使用线性刻度，悬浮显示当天精确数字；提供软件与模型分布、明细及 CSV 导出。
- 按软件和模型查看整轮输出速率（tok/s）。
- 独立 Codex 对账页面：官方接口、明确 GPT、OpenAI 未分类原始请求、本机合计及未解释差额。

## 支持的数据来源

| 软件 | 默认位置 | 读取方式 |
|---|---|---|
| Codex | `CODEX_HOME` 或用户目录的 `.codex` | 会话与归档日志中的原生用量，以及旧格式累计增量 |
| WorkBuddy | 用户目录的 `.workbuddy` | JSON 快照、JSONL trace 事件与会话 transcript 中的原始 usage，按请求去重；有完整起止时间时补充请求速度 |
| ZCode | 用户目录的 `.zcode/cli/db/db.sqlite` | 本地数据库中的模型用量 |
| MiMo Desktop / MiMoCode | 用户目录的 `.local/share/mimocode/mimocode.db` | 共享引擎的 step-finish；桌面与 CLI 来源可能无法区分 |
| DeepSeek Harness | 用户目录的 `.dsh/sessions` | v3/v4 JSONL 及 Zstandard 压缩会话 |

支持已验证的留存格式。未安装软件、没有 usage、使用自定义目录或新版本改变格式时，页面可能显示未发现、未知或不完整。无法恢复已删除的原始日志。

DeepSeek Harness 的用量合计包含主会话与有明确标记的子 Agent 调用，页面分别显示两者用量。客户端某条主会话下的用量可能只覆盖主会话，比较时需匹配范围。子 Agent 依据当前会话的 `subagent/descriptor` 识别；会话的父记录关联本身不作为子 Agent 判据，继承的历史消息仍排除。

## 统计原则与边界

**总 Token = 输入 + 输出。** 缓存读取已经包含在输入，推理已经包含在输出，不重复加总。缓存字段缺失时保留未知。

Codex 逐请求以 `response_id` 去重；旧格式计算累计增量，并抵扣已记原生请求，重复通知不再次累加。压缩 checkpoint 不重复补加。概览、每日趋势、软件与模型明细、CSV 使用同一份去重后的本机记录：纳入明确 GPT 请求及日志明确标记为 OpenAI 的未分类原始请求，排除已知第三方提供方；缺来源标记的旧记录仍按明确型号筛选。

例如 `codex-auto-review` 等原始别名保留原名，不推测实际 GPT 版本，但有可靠原始计数时纳入本机总量。对账另列组成项；冲突记录和与主索引重叠的请求不再次相加。同一软件、模型、北京时间日期下，今天卡片、趋势点、明细与导出合计一致。

**未解释差额 = 官方接口 − 本机可观察合计。** 官方数据只作对照，不加入本机总量。差额不能直接解释为漏算、费用或套餐额度；数值相等也不能证明所有账户与统计范围完全一致。

本机概览按北京时间分日，对账默认使用北京时间以便对照，也可切换 UTC。官方日期保留服务返回值；公开接口未说明每日时区、模型组成或统计截止时间。未返回某日时显示未提供，不当作零。

## Codex 官方账户读取

使用本机官方 Codex App Server 的 `account/usage/read`，需要该电脑安装支持接口的 Codex，并已有该用户的 ChatGPT/Codex 登录。API Key 专用登录可能无法提供账户日统计；本机日志统计仍能使用。

启动读取一次，成功后每小时更新，失败后 15 分钟重试；手动更新至少间隔 5 分钟。页面每 3 秒刷新读取本机索引和已保存缓存，不会每 3 秒请求官方。接口读取时间不等于服务统计截止时间，官方日数据和客户端展示都可能延迟。

[官方接口说明](https://learn.chatgpt.com/docs/app-server#7-token-usage-chatgpt) · [公开更新延迟报告](https://github.com/openai/codex/issues/25479)

## 隐私与数据清理

日志采集只读，不修改 AI 软件的历史、代理、配置或登录文件；代码不直接读取密钥或登录凭据。官方账户查询由官方 Codex 使用其现有登录完成，会与官方服务通信。本机统计不上传，网页不对局域网开放。

自己的 `data/` 存放统计库、扫描游标、官方缓存，以及最多 32KB 的最近一次异常说明。每 3 秒刷新不会保存一份完整快照，磁盘主要随新增用量记录上涨。停止后删除 `data/` 可重建；原始日志还在时，历史会重新索引。

分享原始发布 ZIP，不要压缩已经使用过的完整目录。Git 忽略规则和打包文件清单均排除个人数据；CSV 仅导出汇总与来源，不导出聊天正文或账户指纹。

## 源码运行与打包

源码需要 Python 3.14，自带 DeepSeek 压缩记录所需的 Zstandard 解压。Windows 启动脚本会查找 `runtime/`、PATH 与 Python 注册项；所有应用路径跟随脚本所在目录。

```powershell
python -B server.py --open
```

此命令在当前用户环境建立统计库并启动本机服务，`--open` 同时打开浏览器。`--codex-home` 可指定 Codex 日志目录，`--database` 可指定统计库，`--port` 可调整端口。

```powershell
python -B tools/build_release.py
```

打包工具从 Python 官网下载固定版本运行环境，核对 SHA256，并依据明确文件清单生成 Windows 便携包、源码包和校验清单。输出位于项目旁边的 `token-meter-distribution/`，不把整个运行目录压缩进去。

## 开机启动与目录迁移

按 Win+R 输入 `shell:startup`，把启动入口的快捷方式放进当前用户的启动文件夹，可在登录后启动并打开页面。仅后台启动时，快捷方式可调用 `app/launch_dashboard.ps1`，省略 `-Open`。

如已创建 `TokenMeter-LocalDashboard` 计划任务，脚本仅在任务操作确实指向本目录的 `server.py` 时沿用它。按 Win+R 输入 `taskschd.msc` 可查看：触发器决定何时启动，操作记录程序和路径，设置控制重试及重复实例。“结束”仅停止当前进程，“禁用”阻止后续触发；本目录的停止入口会暂停其任务，启动入口重新启用。

移动普通便携包前先停止；自己设置了快捷方式或计划任务时，要同步更新其路径。

## 许可证

本项目采用 [MIT 许可证](LICENSE)，允许使用、修改、分发及商用，需保留版权和许可声明。

便携包附带的 Python 及其第三方组件保留原有许可，见 `app/runtime/LICENSE.txt`。运行环境来源与校验值见 `app/RUNTIME.txt`。
