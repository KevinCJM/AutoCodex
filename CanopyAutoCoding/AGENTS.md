# CanopyAutoCoding Development Guide

## Project Purpose
- This repository implements a staged automated development workflow.
- Human interaction happens in terminal CLI.
- Agent execution is hosted in `tmux`.
- Business progression should be driven by files, not by parsing terminal prose.

## File Taxonomy
- `Axx_*.py`: stage orchestrators. Each `Axx` owns one workflow stage.
- `Bxx_*.py`: support modules for a specific stage.
- `Prompt_xx_*.py`: stage-specific prompt templates and prompt builders.
- `Txx_*.py`: shared runtime, protocol, and utility modules reused across stages.
- `tests/`: regression coverage for stage logic, runtime, and CLI behavior.
- `Z99_Canopy_自动化开发流程.md`: design draft / workflow reference, not the runtime truth source.

## Current Stage Mapping
- `A00_main.py`: top-level stage entry and stage chaining.
- `A01_Routing_LayerPlanning.py`: AGENT initialization / routing-layer setup.
- `A02_RequirementsAnalysis.py`: requirement intake and requirement analysis workflow.
- `B01_terminal_interaction.py`: terminal control surface for routing stage.
- `T02_tmux_agents.py`: shared `tmux + coding agent` runtime.
- `T05_hitl_runtime.py`: shared file-based HITL orchestration.
- `T06_terminal_progress.py`: shared terminal spinner/progress rendering.

## Architectural Rules
- Stage-specific prompts must live in their matching `Prompt_xx_*.py`.
- Prompt fragments reused by multiple stages must live in `T04_common_prompt.py`.
- `Axx` should orchestrate; they should not embed large prompt bodies.
- Shared runtime behaviors belong in `Txx`, not in a single stage file.
- Do not make business gates depend on terminal UI text when a file protocol can be used instead.

## Runtime Principles
- `tmux` is the session host, not the business truth source.
- Agent output shown in terminal is for observation/debugging only unless no file contract exists.
- Stage completion should be decided by structured files such as:
  - `turn_status.json`
  - stage status JSON
  - final stage artifacts
- Runtime health may still rely on provider-specific shell / prompt / phase detection.

## File-Driven Workflow Rules
- Every agent turn should have an explicit file contract.
- `turn_status.json` must be written last for that turn.
- The system should validate:
  - schema version
  - turn id
  - phase
  - status
  - referenced artifact paths
  - artifact hashes when required
- Quiet-window validation is preferred before accepting a turn as complete.

## HITL Rules
- HITL must be file-driven.
- Agent questions to human should be written to a dedicated markdown file.
- Human replies are collected by the system and wrapped back into the next agent prompt.
- Agent memory/clarification cache should be maintained in a dedicated record file.
- HITL should loop until the agent explicitly indicates information is complete through the file contract.

## Routing Layer Stage Rules
- Final routing-layer artifacts are:
  - `AGENTS.md`
  - `docs/repo_map.json`
  - `docs/task_routes.json`
  - `docs/pitfalls.json`
- Audit/refine intermediate files are temporary and should be cleaned on success.
- Routing stage success should clean routing-stage `tmux` sessions and runtime leftovers, preserving only final artifacts.

## Requirement Analysis Stage Rules
- Requirement intake must end with `{需求名}_原始需求.md`.
- Requirement analysis must end with `{需求名}_需求澄清.md`.
- HITL artifacts may be temporary, but human clarification record retention is allowed when it is part of the final analysis trace.
- Notion intake should use a temporary agent workflow plus file protocol, not stdout parsing.

## Terminal UX Rules
- Long-running agent work should show a single-line spinner/progress indicator.
- Spinner refresh should happen in-place on one line, not by flooding the terminal.
- When a stage launches `tmux` sessions, print:
  - runtime directory
  - session name(s)
  - attach command(s)
- When human input is needed, stop spinner rendering before reading from stdin.

## Coding Conventions
- Prefer small pure helpers for parsing, validation, and path building.
- Keep stage orchestrators readable; move reusable pieces into `Txx`.
- Use absolute paths when runtime files are exchanged with agents.
- Preserve Chinese filenames and user-facing labels where the workflow already uses them.
- Avoid adding dependencies unless the existing environment clearly requires them.

## Testing Expectations
- Every workflow change should add or update unit tests.
- Prefer deterministic file-based tests over terminal-text assertions.
- Cover:
  - prompt builder contracts
  - file validation logic
  - resume behavior
  - HITL loops
  - cleanup behavior
  - CLI interaction ordering
- Run targeted tests first, then a broader regression pass.

## Change Discipline
- Preserve backward compatibility when a prompt/function signature is already in active use, or add a compatibility layer.
- Do not silently move stage-specific prompt logic into runtime modules.
- Do not rely on hidden terminal state or manual operator intervention for normal stage progression.
- Prefer explicit files, explicit state transitions, and explicit cleanup.

## Practical Rule Of Thumb
- If a behavior answers “what should happen in this stage?”, put it in `Axx` or `Prompt_xx`.
- If a behavior answers “how do all stages run agents / HITL / progress / validation?”, put it in `Txx`.
