"""Code analyzer — scans project source code and extracts structure summaries."""

import logging
import os
import re
import zipfile
import tempfile
import shutil
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Extensions we consider source code
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".kts",
    ".swift", ".go", ".rb", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".php", ".scala", ".m", ".mm", ".dart", ".lua",
    ".sh", ".bash", ".zsh", ".r", ".R",
}

# Directories to always skip
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", "build", "dist",
    ".idea", ".vscode", "vendor", "Pods", ".gradle",
    "target", "bin", "obj", ".next", ".nuxt",
}

MAX_FILE_SIZE = 512 * 1024  # 512 KB per file


@dataclass
class CodeStructure:
    file_tree: list[str] = field(default_factory=list)
    signatures: list[dict] = field(default_factory=list)
    summary: str = ""

    def to_text(self) -> str:
        parts = ["## Project File Tree", "```"]
        parts.extend(self.file_tree[:500])
        if len(self.file_tree) > 500:
            parts.append(f"... and {len(self.file_tree) - 500} more files")
        parts.append("```\n")

        if self.signatures:
            parts.append("## Code Signatures\n")
            for sig in self.signatures[:300]:
                parts.append(f"### {sig['file']}")
                for item in sig.get("items", []):
                    parts.append(f"- {item}")
                parts.append("")

        return "\n".join(parts)


def analyze_code(source_path: str) -> CodeStructure:
    """Analyze code from a zip file or directory path.

    Returns a CodeStructure with file tree and extracted signatures.
    """
    if source_path.endswith(".zip"):
        return _analyze_zip(source_path)
    elif os.path.isdir(source_path):
        return _analyze_directory(source_path)
    else:
        raise ValueError(f"Invalid code source: {source_path}")


def _analyze_zip(zip_path: str) -> CodeStructure:
    tmp_dir = tempfile.mkdtemp(prefix="testcraft_code_")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)
        # Find the actual root (zips often have a single top-level dir)
        entries = os.listdir(tmp_dir)
        root = tmp_dir
        if len(entries) == 1:
            candidate = os.path.join(tmp_dir, entries[0])
            if os.path.isdir(candidate):
                root = candidate
        return _analyze_directory(root)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _analyze_directory(root: str) -> CodeStructure:
    structure = CodeStructure()
    root = os.path.abspath(root)

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped dirs
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        rel_dir = os.path.relpath(dirpath, root)
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            structure.file_tree.append(rel_path)

            if ext not in SOURCE_EXTENSIONS:
                continue

            full_path = os.path.join(dirpath, fname)
            if os.path.getsize(full_path) > MAX_FILE_SIZE:
                continue

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except (IOError, OSError):
                continue

            items = _extract_signatures(content, ext)
            if items:
                structure.signatures.append({"file": rel_path, "items": items})

    logger.info(
        "Analyzed %d files, extracted signatures from %d",
        len(structure.file_tree),
        len(structure.signatures),
    )
    return structure


# ---------------------------------------------------------------------------
# Language-specific signature extraction (regex-based)
# ---------------------------------------------------------------------------

def _extract_signatures(content: str, ext: str) -> list[str]:
    extractors = {
        ".py": _extract_python,
        ".js": _extract_js_ts,
        ".ts": _extract_js_ts,
        ".tsx": _extract_js_ts,
        ".jsx": _extract_js_ts,
        ".java": _extract_java_kt,
        ".kt": _extract_java_kt,
        ".kts": _extract_java_kt,
        ".swift": _extract_swift,
        ".go": _extract_go,
        ".rb": _extract_ruby,
        ".rs": _extract_rust,
        ".c": _extract_c_cpp,
        ".cpp": _extract_c_cpp,
        ".h": _extract_c_cpp,
        ".hpp": _extract_c_cpp,
        ".cs": _extract_csharp,
        ".php": _extract_php,
    }
    extractor = extractors.get(ext, _extract_generic)
    return extractor(content)


def _extract_python(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"^class\s+(\w+).*?:", content, re.MULTILINE):
        items.append(f"class {m.group(1)}")
    for m in re.finditer(r"^(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", content, re.MULTILINE):
        name = m.group(1)
        if name.startswith("_") and not name.startswith("__"):
            continue
        params = m.group(2).strip()
        items.append(f"def {name}({params})")
    # Flask/FastAPI routes
    for m in re.finditer(r'@\w+\.(?:route|get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)', content):
        items.append(f"route: {m.group(1)}")
    return items


def _extract_js_ts(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\bclass\s+(\w+)", content):
        items.append(f"class {m.group(1)}")
    for m in re.finditer(r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)", content):
        items.append(f"function {m.group(1)}({m.group(2).strip()})")
    for m in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>", content):
        items.append(f"const {m.group(1)} = () =>")
    # Express routes
    for m in re.finditer(r"\.\s*(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)", content):
        items.append(f"route {m.group(1).upper()}: {m.group(2)}")
    return items


def _extract_java_kt(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\b(?:public|private|protected|internal)?\s*(?:abstract\s+)?(?:class|interface|enum|object)\s+(\w+)", content):
        items.append(f"class {m.group(1)}")
    for m in re.finditer(
        r"(?:public|private|protected|internal)?\s*(?:static\s+)?(?:fun\s+|[\w<>\[\]]+\s+)(\w+)\s*\(([^)]*)\)",
        content,
    ):
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "catch", "class", "new", "return"):
            continue
        items.append(f"method {name}({m.group(2).strip()[:80]})")
    return items


def _extract_swift(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\b(?:class|struct|enum|protocol|actor)\s+(\w+)", content):
        items.append(f"type {m.group(1)}")
    for m in re.finditer(r"\bfunc\s+(\w+)\s*\(([^)]*)\)", content):
        items.append(f"func {m.group(1)}({m.group(2).strip()[:80]})")
    return items


def _extract_go(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"^type\s+(\w+)\s+struct", content, re.MULTILINE):
        items.append(f"type {m.group(1)} struct")
    for m in re.finditer(r"^type\s+(\w+)\s+interface", content, re.MULTILINE):
        items.append(f"type {m.group(1)} interface")
    for m in re.finditer(r"^func\s+(?:\(\w+\s+\*?(\w+)\)\s+)?(\w+)\s*\(([^)]*)\)", content, re.MULTILINE):
        receiver = m.group(1)
        name = m.group(2)
        params = m.group(3).strip()[:80]
        if receiver:
            items.append(f"func ({receiver}).{name}({params})")
        else:
            items.append(f"func {name}({params})")
    return items


def _extract_ruby(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\bclass\s+(\w+)", content):
        items.append(f"class {m.group(1)}")
    for m in re.finditer(r"\bmodule\s+(\w+)", content):
        items.append(f"module {m.group(1)}")
    for m in re.finditer(r"\bdef\s+(\w+[?!=]?)", content):
        items.append(f"def {m.group(1)}")
    return items


def _extract_rust(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\bstruct\s+(\w+)", content):
        items.append(f"struct {m.group(1)}")
    for m in re.finditer(r"\btrait\s+(\w+)", content):
        items.append(f"trait {m.group(1)}")
    for m in re.finditer(r"\bpub\s+(?:async\s+)?fn\s+(\w+)", content):
        items.append(f"fn {m.group(1)}")
    for m in re.finditer(r"\bimpl\s+(\w+)", content):
        items.append(f"impl {m.group(1)}")
    return items


def _extract_c_cpp(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\bclass\s+(\w+)", content):
        items.append(f"class {m.group(1)}")
    for m in re.finditer(r"\bstruct\s+(\w+)\s*\{", content):
        items.append(f"struct {m.group(1)}")
    for m in re.finditer(r"^[\w:*&<>\s]+?\b(\w+)\s*\([^;]*\)\s*\{", content, re.MULTILINE):
        name = m.group(1)
        if name not in ("if", "for", "while", "switch", "catch", "return"):
            items.append(f"function {name}()")
    return items


def _extract_csharp(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\b(?:class|interface|struct|enum)\s+(\w+)", content):
        items.append(f"type {m.group(1)}")
    for m in re.finditer(
        r"(?:public|private|protected|internal)\s+(?:static\s+)?(?:async\s+)?[\w<>\[\]]+\s+(\w+)\s*\(",
        content,
    ):
        name = m.group(1)
        if name not in ("if", "for", "while", "switch", "catch", "class", "new"):
            items.append(f"method {name}()")
    return items


def _extract_php(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\bclass\s+(\w+)", content):
        items.append(f"class {m.group(1)}")
    for m in re.finditer(r"\bfunction\s+(\w+)\s*\(([^)]*)\)", content):
        items.append(f"function {m.group(1)}({m.group(2).strip()[:80]})")
    return items


def _extract_generic(content: str) -> list[str]:
    items = []
    for m in re.finditer(r"\b(?:class|struct|interface|enum|type)\s+(\w+)", content):
        items.append(f"type {m.group(1)}")
    for m in re.finditer(r"\b(?:def|func|function|fn)\s+(\w+)", content):
        items.append(f"function {m.group(1)}")
    return items
