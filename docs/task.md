# x-ai CLI — Implementation Task Breakdown

## Phase 1: Foundation (Models + Config + Logger)

**Commit: `feat: add project foundation — models, config, logger`**

- [x] `x_ai_cli/__init__.py` — package init, export `__version__`
- [x] `x_ai_cli/__main__.py` — `python -m x_ai_cli` entrypoint
- [x] `x_ai_cli/config.py` — `Config` dataclass
- [x] `x_ai_cli/models.py` — dataclasses + YAML frontmatter parser/writer
- [x] `x_ai_cli/logger.py` — rich console + file logging
- [x] `requirements.txt` — `rich`, `pyyaml`

---

## Phase 2: Skill Files

**Commit: `feat: add skill files for leader and worker agents`**

- [x] `skills/leader.md` — Leader skill (DECOMPOSE, REVIEW, MERGE)
- [x] `skills/worker.md` — Worker skill (code gen + feedback handling)

---

## Phase 3: Agent Runner

**Commit: `feat: add agent runner with subprocess + timeout`**

- [x] `x_ai_cli/agent_runner.py` — subprocess launcher, timeout, error recovery

---

## Phase 4: Orchestrator Pipeline

**Commit: `feat: add orchestrator pipeline with round loop`**

- [x] `x_ai_cli/orchestrator.py` — full pipeline + round loop + quality gate

---

## Phase 5: CLI Entrypoint

**Commit: `feat: add CLI entrypoint with argparse + rich`**

- [x] `x_ai_cli/main.py` — argparse + rich output + wire everything

---

## Phase 6: Verification

**Commit: `test: add unit tests + manual verification`**

- [x] All imports pass
- [x] CLI `--help` works
- [x] YAML frontmatter round-trip OK
- [ ] Manual: end-to-end with real agents
- [ ] Manual: parallel workers, retry loop, timeout, logs
