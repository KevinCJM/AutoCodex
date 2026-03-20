from __future__ import annotations

import re

from .common import (
    ANSI_CODE_PATTERN,
    ASSISTANT_PREFIX_PATTERN,
    CLAUDE_SPINNER_ACTIVITY_PATTERN,
    CLAUDE_SPINNER_CHARS,
    CLAUDE_WELCOME_PATTERN,
    CODEX_MODEL_SELECTION_PROMPT_PATTERNS,
    CODEX_QUEUED_MESSAGE_PATTERNS,
    CODEX_UPDATE_PROMPT_PATTERNS,
    CODEX_TRUST_PROMPT_PATTERNS,
    CODEX_WELCOME_PATTERN,
    CODEX_QUEUED_MESSAGE_PATTERNS,
    ERROR_PATTERN,
    GEMINI_WELCOME_PATTERN,
    IDLE_PROMPT_PATTERN,
    IDLE_PROMPT_STRICT_PATTERN,
    IDLE_PROMPT_TAIL_LINES,
    TUI_FOOTER_PATTERN,
    TUI_PROGRESS_PATTERN,
    USER_PREFIX_PATTERN,
    WAITING_PROMPT_PATTERN,
    CliBackend,
    TerminalStatus,
)


class BaseOutputDetector:
    """这是所有 CLI 输出检测器的共同基类。"""

    @staticmethod
    def clean_ansi(text: str) -> str:
        """清理 ANSI 转义序列，保证后续正则匹配更稳定。"""
        return re.sub(ANSI_CODE_PATTERN, "", text)

    @staticmethod
    def _split_blocks(text: str) -> list[str]:
        """把连续文本按空行切成块，便于抽取最后一个有效答复。"""
        blocks: list[str] = []
        current_lines: list[str] = []

        for line in text.splitlines():
            if not line.strip():
                if current_lines:
                    blocks.append("\n".join(current_lines).strip())
                    current_lines = []
                continue
            current_lines.append(line.rstrip())

        if current_lines:
            blocks.append("\n".join(current_lines).strip())

        return [block for block in blocks if block]

    @staticmethod
    def _is_shell_prompt_line(line: str) -> bool:
        """识别普通 shell 提示符，避免把它误当成模型输出。"""
        stripped = line.strip()
        return bool(re.search(r"[$%#]\s*$", stripped)) and "❯" not in stripped

    @staticmethod
    def _tail_text(text: str, max_lines: int = 40) -> str:
        """只截取底部若干行，避免被长滚动历史污染当前状态判断。"""
        return "\n".join(text.splitlines()[-max_lines:])

    def has_trust_prompt(self, clean_output: str) -> bool:
        """默认不识别信任提示，由具体后端覆盖。"""
        return False

    def has_welcome_banner(self, clean_output: str) -> bool:
        """默认不识别欢迎横幅，由具体后端覆盖。"""
        return False

    def has_model_selection_prompt(self, clean_output: str) -> bool:
        """默认不识别启动模型选择提示，由具体后端覆盖。"""
        return False

    def has_update_prompt(self, clean_output: str) -> bool:
        """默认不识别 CLI 更新提示，由具体后端覆盖。"""
        return False

    def looks_like_shell_prompt(self, clean_output: str) -> bool:
        """默认识别底部几行里常见的 shell prompt。"""
        lines = [line for line in clean_output.splitlines() if line.strip()]
        return any(self._is_shell_prompt_line(line) for line in lines[-5:])

    def detect_status(self, output: str) -> TerminalStatus:
        """默认状态识别比较保守，只区分空输出和普通空闲状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR
        return TerminalStatus.IDLE

    def extract_last_message(self, output: str) -> str:
        """
        默认从清洗后的输出里提取最后一个有效文本块。

        这是 Claude 和 Gemini 的兜底方案，避免在没有结构化消息标记时完全拿不到答复。
        """
        clean_output = self.clean_ansi(output)
        lines: list[str] = []
        for line in clean_output.splitlines():
            stripped = line.rstrip()
            normalized = stripped.strip()
            if not normalized:
                lines.append("")
                continue

            if self._should_skip_line(normalized):
                continue
            lines.append(stripped)

        blocks = self._split_blocks("\n".join(lines))
        if not blocks:
            raise ValueError("No assistant response found in terminal output")
        return blocks[-1]

    def _should_skip_line(self, line: str) -> bool:
        """过滤明显属于 UI chrome 的行，减少把提示栏误当答复的概率。"""
        skip_patterns = (
            r"^\? for shortcuts",
            r"^\d+%\s+left",
            r"^context left",
            r"^(?:[A-Za-z0-9_.-]+\s+){0,2}(?:low|medium|high|xhigh|max)\s+·\s+\d+%\s+left(?:\s+·\s+.+)?$",
            r"^(?:model|directory)\s*:",
            r"^Tip:",
            r"^────────────────+",
            r"^[│┌┐└┘╭╮╰╯╷╵─═]+$",
            r"^Press (?:ESC|Esc|esc)",
            r"^↳\s+",
            r"^⚠\s+MCP",
        )
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in skip_patterns):
            return True
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in CODEX_QUEUED_MESSAGE_PATTERNS):
            return True
        if self._is_shell_prompt_line(line):
            return True
        return False


class CodexOutputDetector(BaseOutputDetector):
    """这个检测器只负责理解 Codex 的输出，不负责 tmux 生命周期。"""

    @staticmethod
    def _compute_tui_footer_cutoff(all_lines: list[str]) -> int:
        """
        计算 TUI footer 的起点，避免把底部建议文本误判成用户输入。
        """
        line_count = len(all_lines)
        footer_start_index = line_count

        for index in range(line_count - 1, max(line_count - IDLE_PROMPT_TAIL_LINES - 1, -1), -1):
            if re.search(TUI_FOOTER_PATTERN, all_lines[index]):
                footer_start_index = index
                break

        if footer_start_index == line_count:
            return len("\n".join(all_lines))

        for index in range(footer_start_index - 1, max(footer_start_index - 4, -1), -1):
            line = all_lines[index]
            if not line.strip():
                footer_start_index = index
            elif re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line):
                footer_start_index = index
                break
            else:
                break

        return len("\n".join(all_lines[:footer_start_index]))

    def has_trust_prompt(self, clean_output: str) -> bool:
        """识别工作区信任提示，便于启动时自动确认默认项。"""
        recent_output = self._tail_text(clean_output, max_lines=32)
        if not re.search(r"Press enter to continue", recent_output, re.IGNORECASE):
            return False
        return any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_TRUST_PROMPT_PATTERNS)

    def has_welcome_banner(self, clean_output: str) -> bool:
        """识别 Codex 欢迎页，判断 TUI 是否已经正常拉起。"""
        return bool(re.search(CODEX_WELCOME_PATTERN, clean_output))

    def has_model_selection_prompt(self, clean_output: str) -> bool:
        """
        识别 Codex 首次启动时的 GPT-5.4 升级选择菜单。

        这个提示会拦住后续 prompt 提交，所以要在 runtime 启动阶段就自动处理掉。
        """
        recent_output = self._tail_text(clean_output, max_lines=80)
        return all(
            re.search(pattern, recent_output, re.IGNORECASE)
            for pattern in CODEX_MODEL_SELECTION_PROMPT_PATTERNS
        )

    def has_update_prompt(self, clean_output: str) -> bool:
        """识别 Codex 的 CLI 升级提示，避免 runtime 误触发 Update now。"""
        recent_output = self._tail_text(clean_output, max_lines=60)
        return all(
            re.search(pattern, recent_output, re.IGNORECASE)
            for pattern in CODEX_UPDATE_PROMPT_PATTERNS
        )

    def looks_like_shell_prompt(self, clean_output: str) -> bool:
        """识别底部是否更像普通 shell 提示，而不是 Codex 自己的输入框。"""
        return super().looks_like_shell_prompt(clean_output)

    def detect_status(self, output: str) -> TerminalStatus:
        """根据 pane 输出识别 Codex 当前状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR

        tail_output = "\n".join(clean_output.splitlines()[-25:])
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        cutoff_position = (
            self._compute_tui_footer_cutoff(all_lines) if tui_footer_detected else len(clean_output)
        )

        last_user_match: re.Match[str] | None = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_position:
                last_user_match = match

        output_after_last_user = clean_output[last_user_match.start():] if last_user_match else clean_output
        assistant_after_last_user = bool(
            last_user_match
            and re.search(
                ASSISTANT_PREFIX_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            )
        )

        if self.has_trust_prompt(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER
        if self.has_update_prompt(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER
        if self.has_model_selection_prompt(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        bottom_lines = clean_output.strip().splitlines()[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt_at_end = any(
            re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line, re.IGNORECASE) for line in bottom_lines
        )

        if last_user_match is not None:
            if not assistant_after_last_user:
                if re.search(WAITING_PROMPT_PATTERN, output_after_last_user, re.IGNORECASE | re.MULTILINE):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(ERROR_PATTERN, output_after_last_user, re.IGNORECASE | re.MULTILINE):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR

        if has_idle_prompt_at_end:
            if re.search(TUI_PROGRESS_PATTERN, tail_output, re.MULTILINE):
                return TerminalStatus.PROCESSING

            if last_user_match is not None:
                if re.search(
                        ASSISTANT_PREFIX_PATTERN,
                        clean_output[last_user_match.start():],
                        re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.COMPLETED
                return TerminalStatus.IDLE

            return TerminalStatus.IDLE

        return TerminalStatus.PROCESSING

    def extract_last_message(self, output: str) -> str:
        """提取最后一轮 assistant 的答复，并过滤掉 spinner/progress 块。"""

        def extract_bullet_blocks(text: str) -> list[str]:
            blocks: list[str] = []
            current_block: list[str] = []

            for line in text.splitlines():
                if re.match(r"^\s*•\s*", line):
                    if current_block:
                        blocks.append("\n".join(current_block).strip())
                    current_block = [re.sub(r"^\s*•\s*", "", line, count=1).rstrip()]
                    continue

                if current_block:
                    stripped = line.strip()
                    if re.match(rf"^\s*{IDLE_PROMPT_PATTERN}", stripped):
                        blocks.append("\n".join(current_block).strip())
                        current_block = []
                        continue
                    if any(
                            re.search(pattern, stripped, re.IGNORECASE)
                            for pattern in CODEX_QUEUED_MESSAGE_PATTERNS
                    ):
                        blocks.append("\n".join(current_block).strip())
                        current_block = []
                        continue
                    if self._is_shell_prompt_line(stripped):
                        blocks.append("\n".join(current_block).strip())
                        current_block = []
                        continue
                    current_block.append(line.rstrip())

            if current_block:
                blocks.append("\n".join(current_block).strip())

            return [block for block in blocks if block]

        def is_progress_block(block: str) -> bool:
            first_line = block.splitlines()[0] if block.splitlines() else ""
            return bool(re.search(r"\((?:\d+[hms]\s*)+•\s*esc", first_line, re.IGNORECASE))

        def strip_ui_lines(text: str) -> str:
            kept_lines: list[str] = []
            for line in text.splitlines():
                normalized = line.strip()
                if not normalized:
                    kept_lines.append("")
                    continue
                if self._should_skip_line(normalized):
                    continue
                kept_lines.append(line.rstrip())
            return "\n".join(kept_lines).strip()

        def select_best_block(text: str) -> str:
            candidates = extract_bullet_blocks(text)
            if not candidates:
                candidates = self._split_blocks(text)

            for block in reversed(candidates):
                cleaned = strip_ui_lines(block)
                if not cleaned:
                    continue
                if is_progress_block(cleaned):
                    continue
                return cleaned
            raise ValueError("No Codex response found - no assistant content after filtering")

        clean_output = self.clean_ansi(output)
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        cutoff_position = (
            self._compute_tui_footer_cutoff(all_lines) if tui_footer_detected else len(clean_output)
        )

        user_matches = [
            match
            for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
            if match.start() < cutoff_position
        ]

        if user_matches:
            last_user_match = user_matches[-1]
            assistant_after_user = re.search(
                ASSISTANT_PREFIX_PATTERN,
                clean_output[last_user_match.start():],
                re.IGNORECASE | re.MULTILINE,
            )
            if not assistant_after_user:
                raise ValueError("No Codex response found - no assistant marker after last user message")

            response_start = last_user_match.start() + assistant_after_user.start()

            idle_after = re.search(
                IDLE_PROMPT_STRICT_PATTERN,
                clean_output[response_start:],
                re.MULTILINE,
            )
            if idle_after:
                end_position = response_start + idle_after.start()
            elif tui_footer_detected:
                end_position = cutoff_position
            else:
                end_position = len(clean_output)

            response_text = clean_output[response_start:end_position].strip()
            if response_text:
                response_text = re.sub(
                    r"^(?:assistant|codex|agent)\s*:\s*",
                    "",
                    response_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                return re.sub(r"^\s*•\s*", "", select_best_block(response_text), count=1).strip()

        assistant_matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        if not assistant_matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_assistant_match = assistant_matches[-1]
        start_position = last_assistant_match.end()
        idle_after = re.search(IDLE_PROMPT_STRICT_PATTERN, clean_output[start_position:], re.MULTILINE)
        end_position = start_position + idle_after.start() if idle_after else len(clean_output)
        final_answer = clean_output[start_position:end_position].strip()
        final_answer = re.sub(r"^\s*•\s*", "", select_best_block(final_answer), count=1).strip()
        if not final_answer:
            raise ValueError("Empty Codex response - no content found")
        return final_answer


class ClaudeOutputDetector(BaseOutputDetector):
    """这个检测器负责识别 Claude Code 的常见状态和最后答复。"""

    def has_welcome_banner(self, clean_output: str) -> bool:
        """识别 Claude Code 欢迎页。"""
        return bool(re.search(CLAUDE_WELCOME_PATTERN, clean_output))

    def _content_above_prompt_box(self, clean_output: str, max_lines: int = 40) -> str:
        """
        提取 Claude 底部输入框上方的内容。

        Claude 常把输入区画成上下两条横线，中间是 `❯` 提示，所以要先把这块 UI chrome 剥掉。
        """
        lines = clean_output.splitlines()[-max_lines:]
        border_count = 0
        for index in range(len(lines) - 1, -1, -1):
            if re.fullmatch(r"─+", lines[index].strip()):
                border_count += 1
                if border_count == 2:
                    return "\n".join(lines[:index])
        return "\n".join(lines)

    def detect_status(self, output: str) -> TerminalStatus:
        """根据 Claude Code 的常见 TUI 信号识别状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR

        extended_content = "\n".join(clean_output.splitlines()[-200:])
        if "⌕ Search…" in extended_content:
            return TerminalStatus.IDLE

        full_content = "\n".join(clean_output.splitlines()[-40:])
        full_lower_content = full_content.lower()

        if "ctrl+r to toggle" in full_lower_content:
            return TerminalStatus.IDLE

        if re.search(r"(?:do you want|would you like).+\n+[\s\S]*?(?:yes|❯)", full_lower_content):
            return TerminalStatus.WAITING_USER_ANSWER
        if re.search(r"❯\s+\d+\.", full_content):
            return TerminalStatus.WAITING_USER_ANSWER
        if "esc to cancel" in full_lower_content:
            return TerminalStatus.WAITING_USER_ANSWER
        if "enter to select" in full_lower_content:
            return TerminalStatus.WAITING_USER_ANSWER

        above_prompt_box = self._content_above_prompt_box(clean_output)
        above_lower_content = above_prompt_box.lower()
        if "esc to interrupt" in above_lower_content or "ctrl+c to interrupt" in above_lower_content:
            return TerminalStatus.PROCESSING
        if CLAUDE_SPINNER_ACTIVITY_PATTERN.search(above_prompt_box):
            return TerminalStatus.PROCESSING
        if re.search(r"^[{}].* for \d+[smh]".format(re.escape(CLAUDE_SPINNER_CHARS)), above_prompt_box, re.MULTILINE):
            return TerminalStatus.COMPLETED

        return TerminalStatus.IDLE

    def extract_last_message(self, output: str) -> str:
        """优先从 Claude 输入框上方提取最后一个有效文本块。"""
        clean_output = self.clean_ansi(output)
        content = self._content_above_prompt_box(clean_output)
        lines: list[str] = []
        for line in content.splitlines():
            normalized = line.strip()
            if not normalized:
                lines.append("")
                continue
            if self._should_skip_line(normalized):
                continue
            if re.search(r"^❯\s+\d+\.", normalized):
                continue
            if CLAUDE_SPINNER_ACTIVITY_PATTERN.search(normalized):
                continue
            if re.search(r"^[{}].* for \d+[smh]".format(re.escape(CLAUDE_SPINNER_CHARS)), normalized):
                continue
            lines.append(line.rstrip())

        blocks = self._split_blocks("\n".join(lines))
        if not blocks:
            raise ValueError("No Claude Code response found in terminal output")
        return blocks[-1]


class GeminiOutputDetector(BaseOutputDetector):
    """这个检测器负责识别 Gemini CLI 的常见状态和最后答复。"""

    def has_welcome_banner(self, clean_output: str) -> bool:
        """识别 Gemini CLI 欢迎页。"""
        return bool(re.search(GEMINI_WELCOME_PATTERN, clean_output))

    def detect_status(self, output: str) -> TerminalStatus:
        """根据 Gemini CLI 的确认框和取消提示识别状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR

        lower_content = clean_output.lower()
        if "waiting for user confirmation" in lower_content:
            return TerminalStatus.WAITING_USER_ANSWER
        if (
            "│ apply this change" in lower_content
            or "│ allow execution" in lower_content
            or "│ do you want to proceed" in lower_content
        ):
            return TerminalStatus.WAITING_USER_ANSWER
        if re.search(
            r"(allow execution|do you want to|apply this change)[\s\S]*?\n+[\s\S]*?\byes\b",
            lower_content,
        ):
            return TerminalStatus.WAITING_USER_ANSWER
        if "esc to cancel" in lower_content:
            return TerminalStatus.PROCESSING
        return TerminalStatus.IDLE

    def extract_last_message(self, output: str) -> str:
        """优先剥离底部输入框，再提取 Gemini 的最后一个有效文本块。"""
        clean_output = self.clean_ansi(output)
        lines = clean_output.splitlines()

        input_box_start = len(lines)
        for index in range(len(lines) - 1, -1, -1):
            normalized = lines[index].strip().lower()
            if normalized.startswith("│ >") or normalized == ">" or normalized.endswith("│ >"):
                input_box_start = index
                break
        content = "\n".join(lines[:input_box_start])

        filtered_lines: list[str] = []
        for line in content.splitlines():
            normalized = line.strip()
            if not normalized:
                filtered_lines.append("")
                continue
            if self._should_skip_line(normalized):
                continue
            if normalized.lower().startswith("waiting for user confirmation"):
                continue
            if normalized.startswith("│ Apply this change") or normalized.startswith("│ Allow execution"):
                continue
            if normalized.startswith("│ Do you want to proceed"):
                continue
            filtered_lines.append(line.rstrip())

        blocks = self._split_blocks("\n".join(filtered_lines))
        if not blocks:
            raise ValueError("No Gemini CLI response found in terminal output")
        return blocks[-1]


def build_output_detector(backend: CliBackend) -> BaseOutputDetector:
    """按后端返回对应的输出检测器。"""
    if backend == CliBackend.CODEX:
        return CodexOutputDetector()
    if backend == CliBackend.CLAUDE:
        return ClaudeOutputDetector()
    if backend == CliBackend.GEMINI:
        return GeminiOutputDetector()
    raise ValueError(f"Unsupported backend: {backend}")
