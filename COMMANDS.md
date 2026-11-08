# News Fetch & Summarizer 常用命令速查手册 (Commands Reference)

本文档整理了本项目日常运行、监控状态、数据读取与总结落盘的核心命令，方便在本地终端（PowerShell / CMD / Bash）及 Agent 会话中直接复制执行。

---

## 1. 状态与待处理检查 (Status & Pending)

### 1.1 查看所有新闻源的待总结数及锁状态
```powershell
python llm/agent_helper.py status
```
* **输出示例**：
  ```json
  {
    "pending_counts": {
      "bloomberg": 0,
      "wsj": 0,
      "wsj_print": 0,
      "economist": 0,
      "economist_weekly": 0,
      "barrons_weekly": 0,
      "ft": 0
    },
    "lock_statuses": {
      "bloomberg": "",
      "wsj": "",
      "wsj_print": "",
      "economist": "",
      "economist_weekly": "",
      "barrons_weekly": "",
      "ft": ""
    },
    "total_pending": 0
  }
  ```

---

## 2. 提取待总结新闻 (Pending Articles Extraction)

### 2.1 提取指定媒体的待总结文章（JSON 格式输出至 stdout）
```powershell
# 提取彭博社前 10 篇
python llm/agent_helper.py pending --business bloomberg --limit 10

# 提取金融时报前 10 篇
python llm/agent_helper.py pending --business ft --limit 10

# 提取华尔街日报前 10 篇
python llm/agent_helper.py pending --business wsj --limit 10

# 提取巴伦周刊前 10 篇
python llm/agent_helper.py pending --business barrons_weekly --limit 10

# 提取经济学人前 10 篇
python llm/agent_helper.py pending --business economist --limit 10
```

### 2.2 在终端/脚本中以 UTF-8 内存流方式读取并打印新闻正文
```powershell
# 流式读取示例
python -c "import subprocess, json, sys; sys.stdout.reconfigure(encoding='utf-8'); res = subprocess.check_output([sys.executable, 'llm/agent_helper.py', 'pending', '--business', 'bloomberg', '--limit', '10']); [print(f'[{i}] {x[\"url\"]} | {x[\"timestamp\"]}\n{x[\"content\"]}\n') for i, x in enumerate(json.loads(res))]"
```

---

## 3. 总结落盘与状态回写 (Save Summaries via Stdin Piping)

> **注意**：按照工作区常驻规则，落盘必须使用 `--file -` 标准输入内存管道传输，严禁在磁盘上生成中间 JSON 临时文件。同时在 Windows PowerShell 中需预设 UTF-8 编码。

### 3.1 批量保存模板 (PowerShell 管道模式)
```powershell
$OutputEncoding = [System.Text.Encoding]::UTF8
@'
[
  {
    "business": "bloomberg",
    "url": "https://www.bloomberg.com/news/articles/...",
    "timestamp": "2026-08-24T...",
    "summary": "> 📰 **bloomberg** | [timestamp](url)\n\n1. 🎯 **核心主题**：...\n2. 📊 **全部数据**：...\n3. 📜 **政策列表**：...\n4. 🔍 **政策意图**：...\n5. ⚙️ **主要逻辑**：...\n6. 💰 **主要机会**：...\n7. ⚠️ **主要风险**：...\n8. 💬 **主观看法**：...\n9. 🔮 **结论与展望**：..."
  }
]
'@ | python llm/agent_helper.py save --file -
```


---

## 4. 提示词规范文件 (Prompt Template)

- 标准 9 模块买方分析师提炼模版路径：
  - `llm/prompt/prompt.md`
- 模块标准：
  1. 🎯 **核心主题**
  2. 📊 **全部数据**
  3. 📜 **政策列表**
  4. 🔍 **政策意图**
  5. ⚙️ **主要逻辑**
  6. 💰 **主要机会**
  7. ⚠️ **主要风险**
  8. 💬 **主观看法**
  9. 🔮 **结论与展望**
