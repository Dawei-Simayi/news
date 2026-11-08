# 财经新闻自动抓取与 AI 分析系统

自动从 **华尔街日报 (WSJ)**、**彭博社 (Bloomberg)**、**经济学人 (The Economist)** 抓取最新财经新闻，调用大语言模型进行结构化分析，生成专业级中文投资简报。

---

## 环境要求

- Python 3.10+
- Chrome 浏览器（用于 Playwright 远程调试）

---

## 快速开始

### 1. 创建虚拟环境（推荐）

```bash
# conda
conda create -n news_fetch python=3.12 -y
conda activate news_fetch

# 或使用 venv
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/Mac: source venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt

# 安装 Playwright 浏览器（首次需要）
playwright install chromium
```

### 3. 配置环境变量

复制 `.env.example` 为 `.env`，填入对应 API Key：

```env
DEEPSEEK_API_KEY=your_key
OPEN_ROUTER_API_KEY=your_key
LLAMA_CPP=your_key
OMNIXAI=your_key
```
python 环境变量设置：setx PYTHONIOENCODING utf-8
### 4. 启动 Chrome 远程调试

```bash
# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222

# Mac
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
alias chrome-debug='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="$HOME/Library/Application Support/Google/Chrome/Test Data"'
alias bloomberg='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9223 --user-data-dir="$HOME/Library/Application Support/Google/Chrome/9223"'
alias ft='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9224 --user-data-dir="$HOME/Library/Application Support/Google/Chrome/9224"'


# Linux
google-chrome --remote-debugging-port=9222
```

### 5. 运行（常驻后台 API 服务）

```bash
python run_fetch.py
# 或
uvicorn run_fetch:app --host 0.0.0.0 --port 8000
```

启动后为常驻后台服务，Swagger 文档见 `http://127.0.0.1:8000/docs`。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/fetch/all` | 并发抓取全部来源（按 CDP 端口分组：不同端口并行，同端口内串行） |
| `POST` | `/fetch/{source}` | 按需抓取单个来源（`bloomberg` / `wsj` / `wsj-print` / `economist` / `economist-weekly` / `barrons` / `ft`） |
| `POST` | `/fetch/retry` | 重试 `llmDone=False` 的文章 |

```bash
curl -X POST http://127.0.0.1:8000/fetch/ft
```

---

## 项目结构

```
news_fetch/
├── run_fetch.py            # 主入口：编排所有抓取和分析流程
├── logger.py               # 全局日志配置
├── requirements.txt        # Python 依赖清单
├── .env                    # API Key 配置（需自行创建）
│
├── common/                 # 通用工具模块
│   └── tools.py            # 页面抓取、正文提取、Next.js JSON 解析
│
├── Wall_Street/            # 华尔街日报
│   └── tools.py
│
├── bloomberg/              # 彭博社
│   └── tools.py
│
├── economist/              # 经济学人
│   └── tools.py
│
├── llm/                    # AI 分析模块
│   ├── provider.py         # LLM Provider 配置（DeepSeek / OpenRouter / 本地 / OmniAI）
│   └── summarizer.py       # LLM 调用 + 摘要保存
│
└── prompt/
    └── prompt.md           # AI 系统提示词（中文专业分析框架）
```

---

## 数据存储

| 数据 | 格式 | 位置 |
|------|------|------|
| 原始文章 | Parquet | `.data/{business}.parquet` |
| AI 摘要 | Markdown | `llm/summary/{source}/{date}.md` |

所有 Parquet 统一放在项目根目录的 `.data/` 下，文件名为业务键（与 `llm/batch.py` 的
`BUSINESS` 一致）：`bloomberg` / `wsj` / `wsj_print` / `economist` / `economist_weekly` /
`barrons_weekly` / `ft` / `weeklyonstock`。路径由 `common/paths.py` 的 `data_file()`
生成绝对路径，不再依赖启动时的工作目录。

---

## LLM Provider 配置

当前默认使用 `OMNI_AI`（国内代理渠道），可在 `run_fetch.py` 中切换：

| Provider | 模型 | 备注 |
|----------|------|------|
| DEEP_SEEK | deepseek-v4-flash | 官方 API |
| OPEN_ROUTER | google/gemma-4-31b-it:free | 需代理 |
| LOCAL | Qwen 量化模型 | 本地部署 |
| OMNI_AI | deepseek-v4-flash | 国内代理 |

---

## 许可证

MIT
