# Antigravity 工作区常驻规则 (News Fetch & Summarizer)

本规则适用于 `news_fetch` 项目的所有 Antigravity 会话。

---

## 1. 定时总结调度规则 (Automated Schedule Rule)

- 当用户输入 **`开启定时总结`**、**`start schedule`**、**`开启后台总结任务`** 或使用 `/schedule` 相关的意图时，Agent **必须立即调用 `schedule` 工具** 注册一个后台 Cron 定时任务：
  - **推荐 Cron 表达式**：`*/30 * * * *`（每 30 分钟执行一次，用户若指定周期则按用户要求的周期）
  - **Prompt 内容**：
    > "先调用 `python llm/agent_helper.py status` 查看状态，然后依次对有待处理文章且非 `FETCHING` 的来源执行 `python llm/agent_helper.py pending --business <source> --limit 9999` 一次性获取该媒体当前全部待处理文章。在总结时，**必须先读取 `llm/prompt/prompt.md` 中的提示词模板**，严格按照其中要求的**标准化中文格式与 9 模块标题（带对应 Emoji）**进行提炼，然后通过标准输入管道调用 `python llm/agent_helper.py save --file -` 批量落盘，直到所有来源的待总结数彻底清零。"
  - **`IsDaemon` 属性**：设置为 `false`（作为当前工作流的后台调度）。

---

## 2. 爬虫并发与 Lock 状态文件避让机制

- 项目根目录下维护 `lock/` 文件夹（如 `lock/bloomberg.lock`、`lock/wsj.lock` 等）。
- **状态符号定义**：
  - `FETCHING`：爬虫正在运行写入 Parquet。Agent **必须跳过该新闻源**，等待下个定时周期。
  - `""`（空内容）：空闲状态，爬虫未在运行，数据安全，可正常总结。
- **自动管理**：爬虫启动时写入 `FETCHING`，爬取结束自动清空文件。

---

## 3. 金融研报提炼核心红线 (100% 遵守)

1. **【严禁调用外部 API】** 严禁调用 `llm/provider.py` 或任何外部大模型 API！所有新闻摘要必须由 Antigravity 会话自身大脑原生算力直接推理生成。
2. **【严禁直接操作 Parquet / Markdown】** 所有数据读取必须通过 `python llm/agent_helper.py pending`，所有总结落盘和状态回写必须通过 `llm/agent_helper.py` 的 `save_batch`。
3. **【严禁在聊天窗口刷屏大段正文】** 聊天窗口中仅输出简明统计报告（处理篇数、来源、归档路径、剩余待处理）。

---

## 4. 并发调度与无状态 Token 防膨胀铁律 (Stateless Concurrency & Anti-Bloat Rule)

1. **【并发调度铁律：单媒体专属 Subagent (1 业务源 ↔ 1 Subagent)】**
   - **严禁同源多智能体并发**：严禁对同一个新闻业务源同时派出多个 Subagent，以杜绝并发读取竞态导致的重复总结！
   - **跨媒体物理隔离并行**：每个有待处理文章的新闻源（如 `bloomberg`、`wsj`、`ft` 等）必须且仅分配 **1 个专属 Subagent** 全权负责，各媒体子智能体之间独立并行，互不干扰。
2. **【单 Subagent 严格单批 5 篇与无状态即用即销 (Pure One-Shot 5-Article Subagents)】**
   - **单 Subagent 严格配额 5 篇**：每个 Subagent 生命周期仅专职负责 **单批 5 篇文章**（或尾批不足 5 篇的收尾），执行一次性读取 -> 内存中一起总结 5 篇 -> 一次性批量保存落盘。
   - **绝无多轮循环（即用即销）**：Subagent 完成本批 5 篇的落盘后，立即向主智能体汇报简报并结束生命周期，严禁在同一个子会话中长周期多轮运行！
   - **主智能体销毁与轮转**：
     - 主智能体收到汇报后，**必须立即调用 `manage_subagents kill` 彻底销毁该 Subagent 会话**，释放其全部上下文。
     - 若该业务源仍有剩余待处理文章（`pending > 0`），主智能体立即派发**全新的纯净 Subagent** 处理下一批 5 篇，直到该业务源待处理数归零。
3. **【命令白名单与绝对禁令 (Immutable Command Whitelist)】**
   - 任何 Subagent 只能使用以下两个固定命令，**绝对严禁任何命令变异**：
     - **读取命令（唯一）**：
       ```powershell
       python llm/agent_helper.py pending --business <source> --limit 5
       ```
     - **保存命令（唯一）**：
       ```powershell
       $OutputEncoding = [System.Text.Encoding]::UTF8
       @'
       [
         {
           "business": "<source>",
           "url": "<url_1>",
           "timestamp": "<timestamp_1>",
           "summary": "<第1篇9模块中文Markdown摘要>"
         },
         {
           "business": "<source>",
           "url": "<url_2>",
           "timestamp": "<timestamp_2>",
           "summary": "<第2篇9模块中文Markdown摘要>"
         }
       ]
       '@ | python llm/agent_helper.py save --file -
       ```
   - **绝对禁止反模式**：
     - ❌ 严禁使用 `powershell -NoProfile -Command "..."`
     - ❌ 严禁使用 `ConvertFrom-Json`、`$items[13]`、`ForEach-Object` 等在命令行中对文章切片
     - ❌ 严禁使用 `python -c` 或 `@'...'@ | python -` 进行 Python 脚本中转（会导致中文损坏为 `??` 问号乱码）
     - ❌ 严禁在本地创建任何临时文件（如 `*.json`、`*.txt`、`temp_*` 等）
4. **【派发 Subagent 的标准统一 Prompt 模板】**
   主 Agent 每次调用 `invoke_subagent` 派发任务时，**必须直接使用以下标准化 Prompt**（替换其中的 `<source>`）：
   ```text
   你是专职负责新闻源【<source>】的金融研报提炼 Subagent。
   你的任务：在当前单批次会话中，读取并一次性总结当前批次的 5 篇待处理文章（不足 5 篇则处理实际剩余篇数）。

   【唯一命令白名单】你只能使用以下两条命令，严禁使用 powershell -Command、严禁 ConvertFrom-Json、严禁切片：
   1. 批量读取本批 5 篇：
      python llm/agent_helper.py pending --business <source> --limit 5
   2. 批量保存本批摘要：
      $OutputEncoding = [System.Text.Encoding]::UTF8
      @'
      [
        {
          "business": "<source>",
          "url": "<url>",
          "timestamp": "<timestamp>",
          "summary": "<严格按 9 模块标准生成的中文Markdown摘要>"
        }
      ]
      '@ | python llm/agent_helper.py save --file -

   【单批 SOP 流程】：
   1. 执行读取命令获取当前批次（最多 5 篇）。若返回为 []，说明当前已无待处理文章，直接向主智能体汇报，结束会话。
   2. 对该批所有文章，分别根据 llm/prompt/prompt.md 标准提炼 9 模块中文研报摘要。
   3. 将所有摘要组织为 JSON 数组填充进保存命令，通过标准输入管道一次性保存。保存后底层会自动将这些文章标记为完成。
   4. 结束后仅向主智能体输出一句话简报：“【<source>】完成：共处理 X 篇，已落盘更新”，严禁输出任何大段正文！任务结束。
   ```
5. **【严禁中途停顿或交还控制权】** 当用户下达总结、批量处理、自动跑完或定时任务被触发时，只要 `status` 检查中存在非 `FETCHING` 的待处理文章（`pending > 0`）：
   - Agent 及 Subagent **严禁在处理完部分篇数后停止调用工具**！
   - Agent **严禁向用户询问“是否继续”或提前输出阶段性文字回复交还回合**！
   - 直到所有新闻源的 pending 全部彻底归零（`total_pending == 0`）后，才允许向用户输出最终的完成统计报告。

---

## 5. 临时文件控制与内存流化规范 (File Cleanup & Memory Piping Rule)

1. **【所有 Agent / Subagent 全体禁止落盘】** 主 Agent 及所有派生的 Subagent（子智能体）**绝对严禁**在项目根目录或任何子目录下创建、写入任何形式的临时数据文件（如 `*.json`、`*.txt`、`temp_*`、`summary_*`、`article_*` 等）或临时执行脚本（如 `*.py`、`*.sh`、`check.py` 等）。
2. **【严禁使用 Python 脚本进行中转执行】** 
   - **严禁 `python -c "..."`**：绝对严禁在命令行中通过 `python -c "..."` 构造包含多行文本、字典或 Markdown 的内联 Python 脚本（会触发命令行引号剥离、语法错误）。
   - **严禁 `@'...'@ | python -`**：绝对严禁将 Python 脚本通过 Here-String 管道喂给 `python -` 进行中转解析与保存！这会导致 Windows 管道编码错乱，使中文摘要全部损毁为问号乱码。
3. **【100% 内存管道机制 (Memory Piping Only)】**
   - **数据读取**：调用 `pending` 时，直接从进程标准输出（stdout）中读取解析 JSON，**严禁使用 `>` 等命令重定向生成本地文件**。
   - **数据保存**：调用 `save` 时，必须使用 `--file -` 参数，通过标准输入（stdin）管道传输 JSON 总结，在内存中完成，**严禁生成临时 JSON 文件中转**。
   - **PowerShell 管道标准范式（唯一标准）**：
     ```powershell
     $OutputEncoding = [System.Text.Encoding]::UTF8

     @'
     [
       {
         "business": "xxx",
         "url": "https://example.com",
         "timestamp": "2026-08-29T00:00:00Z",
         "summary": "这里是中文 Markdown 总结内容"
       }
     ]
     '@ | python llm/agent_helper.py save --file -
     ```
     > **核心铁律**：
     > 1. 必须前置声明 `$OutputEncoding = [System.Text.Encoding]::UTF8`，防止 Windows 默认编码（GBK 等）导致中文乱码。
     > 2. 必须使用单引号 Here-String（`@' ... '@`），PowerShell 对其内容做 100% 原样字面量传输，彻底避免变量插值与引号转义冲突。
     > 3. **【严格双引号 JSON】** 管道内部的 JSON 数据必须严格符合标准 JSON 规范，**所有 Key 与 Value 必须使用标准英文双引号 `"`**，严禁使用 Python 字典风格的单引号 `'`。
4. **【不允许生成调试文件】** 严禁任何形式的临时调试、验证或数据拆分脚本落盘。
5. **【违规立即物理清理】** 若任何执行环节意外生成了临时文件，必须在当步内立即执行物理删除清理。
