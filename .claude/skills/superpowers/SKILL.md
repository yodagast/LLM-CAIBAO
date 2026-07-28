---
name: superpowers
description: Advanced capabilities and power user techniques for maximizing Claude Code productivity. Use when tackling complex tasks, optimizing workflows, or pushing beyond basic usage.
---

# Superpowers - Advanced Claude Code Techniques

## Power User Workflows

### 1. Multi-Agent Orchestration

Launch specialized subagents for parallel processing:

```python
# Launch multiple agents for complex analysis
Agent(subagent_type="Browser", prompt="Research competitor APIs")
Agent(subagent_type="Browser", prompt="Find documentation examples")

# Results returned automatically
```

### 2. Context Compression Strategies

Maximize context window efficiency:

```python
# Use progressive disclosure
# SKILL.md → reference.md (only when needed)

# Use keywords for memory retrieval
search_memory(
    query="authentication",
    keywords="auth,login,token",  # 3 keywords max
    depth="shallow"  # Start shallow, go deep if needed
)

# Use glob patterns for targeted file search
search_file(query="**/test_*.py")  # Only test files
```

### 3. Intelligent Task Decomposition

Break complex tasks into atomic operations:

```python
todo_write(
    merge=False,
    todos=[
        # Group related changes
        {"id": "auth_setup", "content": "Setup auth module (auth.py, config.py)", "status": "PENDING"},
        {"id": "auth_tests", "content": "Add auth tests (test_auth.py)", "status": "PENDING"},
        
        # Verification immediately after implementation
        {"id": "verify_auth", "content": "Run auth tests", "status": "PENDING"},
        
        # Integration after verification
        {"id": "integrate", "content": "Integrate with main app", "status": "PENDING"},
    ]
)
```

### 4. Semantic Code Navigation

Use LSP for precise code intelligence:

```python
# Find all references to a symbol
lsp(operation="findReferences", filePath="main.py", line=42, character=15)

# Go to definition
lsp(operation="goToDefinition", filePath="main.py", line=42, character=15)

# Find implementations of interface
lsp(operation="goToImplementation", filePath="interface.py", line=10, character=5)

# Get all symbols in document
lsp(operation="documentSymbol", filePath="main.py")

# Search symbols across workspace
lsp(operation="workspaceSymbol", query="UserService")
```

## Advanced Search Techniques

### Semantic Search Patterns

```python
# High-level intent search (start here)
search_codebase(
    query="how does payment processing work",
    key_words="payment,processing,transaction"
)

# Follow up with specific searches
search_codebase(
    query="payment error handling and retry logic",
    key_words="payment,error,retry"
)

# Narrow to specific directories
search_codebase(
    query="payment validation",
    key_words="validation,payment",
    target_directories=["/project/src/payments"]
)
```

### Regex Search Mastery

```python
# Find all class definitions
grep_code(regex="class\\s+\\w+.*:", type="py")

# Find specific function calls
grep_code(regex="process_payment\\(", glob="*.py")

# Find TODO comments
grep_code(regex="# TODO:.*", type="py")

# Multi-line patterns
grep_code(
    regex="def\\s+\\w+.*\\{[\\s\\S]*?return",
    multiline=True,
    type="js"
)
```

## Memory Mastery

### Strategic Memory Organization

```python
# Create project-specific memories
update_memory(
    action="create",
    scope="workspace",  # Project-specific
    title="API Rate Limits",
    content="Max 100 requests/min, use exponential backoff",
    keywords="api,rate-limit,backoff",
    category="project_environment_configuration"
)

# Create global memories for cross-project knowledge
update_memory(
    action="create",
    scope="global",  # Cross-project
    title="Preferred Testing Framework",
    content="Use pytest with coverage plugin for all Python projects",
    keywords="testing,pytest,coverage",
    category="development_test_specification"
)
```

### Memory Retrieval Strategies

```python
# Start with overview, go deep when needed
search_memory(
    query="project setup",
    keywords="setup,config",
    category="project_introduction",
    depth="shallow"  # Fast overview
)

# Deep search for comprehensive information
search_memory(
    query="database architecture",
    keywords="database,schema",
    category="project_tech_stack",
    depth="deep"  # Full context
)
```

## Terminal Power Techniques

### Background Process Management

```python
# Start long-running process in background
run_in_terminal(
    command="npm run dev",
    is_background=True  # Don't block
)
# Returns terminal_id for monitoring

# Check output periodically
get_terminal_output(
    terminal_id="abc123",
    wait_seconds=5  # Wait for output
)
```

### Command Chaining

```python
# Sequential commands with proper ordering
run_in_terminal(command="git add .")
run_in_terminal(command="git commit -m 'Update'")
run_in_terminal(command="git push")
```

## Code Generation Excellence

### Efficient File Creation

```python
# Create with essential imports included
create_file(
    file_path="/project/src/new_module.py",
    file_content="""
#!/usr/bin/env python3
"""Module description."""

from pathlib import Path
from typing import Optional

def main() -> None:
    pass

if __name__ == "__main__":
    main()
"""
)
```

### Smart Replacement Patterns

```python
# Group related changes in single call
search_replace(
    file_path="module.py",
    replacements=[
        # All related changes together
        {"original_text": "import old_lib", "new_text": "import new_lib"},
        {"original_text": "old_lib.process()", "new_text": "new_lib.process()"},
        {"original_text": "old_lib.config", "new_text": "new_lib.config"},
    ]
)
```

## Web Integration

### Real-time Information Gathering

```python
# Search for latest documentation
search_web(
    query="pytest fixtures best practices 2024",
    timeRange="OneMonth"  # Recent results
)

# Fetch specific page content
fetch_content(
    url="https://docs.python.org/3/library/pathlib.html",
    query="Path methods for file operations"
)
```

## Debugging Superpowers

### Systematic Debugging Workflow

```python
# 1. Get compile/lint errors
get_problems(file_paths=["/project/src/main.py"])

# 2. Search for error context
search_codebase(
    query="how is main.py connected to config",
    key_words="main,config,connection"
)

# 3. Find related error handling
grep_code(regex="try:|except|raise", type="py")

# 4. Check terminal for runtime errors
run_in_terminal(command="python main.py --debug")
```

## Performance Optimization

### Token Efficiency

```python
# 1. Use concise search queries
search_codebase(query="auth flow", key_words="auth")  # Not "authentication flow process"

# 2. Limit file reads to relevant sections
read_file(file_path="large_file.py", start_line=100, end_line=200)

# 3. Use shallow memory search first
search_memory(depth="shallow", ...)  # Before deep

# 4. Target specific directories
search_codebase(target_directories=["/src/auth"], ...)
```

### Parallel Execution Rules

| Operation Type | Can Parallelize |
|---------------|-----------------|
| File reads | ✅ Yes |
| Code searches | ✅ Yes |
| LSP operations | ✅ Yes |
| Memory searches | ✅ Yes |
| Web searches | ✅ Yes |
| File edits | ❌ Sequential |
| Terminal commands | ❌ Sequential |

## Skill Synergy

Combine skills for maximum power:

```python
# Complex feature: PDF report analysis

# 1. Use pdf-processing skill
Skill(skill="pdf-processing")  # Guide extraction

# 2. Use data-analysis skill  
Skill(skill="data-analysis")  # Guide analysis

# 3. Use python-code-style skill
Skill(skill="python-code-style")  # Ensure quality

# Result: Comprehensive, high-quality implementation
```

## Pro Tips

### 1. Always Verify After Edit

```python
# Edit → Verify cycle
search_replace(...)
get_problems(file_paths=[...])  # Check immediately
run_in_terminal(command="pytest tests/")  # Verify
```

### 2. Use Todo for Complex Tasks

```python
# 3+ steps → use todo_write
# 1-2 steps → skip todo
```

### 3. Memory Before Major Changes

```python
# Recall project conventions before editing
search_memory(
    query="coding standards",
    keywords="style,conventions",
    category="development_code_specification"
)
```

### 4. Semantic Search First

```python
# Understanding → grep_code
search_codebase(query="how X works", ...)  # First
grep_code(regex="def X", ...)  # After understanding
```