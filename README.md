# TestCraft — AI 驱动的测试用例生成工具

一款基于 Web 的工具，通过 AI 从需求文档和源代码中自动生成全面的测试用例。

## 功能特性

- **文档导入**：上传需求文档（Word `.docx`、PDF、Markdown `.md`、纯文本）
- **代码导入**：上传项目源码压缩包（ZIP）或指定本地目录路径，支持任意语言（Python、Swift、Java、JS、Go、Kotlin、Rust、C/C++ 等）
- **AI 分析**：将解析后的需求和代码结构发送给大语言模型，自动生成完整测试用例
- **测试用例导出**：将生成的测试用例下载为 Excel（`.xlsx`）或 Markdown 格式
- **深色模式**：支持亮色/深色主题切换
- **可配置的大模型**：支持 OpenAI、Anthropic 及任何 OpenAI 兼容的 API

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动应用

```bash
python app.py
```

应用将在 **http://localhost:5000** 启动。

### 3. 配置大模型

点击导航栏中的 **配置**，填写以下信息：
- **提供商**：OpenAI / Anthropic / 自定义
- **Base URL**：API 接口地址（按提供商自动填充）
- **API Key**：你的 API 密钥
- **模型**：模型名称（如 `gpt-4o`、`claude-sonnet-4-20250514`）

### 4. 生成测试用例

1. 上传需求文档（`.docx`、`.pdf`、`.md` 或 `.txt`）
2. 可选：上传项目源码（`.zip`）或填写本地目录路径
3. 点击 **生成测试用例**
4. 查看、筛选和搜索结果
5. 下载为 Excel 或 Markdown 格式

## 项目结构

```
TestCraft/
├── app.py                  # Flask 主应用
├── requirements.txt
├── config.py               # 大模型提供商配置
├── templates/
│   ├── base.html           # 基础模板（含导航栏）
│   ├── index.html          # 首页/上传页
│   ├── configure.html      # 大模型 API 配置页
│   └── results.html        # 测试用例结果展示与下载页
├── static/
│   ├── css/style.css
│   └── js/main.js
├── services/
│   ├── doc_parser.py       # 解析 Word/PDF/Markdown 文档
│   ├── code_analyzer.py    # 扫描并摘要项目代码结构
│   ├── ai_generator.py     # 大模型集成，生成测试用例
│   └── export.py           # 导出为 Excel/Markdown
├── uploads/                # 临时上传存储（自动清理）
└── outputs/                # 生成的测试用例文件（自动清理）
```

## 测试用例输出格式

每条生成的测试用例包含以下字段：

| 字段 | 说明 |
|---|---|
| ID | 唯一标识符（TC-001、TC-002……） |
| 模块 | 功能或模块名称 |
| 测试用例名称 | 描述性测试名称 |
| 优先级 | P0（关键）至 P3（低） |
| 前置条件 | 测试前需完成的准备工作 |
| 步骤 | 逐步测试操作流程 |
| 预期结果 | 应发生的结果 |
| 类型 | 功能 / UI / 边界用例 / 性能 / 安全 / 集成 |

## 配置说明

生产环境请设置 `FLASK_SECRET_KEY` 环境变量：

```bash
export FLASK_SECRET_KEY="your-secure-secret-key"
```

文件上传限制为 50 MB，临时文件将在 1 小时后自动清理。
