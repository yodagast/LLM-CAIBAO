---
name: everything-claude-code
description: Comprehensive guide for Claude Code usage, covering all features, commands, workflows, and best practices. Use when exploring Claude Code capabilities, learning new features, or troubleshooting issues.
---

# Everything Claude Code

## Core Capabilities

Claude Code is an AI coding assistant integrated into your IDE for autonomous and collaborative coding.

### Key Features

1. **Code Generation & Editing** - Write, modify, and refactor code
2. **Codebase Understanding** - Search, read, and analyze project files
3. **Terminal Execution** - Run commands, tests, and builds
4. **Multi-file Operations** - Batch edits across multiple files
5. **Web Search & Fetch** - Get real-time information
6. **Memory System** - Remember project context and preferences

## Commands Reference

### File Operations

| Command | Description |
|---------|-------------|
| `read_file` | Read file contents (supports images) |
| `create_file` | Create new file (max 1000 lines) |
| `search_replace` | Edit existing file |
| `delete_file` | Delete file safely |
| `search_file` | Find files by glob pattern |

### Code Search

| Command | Description |
|---------|-------------|
| `search_codebase` | Semantic search by meaning |
| `grep_code` | Regex search in files |
| `lsp` | Language server features (definitions, references) |
| `list_dir` | List directory contents |

### Execution

| Command | Description |
|---------|-------------|
| `run_in_terminal` | Execute shell commands |
| `get_terminal_output` | Get output from background processes |
| `get_problems` | Get compile/lint errors |

### Planning & Workflow

| Command | Description |
|---------|-------------|
| `todo_write` | Manage task lists |
| `switch_mode` | Switch to plan/debug/ask modes |
| `create_plan` | Create implementation plan |

### Knowledge & Memory

| Command | Description |
|---------|-------------|
| `search_memory` | Retrieve stored memories |
| `update_memory` | Create/update/delete memories |
| `search_web` | Web search for real-time info |
| `fetch_content` | Fetch webpage content |

## Best Practices

### 1. File Editing

```python
# ALWAYS use search_replace for existing files
# NEVER use create_file unless explicitly needed

# Good - editing existing file
search_replace(
    file_path="/path/to/file.py",
    replacements=[{
        "original_text": "old_code",
        "new_text": "new_code"
    }]
)

# Bad - recreating entire file
create_file(file_path="/path/to/file.py", file_content="...")
```

### 2. Parallel Tool Calls

```python
# Good - parallel for independent operations
read_file(file_path="a.py")
read_file(file_path="b.py")
read_file(file_path="c.py")

# Bad - sequential when parallel is possible
read_file(file_path="a.py")  # wait
read_file(file_path="b.py")  # wait
read_file(file_path="c.py")  # wait
```

### 3. Sequential Operations

```python
# MUST be sequential: file edits, terminal commands
search_replace(file_path="a.py", ...)  # first
search_replace(file_path="b.py", ...)  # second
run_in_terminal(command="pytest")      # third
```

### 4. Task Planning

```python
# Use todo_write for complex tasks (3+ steps)
todo_write(
    merge=False,
    todos=[
        {"id": "step1", "content": "Setup dependencies", "status": "PENDING"},
        {"id": "step2", "content": "Create main module", "status": "PENDING"},
        {"id": "step3", "content": "Add tests", "status": "PENDING"},
    ]
)

# Update as you progress
todo_write(merge=True, todos=[
    {"id": "step1", "status": "COMPLETE"},
    {"id": "step2", "status": "IN_PROGRESS"},
])
```

### 5. Memory Usage

```python
# Create memory for important project knowledge
update_memory(
    action="create",
    title="Database Connection Pattern",
    content="Use connection pooling with max_connections=10",
    keywords="database,connection,pooling",
    category="project_tech_stack"
)

# Retrieve when needed
search_memory(
    query="database configuration",
    keywords="database,connection",
    category="project_tech_stack",
    depth="shallow"
)
```

## Workflow Patterns

### New Feature Development

```
1. Plan (switch_mode → plan)
2. Search codebase for related code
3. Create todo list
4. Implement step by step
5. Run tests
6. Update memory if needed
```

### Bug Fixing

```
1. Get problems (get_problems)
2. Search codebase for error context
3. Identify root cause
4. Apply fix (search_replace)
5. Verify fix (run tests)
```

### Code Review

```
1. Read relevant files
2. Check against python-code-style skill
3. Provide feedback with specific line references
4. Suggest improvements
```

## Common Pitfalls

### ❌ Avoid

1. **Creating new files unnecessarily** - Prefer editing existing
2. **Sequential parallel operations** - Maximize efficiency
3. **Creating .md documentation proactively** - Only when requested
4. **Guessing line numbers** - Use exact matches
5. **Ignoring gitignore** - Respect project conventions

### ✅ Prefer

1. **Semantic search first** - Use search_codebase for understanding
2. **Parallel tool calls** - Execute independent operations together
3. **Context managers** - Use `with` for file operations
4. **Type hints** - Add for all public functions
5. **Meaningful names** - Descriptive variable/function names

## Mode Switching

| Mode | When to Use |
|------|-------------|
| `plan` | Complex architecture decisions, large scope tasks |
| `debug` | Investigating bugs with runtime evidence |
| `ask` | Read-only exploration, answering questions |
| Default | Implementation mode (current) |

## Skills Integration

Combine multiple skills for comprehensive workflows:

```markdown
# Financial Report Analysis Workflow

1. Use **pdf-processing** → Extract data from PDF
2. Use **data-analysis** → Analyze extracted data
3. Use **python-code-style** → Ensure code quality
4. Use **tushare-skill** → Get supplementary market data
```