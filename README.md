# TestCraft — AI-Powered Test Case Generator

A web tool that generates comprehensive test cases from requirements documents and source code using AI.

## Features

- **Document Import**: Upload requirement docs (Word `.docx`, PDF, Markdown `.md`, plain text)
- **Code Import**: Upload project source code as ZIP or specify a local directory path — supports any language (Python, Swift, Java, JS, Go, Kotlin, Rust, C/C++, etc.)
- **AI Analysis**: Send parsed requirements + code structure to an LLM to generate comprehensive test cases
- **Test Case Output**: Download generated test cases as Excel (`.xlsx`) or Markdown
- **Dark Mode**: Toggle between light and dark themes
- **Configurable LLM**: Supports OpenAI, Anthropic, and any OpenAI-compatible API

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the application

```bash
python app.py
```

The app will start at **http://localhost:5000**.

### 3. Configure your LLM

Navigate to **Configure** in the nav bar and enter:
- **Provider**: OpenAI / Anthropic / Custom
- **Base URL**: API endpoint (auto-filled per provider)
- **API Key**: Your API key
- **Model**: Model name (e.g., `gpt-4o`, `claude-sonnet-4-20250514`)

### 4. Generate test cases

1. Upload a requirements document (`.docx`, `.pdf`, `.md`, or `.txt`)
2. Optionally upload project source code (`.zip`) or enter a local directory path
3. Click **Generate Test Cases**
4. View, filter, and search results
5. Download as Excel or Markdown

## Project Structure

```
TestCraft/
├── app.py                  # Flask main app
├── requirements.txt
├── config.py               # LLM provider config
├── templates/
│   ├── base.html           # Base template with nav
│   ├── index.html          # Landing/upload page
│   ├── configure.html      # LLM API config page
│   └── results.html        # Test case results display + download
├── static/
│   ├── css/style.css
│   └── js/main.js
├── services/
│   ├── doc_parser.py       # Parse Word/PDF/Markdown
│   ├── code_analyzer.py    # Scan and summarize project code structure
│   ├── ai_generator.py     # LLM integration for test case generation
│   └── export.py           # Export to Excel/Markdown
├── uploads/                # Temp upload storage (auto-cleaned)
└── outputs/                # Generated test case files (auto-cleaned)
```

## Test Case Output Format

Each generated test case includes:
| Field | Description |
|---|---|
| ID | Unique identifier (TC-001, TC-002, ...) |
| Module | Feature or module name |
| Test Case Name | Descriptive test name |
| Priority | P0 (Critical) through P3 (Low) |
| Preconditions | Setup required before test |
| Steps | Step-by-step test procedure |
| Expected Result | What should happen |
| Type | Functional / UI / Edge Case / Performance / Security / Integration |

## Configuration

Set `FLASK_SECRET_KEY` environment variable for production:

```bash
export FLASK_SECRET_KEY="your-secure-secret-key"
```

File upload limit is 50 MB. Temporary files are cleaned up after 1 hour.
