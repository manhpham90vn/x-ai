# Simplified Local CLI — Multi-Agent AI Coding System

Orchestrator CLI chạy local, điều phối 3 AI coding tools thật qua file-based protocol:

| Role | Tool | Launch Command |
|---|---|---|
| **Leader** (task split, review, merge) | Claude Code | `claude -p ... --system-prompt-file skill.md` |
| **Worker 1** (code gen) | Antigravity | `antigravity ... --skill skill.md` |
| **Worker 2** (code gen) | Codex | `codex --approval-mode full-auto ...` |


## Communication Protocol

Tất cả agent giao tiếp qua **file-based task/result Markdown**:

```
tasks/
├── {task_id}.task.md          # Orchestrator writes → Agent reads
└── {task_id}.result.md        # Agent writes → Orchestrator reads
logs/
└── {run_id}/                  # Logs cho mỗi lần chạy
    ├── orchestrator.log
    ├── {task_id}.leader.log
    └── {task_id}.worker-{n}.log
```

### Task file contract

**Task file** `{task_id}.task.md` (Orchestrator → Agent):
```markdown
---
id: "uuid"
type: "decompose|generate|review|merge|execute"
work_dir: "/path/to/project"
round: 1
parent_task_id: "uuid"          # null nếu là root task
feedback: null                  # structured feedback từ round trước (nếu có)
---

## Instructions
Please execute the following prompt:
Build a REST API for user auth with JWT...

## Context
[Optional: code files, constraints, interface contracts]
```

### Result file contract

**Result file** `{task_id}.result.md` (Agent → Orchestrator):
```markdown
---
id: "uuid"
status: "success|error|partial"
score: 85                        # Leader review score (0-100), chỉ có khi type=review
round: 1
files_changed:
  - src/auth.py
  - src/utils.py
error_message: null              # Chi tiết lỗi nếu status=error
execution_result:                # Chỉ có khi type=execute
  exit_code: 0
  tests_passed: true
---

## Summary
Created the JWT modules...

## Approach
[Mô tả approach, reasoning, assumptions]
```

Each agent has a **skill file** that teaches it how to read/execute/write the Markdown result files.

## Pipeline Flow

```
User Request
    │
    ▼
┌──────────────────────────────────────────────┐
│  [1] DECOMPOSE — Leader (Claude Code)        │
│  Prompt: "Split this task into sub-tasks"    │
│  Output: list of sub-tasks in result.md      │
└──────────────┬───────────────────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
┌─────────────┐ ┌─────────────┐
│ [2] WORKER 1│ │ [2] WORKER 2│   ← Parallel
│ Antigravity │ │ Codex       │
│ (code gen)  │ │ (code gen)  │
└──────┬──────┘ └──────┬──────┘
       │               │
       └───────┬───────┘
               ▼
┌──────────────────────────────────────────────┐
│  [3] REVIEW — Leader (Claude Code)           │
│  Prompt: "Review both solutions, score them" │
│  Output: scores + best pick + merge plan     │
└──────────────┬───────────────────────────────┘
               │
               ▼  (score >= threshold?)
              YES ──────────────────┐
               │                    │
               ▼                    │
┌──────────────────────────────────────────────┐
│  [4] EXECUTE — Run tests in work_dir         │
│  Command: pytest / npm test / go test        │
│  Output: exit_code + test results            │
└──────────────┬───────────────────────────────┘
               │
          PASS? ├── YES ──┐
               │          │
               NO         ▼
               │   ┌──────────────────────────────────┐
               │   │  [5] MERGE — Leader (Claude Code) │
               │   │  Prompt: "Merge best parts"       │
               │   │  Output: final solution            │
               │   └───────────────────────────────────┘
               │
               ▼
        ┌─────────────┐
        │  Round < 3? │
        │  YES → goto [2] with feedback
        │  NO  → accept best-effort + warnings
        └─────────────┘
```

### Round/Retry Logic

```
for round in 1..MAX_ROUNDS:
  1. Workers gen code (parallel)
  2. Leader review + score mỗi solution (0–100)
  3. If best_score >= QUALITY_THRESHOLD (default 70):
       → Execute tests trong work_dir
       → If tests pass → MERGE → DONE
       → If tests fail → build feedback → next round
  4. Else:
       → Leader tạo structured feedback:
         - mandatory_fixes: issues bắt buộc sửa
         - score_gaps: dimensions yếu nhất
         - execution_errors: lỗi khi chạy test (nếu có)
       → Workers nhận feedback → gen lại (round+1)
  5. After MAX_ROUNDS → accept best-effort solution + warnings
```

## Proposed Changes

### Skill Files (installed into each agent)

#### [NEW] [skills/leader.md]

Skill file for Claude Code (Leader role). Instructs it to:
- Read task MD → execute prompt → write result MD
- For DECOMPOSE tasks: return the list of sub-tasks in the body of `result.md`
- For REVIEW tasks: return scores and recommendation in the body
- For MERGE tasks: write final code files + summary

**Template hướng dẫn trong skill file:**

```markdown
# Leader Agent Skill

You are the Leader agent in a multi-agent coding system.

## Protocol
1. Read the task file at the path provided as your prompt
2. Parse YAML frontmatter to get `id`, `type`, `work_dir`
3. Execute based on `type`:
   - `decompose`: Split the task into independent sub-tasks
   - `review`: Score each worker's solution (0-100) on: correctness, code_quality, security, performance, maintainability
   - `merge`: Combine best parts of solutions into final code
4. Write result to `{same_dir}/{id}.result.md`

## Result Format
Your result MUST follow this exact YAML frontmatter format:

---
id: "{same id from task}"
status: "success|error"
score: <number 0-100>     # only for review tasks
round: <current round>
files_changed:
  - path/to/file1
  - path/to/file2
---

## Body content here...

## Example: DECOMPOSE
Input: "Build a REST API for user auth with JWT"
Output body:
- Sub-task 1: Implement JWT token generation module
- Sub-task 2: Create user registration endpoint
- Sub-task 3: Create login endpoint with token response

## Example: REVIEW
Output body:
### Worker 1 (Antigravity) — Score: 82
- Correctness: 85 — Logic is sound
- Security: 65 — Uses deprecated HS256
- ...
### Recommendation: Worker 1 as base, merge Worker 2's input validation
```

#### [NEW] [skills/worker.md](file:///home/manh/Project/self/x-ai/x_ai_cli/skills/worker.md)

Skill file for Workers (Antigravity & Codex). Instructs it to:
- Read task MD → generate code → write result MD
- Write generated code files directly to `work_dir`
- Return summary with file list and approach description in the body

**Template hướng dẫn trong skill file:**

```markdown
# Worker Agent Skill

You are a Worker agent in a multi-agent coding system.

## Protocol
1. Read the task file at the path provided as your prompt
2. Parse YAML frontmatter to get `id`, `work_dir`, `round`, `feedback`
3. If `round > 1` and `feedback` exists: address ALL mandatory_fixes from feedback
4. Generate code and write files to `work_dir`
5. Write result to `{same_dir}/{id}.result.md`

## Result Format
---
id: "{same id from task}"
status: "success|error"
round: <current round>
files_changed:
  - path/to/file1.py
  - path/to/file2.py
---

## Summary
Brief description of what was built.

## Approach
Detailed reasoning and design decisions.

## Assumptions
- Assumption 1
- Assumption 2

## IMPORTANT RULES
- You MUST write result to the EXACT path: `{task_dir}/{id}.result.md`
- ALL generated code goes into `work_dir`
- If you receive feedback, you MUST address every item in `mandatory_fixes`
```

---

### Core Orchestrator

#### [NEW] [x\_ai\_cli/\_\_init\_\_.py]

Package init. Exports `__version__`.

#### [NEW] [x\_ai\_cli/main.py]

CLI entrypoint:
```bash
python -m x_ai_cli "Build a REST API for user auth with JWT"
python -m x_ai_cli --workers 2 --max-rounds 2 "..."
python -m x_ai_cli --file task.txt
python -m x_ai_cli --verbose "..."          # chi tiết logs
python -m x_ai_cli --test-cmd "pytest" "..."  # custom test command
```
Uses `argparse` + `rich` for terminal output.

#### [NEW] [x\_ai\_cli/\_\_main\_\_.py]

`python -m x_ai_cli` entrypoint:
```python
from x_ai_cli.main import main

if __name__ == "__main__":
    main()
```

#### [NEW] [x\_ai\_cli/config.py]

Configuration dataclass:
```python
@dataclass
class Config:
    # Agent binary paths
    claude_bin: str = "claude"
    antigravity_bin: str = "antigravity"
    codex_bin: str = "codex"

    # Pipeline settings
    work_dir: str = "."
    tasks_dir: str = "tasks"
    logs_dir: str = "logs"
    max_rounds: int = 3
    quality_threshold: float = 70.0
    worker_timeout_sec: int = 300       # 5 phút per agent
    leader_timeout_sec: int = 600       # 10 phút cho leader
    test_command: str | None = None     # pytest, npm test, etc.

    # Logging
    verbose: bool = False
    log_to_file: bool = True
```

#### [NEW] [x\_ai\_cli/models.py]

Dataclasses: `Task`, `SubTask`, `AgentTask`, `AgentResult`, `ReviewResult`, `Score`, `Feedback`, `ExecutionResult`. Supports parsing YAML frontmatter from Markdown.

```python
@dataclass
class Feedback:
    """Structured feedback cho round tiếp theo."""
    mandatory_fixes: list[str]
    score_gaps: dict[str, str]          # dimension → "current → target"
    execution_errors: list[str]
    previous_best_approach: str

@dataclass
class ExecutionResult:
    """Kết quả chạy test."""
    exit_code: int
    stdout: str
    stderr: str
    tests_passed: bool
```

#### [NEW] [x\_ai\_cli/orchestrator.py]

Main pipeline logic:
- `decompose_task()` → dispatch to Leader
- `run_workers(feedback=None)` → dispatch to Workers in parallel (`asyncio`)
- `review_solutions()` → dispatch to Leader for scoring
- `execute_tests()` → run test command in `work_dir` via subprocess
- `merge_solutions()` → dispatch to Leader for final merge
- `run_pipeline()` → orchestrate full round loop:

```python
async def run_pipeline(self, user_request: str) -> PipelineResult:
    sub_tasks = await self.decompose_task(user_request)
    
    for sub_task in sub_tasks:
        feedback = None
        best_solution = None
        
        for round_num in range(1, self.config.max_rounds + 1):
            logger.info(f"Round {round_num}/{self.config.max_rounds}")
            
            # Phase 1: Parallel code gen
            solutions = await self.run_workers(sub_task, round_num, feedback)
            
            if not solutions:
                logger.error("No workers responded")
                break
            
            # Phase 2: Leader review + score
            review = await self.review_solutions(sub_task, solutions)
            best_solution = review.best_solution
            
            # Phase 3: Quality gate
            if review.best_score >= self.config.quality_threshold:
                # Phase 4: Execute tests (if test_command configured)
                if self.config.test_command:
                    exec_result = await self.execute_tests()
                    if exec_result.tests_passed:
                        return await self.merge_solutions(sub_task, review)
                    else:
                        feedback = self.build_feedback(review, exec_result)
                        continue
                else:
                    return await self.merge_solutions(sub_task, review)
            else:
                feedback = self.build_feedback(review, exec_result=None)
        
        # Exhausted rounds → best-effort
        logger.warning("Max rounds exhausted, returning best-effort")
        return PipelineResult(status="best_effort", solution=best_solution)
```

#### [NEW] [x\_ai\_cli/agent\_runner.py]

Subprocess launcher for each agent type:
- `run_claude(task_file)` → `claude -p "..." --system-prompt-file leader.md --dangerously-skip-permissions`
- `run_antigravity(task_file)` → launches Antigravity with skill
- `run_codex(task_file)` → `codex --approval-mode full-auto -q "..."`
- Polls for `*.result.md` with timeout
- Returns parsed `AgentResult` (extracts YAML frontmatter + body)

**Error handling:**

```python
async def run_agent(self, agent_type: str, task_file: str) -> AgentResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *self.build_command(agent_type, task_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=self.config.worker_timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        logger.error(f"{agent_type} timed out after {self.config.worker_timeout_sec}s")
        return AgentResult(status="error", error_message="Agent timed out")
    except FileNotFoundError:
        logger.error(f"{agent_type} binary not found")
        return AgentResult(status="error", error_message=f"{agent_type} not installed")
    
    # Parse result.md
    result_file = task_file.replace(".task.md", ".result.md")
    return self.parse_result(result_file)
```

#### [NEW] [x\_ai\_cli/logger.py]

Logging setup:
- Console output via `rich` (progress bars, status spinners, colored output)
- File logging to `logs/{run_id}/orchestrator.log`
- Per-agent logs: `logs/{run_id}/{task_id}.{agent_type}.log`
- `--verbose` flag enables debug-level console output
- Log mỗi phase transition: DECOMPOSE → WORKERS → REVIEW → EXECUTE → MERGE

---

### Dependencies

#### [NEW] [requirements.txt]

```
rich>=13.0
pyyaml>=6.0
```

> [!IMPORTANT]
> Yêu cầu cài sẵn trên máy: `claude` CLI, `antigravity`, `codex` CLI.

## Verification Plan

### Automated Testing
1. Unit tests cho `models.py` — parse YAML frontmatter đúng
2. Unit tests cho `agent_runner.py` — mock subprocess, verify timeout handling
3. Unit tests cho round loop logic — verify retry khi score thấp, stop khi pass

### Manual Testing
1. Chạy với task đơn giản, verify mỗi phase tạo đúng file task/result MD
2. Verify parallel workers chạy đồng thời
3. Verify Leader review và merge kết quả đúng
4. Verify retry loop hoạt động khi score < threshold
5. Verify timeout handling — kill agent sau 5 phút hung
6. Verify logs ghi đúng vào `logs/` folder
7. Verify `--verbose` hiển thị chi tiết
8. Verify test execution (với `--test-cmd pytest`)
