from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from A02_RequirementsAnalysis import (
    DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR,
    DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT,
    DEFAULT_REQUIREMENTS_ANALYSIS_MODEL,
    DEFAULT_NOTION_EFFORT,
    DEFAULT_NOTION_MODEL,
    NOTION_RUNTIME_ROOT_NAME,
    REQUIREMENTS_ANALYSIS_STAGE_NAME,
    REQUIREMENTS_ANALYSIS_TURN_PHASE,
    NOTION_STAGE_NAME,
    NOTION_TURN_PHASE,
    build_notion_retry_message,
    build_notion_hitl_paths,
    build_notion_followup_prompt,
    build_output_path,
    build_requirements_analysis_paths,
    build_parser,
    build_prefixed_sha256,
    ensure_requirements_hitl_record_file,
    list_existing_requirements,
    cleanup_notion_runtime_paths,
    cleanup_stage_runtime_paths,
    collect_requirements_analysis_agent_selection,
    collect_request,
    collect_text_input_interactive,
    extract_text_from_docx,
    extract_text_from_local_file,
    extract_text_from_pdf,
    format_notion_failure_message,
    main,
    render_agent_boot_progress_line,
    render_requirements_analysis_progress_line,
    render_requirements_analysis_tmux_start_summary,
    render_notion_progress_line,
    render_notion_tmux_start_summary,
    run_requirement_intake_stage,
    run_requirements_analysis,
    run_requirements_stage,
    prompt_requirement_name_selection,
    prompt_requirement_name_with_existing,
    prompt_project_dir,
    sanitize_requirement_name,
    stdin_is_interactive,
    validate_notion_status,
)
from Prompt_02_RequirementIntake import (
    NOTION_STATUS_ERROR,
    NOTION_STATUS_HITL,
    NOTION_STATUS_OK,
    NOTION_STATUS_SCHEMA_VERSION,
    get_notion_requirement,
)
from Prompt_03_RequirementsClarification import (
    REQUIREMENTS_STATUS_HITL,
    REQUIREMENTS_STATUS_OK,
    REQUIREMENTS_STATUS_SCHEMA_VERSION,
    fintech_ba,
    hitl_bck,
    requirements_understand,
    resume_requirements_understand,
)
from T09_terminal_ops import PromptBackRequested


def _make_simple_pdf(text: str) -> bytes:
    objects: list[str] = []

    def add(obj: str) -> None:
        objects.append(obj)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    content = f"BT\n/F1 12 Tf\n72 100 Td\n({text}) Tj\nET\n"
    add("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>")
    add(f"<< /Length {len(content.encode('latin1'))} >>\nstream\n{content}endstream")
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    parts = ["%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part.encode("latin1")) for part in parts))
        parts.append(f"{index} 0 obj\n{obj}\nendobj\n")
    xref_offset = sum(len(part.encode("latin1")) for part in parts)
    parts.append(f"xref\n0 {len(objects) + 1}\n")
    parts.append("0000000000 65535 f \n")
    for offset in offsets[1:]:
        parts.append(f"{offset:010d} 00000 n \n")
    parts.append(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n")
    return "".join(parts).encode("latin1")


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def _make_simple_docx(path: Path, lines: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>"
        for line in lines
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<?xml version='1.0' encoding='UTF-8'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'></Types>")
        archive.writestr("_rels/.rels", "<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'></Relationships>")
        archive.writestr("word/document.xml", document_xml)


def _write_stage_status(
    status_path: Path,
    *,
    turn_id: str,
    hitl_round: int,
    status: str,
    output_path: Path | None,
    question_path: Path | None,
    record_path: Path | None,
    summary: str,
    extra: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": NOTION_STATUS_SCHEMA_VERSION,
        "stage": NOTION_STAGE_NAME,
        "turn_id": turn_id,
        "hitl_round": hitl_round,
        "status": status,
        "summary": summary,
        "output_path": str(output_path.resolve()) if output_path else "",
        "question_path": str(question_path.resolve()) if question_path else "",
        "record_path": str(record_path.resolve()) if record_path else "",
        "artifact_hashes": {},
        "written_at": "2026-04-13T12:00:00+08:00",
    }
    if extra:
        payload.update(extra)
    artifact_hashes: dict[str, str] = {}
    for candidate in (output_path, question_path, record_path):
        if candidate is None:
            continue
        artifact_hashes[str(candidate.resolve())] = build_prefixed_sha256(candidate)
    payload["artifact_hashes"] = artifact_hashes
    status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_turn_status(
    turn_status_path: Path,
    *,
    turn_id: str,
    stage_status_path: Path,
    artifact_paths: list[Path],
    phase: str = NOTION_TURN_PHASE,
) -> None:
    artifacts: dict[str, str] = {"stage_status": str(stage_status_path.resolve())}
    artifact_hashes = {str(stage_status_path.resolve()): build_prefixed_sha256(stage_status_path)}
    for index, artifact_path in enumerate(artifact_paths, start=1):
        artifacts[f"artifact_{index}"] = str(artifact_path.resolve())
        artifact_hashes[str(artifact_path.resolve())] = build_prefixed_sha256(artifact_path)
    turn_status_path.parent.mkdir(parents=True, exist_ok=True)
    turn_status_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "turn_id": turn_id,
                "phase": phase,
                "status": "done",
                "artifacts": artifacts,
                "artifact_hashes": artifact_hashes,
                "written_at": "2026-04-13T12:00:00+08:00",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


class RequirementsAnalysisIntakeTests(unittest.TestCase):
    @staticmethod
    def _stub_analysis_result(project_dir: str | Path, requirement_name: str):
        clear_path = Path(project_dir) / f"{sanitize_requirement_name(requirement_name)}_需求澄清.md"
        clear_path.write_text("需求澄清正文\n", encoding="utf-8")
        return type(
            "AnalysisResult",
            (),
            {"requirements_clear_path": str(clear_path.resolve()), "cleanup_paths": ()},
        )()

    def test_sanitize_requirement_name_replaces_invalid_characters(self):
        self.assertEqual(sanitize_requirement_name('交易 表/需求:*?"<>|'), "交易_表_需求")

    def test_build_output_path_uses_safe_requirement_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = build_output_path(tmpdir, "交易 表")
        self.assertEqual(output_path.name, "交易_表_原始需求.md")

    def test_collect_text_input_interactive_stops_on_eof(self):
        with patch("builtins.input", side_effect=["第一行", "第二行", "EOF"]):
            text = collect_text_input_interactive()
        self.assertEqual(text, "第一行\n第二行")

    def test_collect_request_interactive_prompts_for_missing_fields(self):
        parser = build_parser()
        args = parser.parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("builtins.input", side_effect=[tmpdir, "需求A", "1"]):
                request = collect_request(args)
        self.assertEqual(request.project_dir, str(Path(tmpdir).resolve()))
        self.assertEqual(request.requirement_name, "需求A")
        self.assertEqual(request.input_type, "text")

    def test_prompt_project_dir_requires_absolute_path_and_reuses_invalid_input(self):
        metadata_calls: list[dict[str, object]] = []

        @contextmanager
        def capture_prompt_metadata(**metadata):
            metadata_calls.append(metadata)
            yield

        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_mock = Mock(side_effect=["relative/path", tmpdir])
            with patch("T12_requirements_common.prompt_with_default", prompt_mock), patch(
                "T12_requirements_common.prompt_metadata",
                side_effect=capture_prompt_metadata,
            ), patch("T09_terminal_ops.message") as message_mock:
                result = prompt_project_dir()

        self.assertEqual(result, str(Path(tmpdir).resolve()))
        self.assertEqual(prompt_mock.call_args_list[0].args, ("输入项目工作目录", ""))
        self.assertEqual(prompt_mock.call_args_list[1].args, ("输入项目工作目录", "relative/path"))
        self.assertEqual(metadata_calls[1]["error_message"], "目录无效: 请输入绝对路径")
        message_mock.assert_any_call("目录无效: 请输入绝对路径")

    def test_collect_request_clears_existing_human_exchange_file_for_new_requirement(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ask_human_path = root / "需求A_与人类交流.md"
            ask_human_path.write_text("旧的 HITL 提问\n", encoding="utf-8")
            args = parser.parse_args(["--project-dir", tmpdir, "--requirement-name", "需求A", "--input-type", "text"])
            request = collect_request(args)
            self.assertEqual(request.requirement_name, "需求A")
            self.assertEqual(ask_human_path.read_text(encoding="utf-8"), "")

    def test_collect_request_clears_existing_human_exchange_file_when_reusing_existing_requirement(self):
        parser = build_parser()
        args = parser.parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            ask_human_path = root / "需求A_与人类交流.md"
            ask_human_path.write_text("旧的 HITL 提问\n", encoding="utf-8")
            with patch("builtins.input", side_effect=[tmpdir, "1"]):
                request = collect_request(args)
            self.assertEqual(request.requirement_name, "需求A")
            self.assertTrue(request.reuse_existing_original_requirement)
            self.assertEqual(ask_human_path.read_text(encoding="utf-8"), "")

    def test_collect_request_can_reuse_existing_requirement_from_args(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            args = parser.parse_args([
                "--project-dir",
                tmpdir,
                "--requirement-name",
                "需求A",
                "--reuse-existing-original-requirement",
            ])
            request = collect_request(args)

        self.assertEqual(request.requirement_name, "需求A")
        self.assertTrue(request.reuse_existing_original_requirement)
        self.assertEqual(request.input_type, "")
        self.assertEqual(request.input_value, "")

    def test_collect_request_can_back_from_input_type_to_requirement_name(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            args = parser.parse_args(["--project-dir", tmpdir])
            with patch("A02_RequirementIntake.prompt_requirement_name", side_effect=["需求A", "需求B"]) as name_prompt, patch(
                "A02_RequirementIntake.prompt_input_type",
                side_effect=[PromptBackRequested(), "text"],
            ):
                request = collect_request(args)
        self.assertEqual(request.requirement_name, "需求B")
        self.assertEqual(request.input_type, "text")
        self.assertEqual(name_prompt.call_count, 2)

    def test_collect_request_can_bubble_previous_stage_back_from_first_requirement_prompt(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            args = parser.parse_args(["--project-dir", tmpdir, "--allow-previous-stage-back"])
            with patch("A02_RequirementIntake.prompt_requirement_name", side_effect=PromptBackRequested()):
                with self.assertRaises(PromptBackRequested):
                    collect_request(args)

    def test_run_requirement_intake_stage_reuses_existing_requirement_from_args(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("正文A\n", encoding="utf-8")
            result = run_requirement_intake_stage([
                "--project-dir",
                tmpdir,
                "--requirement-name",
                "需求A",
                "--reuse-existing-original-requirement",
            ])

        self.assertEqual(result.requirement_name, "需求A")
        self.assertEqual(result.original_requirement_path, str(original_path.resolve()))

    def test_run_requirement_intake_stage_reprompts_after_overwrite_declined(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing_path = root / "需求A_原始需求.md"
            existing_path.write_text("旧内容\n", encoding="utf-8")
            with patch(
                "builtins.input",
                side_effect=[
                    "2",
                    "需求A",
                    "1",
                    "no",
                    "2",
                    "需求B",
                    "1",
                    "新内容",
                    "EOF",
                ],
            ), patch("sys.stdin", _TTYStringIO("")):
                result = run_requirement_intake_stage(["--project-dir", tmpdir])

            new_path = root / "需求B_原始需求.md"
            self.assertEqual(result.requirement_name, "需求B")
            self.assertEqual(existing_path.read_text(encoding="utf-8"), "旧内容\n")
            self.assertEqual(new_path.read_text(encoding="utf-8"), "新内容\n")

    def test_run_requirement_intake_stage_overwrite_prompt_allows_back_under_bridge_ui(self):
        from tmux_core.requirements_scope import CREATE_NEW_REQUIREMENT_SELECTION_VALUE
        from T09_terminal_ops import BridgePromptRequest, BridgeTerminalUI, PROMPT_BACK_VALUE, use_terminal_ui

        captured_requests: list[BridgePromptRequest] = []
        requirement_names = iter(["需求A", "需求B"])

        def emit_event(_event_type: str, _payload: dict[str, object]) -> None:
            return None

        def request_prompt(request: BridgePromptRequest) -> dict[str, object]:
            captured_requests.append(request)
            prompt_text = str(request.payload.get("prompt_text", ""))
            if request.prompt_type == "select" and prompt_text == "选择已有需求或创建新需求":
                return {"value": CREATE_NEW_REQUIREMENT_SELECTION_VALUE}
            if request.prompt_type == "select" and prompt_text == "选择输入方式":
                return {"value": "text"}
            if request.prompt_type == "select" and prompt_text.startswith("文件已存在，是否覆盖"):
                return {"value": PROMPT_BACK_VALUE}
            if request.prompt_type == "text" and prompt_text == "输入需求名称":
                return {"value": next(requirement_names)}
            if request.prompt_type == "multiline":
                return {"value": "新内容"}
            return {"value": str(request.payload.get("default_value", ""))}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("旧内容\n", encoding="utf-8")
            with use_terminal_ui(BridgeTerminalUI(emit_event=emit_event, request_prompt=request_prompt)):
                result = run_requirement_intake_stage(["--project-dir", tmpdir])

        overwrite_requests = [
            request
            for request in captured_requests
            if str(request.payload.get("prompt_text", "")).startswith("文件已存在，是否覆盖")
        ]
        self.assertEqual(result.requirement_name, "需求B")
        self.assertEqual(len(overwrite_requests), 1)
        self.assertTrue(overwrite_requests[0].payload["allow_back"])
        self.assertEqual(overwrite_requests[0].payload["back_value"], PROMPT_BACK_VALUE)
        self.assertEqual(overwrite_requests[0].payload["stage_step_index"], 5)

    def test_clarification_collect_request_clears_existing_human_exchange_file(self):
        from A03_RequirementsClarification import build_parser as build_clarification_parser, collect_request as collect_clarification_request

        parser = build_clarification_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ask_human_path = root / "需求A_与人类交流.md"
            ask_human_path.write_text("旧的 HITL 提问\n", encoding="utf-8")
            args = parser.parse_args(["--project-dir", tmpdir, "--requirement-name", "需求A"])
            project_dir, requirement_name = collect_clarification_request(args)
            self.assertEqual(project_dir, str(root.resolve()))
            self.assertEqual(requirement_name, "需求A")
            self.assertEqual(ask_human_path.read_text(encoding="utf-8"), "")

    def test_list_existing_requirements_returns_non_empty_original_requirement_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            (root / "需求B_原始需求.md").write_text("正文B\n", encoding="utf-8")
            (root / "空需求_原始需求.md").write_text("", encoding="utf-8")
            result = list_existing_requirements(root)
        self.assertEqual(result, ("需求A", "需求B"))

    def test_prompt_requirement_name_with_existing_can_select_existing_requirement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            (root / "需求B_原始需求.md").write_text("正文B\n", encoding="utf-8")
            with patch("builtins.input", side_effect=["2"]):
                result = prompt_requirement_name_with_existing(root)
        self.assertEqual(result, "需求B")

    def test_prompt_requirement_name_with_existing_can_create_new_requirement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            with patch("builtins.input", side_effect=["2", "新需求"]):
                result = prompt_requirement_name_with_existing(root)
        self.assertEqual(result, "新需求")

    def test_prompt_requirement_name_selection_marks_existing_requirement_for_reuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            with patch("builtins.input", side_effect=["1"]):
                result = prompt_requirement_name_selection(root)
        self.assertEqual(result.requirement_name, "需求A")
        self.assertTrue(result.reuse_existing_original_requirement)

    def test_collect_requirements_analysis_agent_selection_prompts_when_interactive(self):
        parser = build_parser()
        args = parser.parse_args([])
        with patch("A02_RequirementsAnalysis.stdin_is_interactive", return_value=True), patch(
            "builtins.input",
            side_effect=["1", "1", "3", ""],
        ):
            selection = collect_requirements_analysis_agent_selection(args)
        self.assertEqual(selection.vendor, DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR)
        self.assertEqual(selection.model, DEFAULT_REQUIREMENTS_ANALYSIS_MODEL)
        self.assertEqual(selection.reasoning_effort, DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT)
        self.assertEqual(selection.proxy_url, "")

    def test_collect_requirements_analysis_agent_selection_requires_args_when_noninteractive(self):
        parser = build_parser()
        args = parser.parse_args([])
        with patch("A02_RequirementsAnalysis.stdin_is_interactive", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "需求澄清阶段需要选择厂商"):
                collect_requirements_analysis_agent_selection(args)

    def test_collect_requirements_analysis_agent_selection_prompts_under_bridge_interactive_ui(self):
        parser = build_parser()
        args = parser.parse_args([])
        from T09_terminal_ops import BridgeTerminalUI, BridgePromptRequest, use_terminal_ui

        select_calls = 0

        def emit_event(_event_type: str, _payload: dict[str, object]) -> None:
            return None

        def request_prompt(request: BridgePromptRequest) -> dict[str, object]:
            nonlocal select_calls
            if request.prompt_type == "select":
                select_calls += 1
                return {"value": str(request.payload.get("default_value", ""))}
            return {"value": ""}

        with use_terminal_ui(BridgeTerminalUI(emit_event=emit_event, request_prompt=request_prompt)):
            selection = collect_requirements_analysis_agent_selection(args)
        self.assertEqual(selection.vendor, DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR)
        self.assertEqual(selection.model, DEFAULT_REQUIREMENTS_ANALYSIS_MODEL)
        self.assertEqual(selection.reasoning_effort, DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT)
        self.assertEqual(selection.proxy_url, "")
        self.assertEqual(select_calls, 3)

    def test_collect_requirements_analysis_agent_selection_can_bubble_previous_stage_back_from_first_prompt(self):
        args = type(
            "Args",
            (),
            {
                "vendor": "",
                "model": "",
                "effort": "",
                "proxy_url": "",
                "allow_previous_stage_back": True,
            },
        )()
        with patch("A02_RequirementsAnalysis.stdin_is_interactive", return_value=True), patch(
            "A02_RequirementsAnalysis.prompt_vendor",
            side_effect=PromptBackRequested(),
        ):
            with self.assertRaises(PromptBackRequested):
                collect_requirements_analysis_agent_selection(args)

    def test_clarification_reuse_prompt_allows_previous_stage_back_under_bridge_ui(self):
        from A03_RequirementsClarification import run_requirements_clarification_stage
        from T09_terminal_ops import BridgePromptRequest, BridgeTerminalUI, use_terminal_ui

        captured_requests: list[BridgePromptRequest] = []

        def emit_event(_event_type: str, _payload: dict[str, object]) -> None:
            return None

        def request_prompt(request: BridgePromptRequest) -> dict[str, object]:
            captured_requests.append(request)
            return {"value": "__tmux_back__"}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("原始需求\n", encoding="utf-8")
            (root / "需求A_需求澄清.md").write_text("已有需求澄清\n", encoding="utf-8")
            with use_terminal_ui(BridgeTerminalUI(emit_event=emit_event, request_prompt=request_prompt)):
                with self.assertRaises(PromptBackRequested):
                    run_requirements_clarification_stage(
                        [
                            "--project-dir",
                            tmpdir,
                            "--requirement-name",
                            "需求A",
                            "--allow-previous-stage-back",
                        ]
                    )

        self.assertEqual(captured_requests[0].prompt_type, "select")
        self.assertTrue(captured_requests[0].payload["allow_back"])
        self.assertEqual(captured_requests[0].payload["prompt_text"], "是否直接复用已有的需求澄清并跳入需求评审阶段")

    def test_stdin_is_interactive_reflects_stdin_capability(self):
        with patch("sys.stdin", _TTYStringIO("")):
            self.assertTrue(stdin_is_interactive())
        with patch("sys.stdin", io.StringIO("")):
            self.assertFalse(stdin_is_interactive())

    def test_extract_text_from_docx_reads_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = Path(tmpdir) / "sample.docx"
            _make_simple_docx(docx_path, ["第一段", "第二段"])
            text = extract_text_from_docx(docx_path)
        self.assertEqual(text, "第一段\n第二段")

    def test_extract_text_from_pdf_reads_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "sample.pdf"
            pdf_path.write_bytes(_make_simple_pdf("Hello PDF"))
            text = extract_text_from_pdf(pdf_path)
        self.assertIn("Hello PDF", text)

    def test_extract_text_from_local_file_supports_markdown_pdf_and_docx(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            md_path = root / "sample.md"
            pdf_path = root / "sample.pdf"
            docx_path = root / "sample.docx"
            md_path.write_text("# 标题", encoding="utf-8")
            pdf_path.write_bytes(_make_simple_pdf("PDF TEXT"))
            _make_simple_docx(docx_path, ["DOCX 文本"])
            self.assertEqual(extract_text_from_local_file(root, "sample.md"), "# 标题")
            self.assertIn("PDF TEXT", extract_text_from_local_file(root, "sample.pdf"))
            self.assertEqual(extract_text_from_local_file(root, "sample.docx"), "DOCX 文本")

    def test_get_notion_requirement_prompt_mentions_output_files_only(self):
        prompt = get_notion_requirement(
            "https://www.notion.so/demo",
            original_requirement_md="/tmp/需求A_原始需求.md",
            ask_human_md="/tmp/需求A_与人类交流.md",
        )
        self.assertIn("$notion-api-token-ops", prompt)
        self.assertIn("/tmp/需求A_原始需求.md", prompt)
        self.assertIn("/tmp/需求A_与人类交流.md", prompt)
        self.assertNotIn("/tmp/notion_status.json", prompt)
        self.assertNotIn("/tmp/turns/notion_requirement_1/turn_status.json", prompt)

    def test_build_notion_followup_prompt_mentions_human_message_and_record(self):
        prompt = build_notion_followup_prompt(
            "请把子页面也纳入范围",
            "https://www.notion.so/demo",
            original_requirement_md="/tmp/需求A_原始需求.md",
            ask_human_md="/tmp/需求A_与人类交流.md",
            hitl_record_md="/tmp/需求A_需求录入_HITL记录.md",
        )
        self.assertIn("请把子页面也纳入范围", prompt)
        self.assertIn("/tmp/需求A_需求录入_HITL记录.md", prompt)
        self.assertNotIn("/tmp/notion_status.json", prompt)

    def test_build_notion_followup_prompt_updates_record_and_output_files(self):
        prompt = build_notion_followup_prompt(
            "继续读取相关子页面",
            "https://www.notion.so/demo",
            original_requirement_md="/tmp/需求A_原始需求.md",
            ask_human_md="/tmp/需求A_与人类交流.md",
            hitl_record_md="/tmp/需求A_需求录入_HITL记录.md",
        )
        self.assertIn("继续读取相关子页面", prompt)
        self.assertIn("/tmp/需求A_需求录入_HITL记录.md", prompt)
        self.assertIn("/tmp/需求A_原始需求.md", prompt)

    def test_requirements_understand_prompt_mentions_hitl_files_without_runtime_params(self):
        prompt = requirements_understand(
            fintech_ba,
            original_requirement_md="/tmp/需求A_原始需求.md",
            requirements_clear_md="/tmp/需求A_需求澄清.md",
            ask_human_md="/tmp/需求A_与人类交流.md",
            hitl_record_md="/tmp/需求A人机交互澄清记录.md",
        )
        self.assertIn("/tmp/需求A_需求澄清.md", prompt)
        self.assertIn("/tmp/需求A_与人类交流.md", prompt)
        self.assertIn("/tmp/需求A人机交互澄清记录.md", prompt)
        self.assertIn("禁止修改除了《/tmp/需求A_需求澄清.md》/《/tmp/需求A_与人类交流.md》/《/tmp/需求A人机交互澄清记录.md》之外的文档", prompt)
        self.assertNotIn("/tmp/requirements_analysis_status.json", prompt)
        self.assertNotIn("/tmp/turns/requirements_analysis_1/turn_status.json", prompt)

    def test_hitl_bck_prompt_mentions_record_files_without_runtime_params(self):
        prompt = hitl_bck(
            "边界以原始需求第 4 条为准",
            original_requirement_md="/tmp/需求A_原始需求.md",
            hitl_record_md="/tmp/需求A人机交互澄清记录.md",
            requirements_clear_md="/tmp/需求A_需求澄清.md",
            ask_human_md="/tmp/需求A_与人类交流.md",
        )
        self.assertIn("边界以原始需求第 4 条为准", prompt)
        self.assertIn("/tmp/需求A人机交互澄清记录.md", prompt)
        self.assertNotIn("/tmp/requirements_analysis_status.json", prompt)

    def test_validate_notion_status_accepts_completed_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "需求A_原始需求.md"
            stage_status_path = root / "notion_status.json"
            output_path.write_text("正文", encoding="utf-8")
            _write_stage_status(
                stage_status_path,
                turn_id="read_notion_requirement_1",
                hitl_round=1,
                status=NOTION_STATUS_OK,
                output_path=output_path,
                question_path=None,
                record_path=None,
                summary="done",
            )
            payload = validate_notion_status(
                stage_status_path,
                turn_id="read_notion_requirement_1",
                hitl_round=1,
                output_path=output_path,
                question_path=root / "unused_question.md",
                record_path=root / "unused_record.md",
            )
        self.assertEqual(payload.status, NOTION_STATUS_OK)

    def test_validate_notion_status_accepts_hitl_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            question_path = root / "问题.md"
            record_path = root / "记录.md"
            stage_status_path = root / "notion_status.json"
            question_path.write_text("- [阻断] 需要确认边界\n", encoding="utf-8")
            record_path.write_text("- [待确认] 子页面范围\n", encoding="utf-8")
            _write_stage_status(
                stage_status_path,
                turn_id="read_notion_requirement_1",
                hitl_round=1,
                status=NOTION_STATUS_HITL,
                output_path=None,
                question_path=question_path,
                record_path=record_path,
                summary="need hitl",
            )
            payload = validate_notion_status(
                stage_status_path,
                turn_id="read_notion_requirement_1",
                hitl_round=1,
                output_path=root / "需求A_原始需求.md",
                question_path=question_path,
                record_path=record_path,
            )
        self.assertEqual(payload.status, NOTION_STATUS_HITL)

    def test_format_notion_failure_message_includes_next_step_and_verification(self):
        message = format_notion_failure_message(
            {
                "error": "No token found",
                "next_step": "配置 NOTION_TOKEN",
                "verification_command": "bash notion_api_token_run.sh health",
            }
        )
        self.assertIn("No token found", message)
        self.assertIn("配置 NOTION_TOKEN", message)
        self.assertIn("health", message)

    def test_build_notion_retry_message_uses_question_file_when_output_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "需求A_原始需求.md"
            question_path = root / "需求A_与人类交流.md"
            output_path.write_text("", encoding="utf-8")
            question_path.write_text("- 请确认 Notion 页面权限\n", encoding="utf-8")
            message = build_notion_retry_message(question_path, output_path=output_path)
        self.assertIn("Notion 需求录入失败", message)
        self.assertIn("请确认 Notion 页面权限", message)

    def test_build_notion_retry_message_returns_empty_when_output_already_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "需求A_原始需求.md"
            question_path = root / "需求A_与人类交流.md"
            output_path.write_text("已获取正文\n", encoding="utf-8")
            question_path.write_text("- 请确认 Notion 页面权限\n", encoding="utf-8")
            message = build_notion_retry_message(question_path, output_path=output_path)
        self.assertEqual(message, "")

    def test_render_notion_tmux_start_summary_includes_attach_command(self):
        class _Worker:
            runtime_dir = "/tmp/runtime"
            session_name = "agreq-codex-demo"

        summary = render_notion_tmux_start_summary(_Worker())
        self.assertIn("Notion 临时智能体已启动", summary)
        self.assertIn("tmux attach -t agreq-codex-demo", summary)

    def test_render_notion_progress_line_contains_status_phase_and_note(self):
        class _Worker:
            def read_state(self):
                return {
                    "status": "running",
                    "agent_state": "BUSY",
                    "health_status": "alive",
                    "note": "turn:read_notion_requirement",
                    "workflow_stage": "requirements_notion_read",
                }

        text = render_notion_progress_line(worker=_Worker(), requirement_name="需求A", tick=7)
        self.assertIn("⠧ Notion需求录入中", text)
        self.assertIn("需求A:running/BUSY", text)
        self.assertIn("health=alive", text)
        self.assertIn("turn:read_notion_requirement", text)

    def test_render_agent_boot_progress_line_contains_boot_message(self):
        text = render_agent_boot_progress_line(tick=6)
        self.assertIn("⠦ 智能体启动中...", text)

    def test_cleanup_notion_runtime_paths_removes_runtime_dir_and_empty_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / ".requirements_analysis_runtime"
            runtime_dir = root / "worker-demo"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "notion_status.json").write_text("{}", encoding="utf-8")
            removed = cleanup_notion_runtime_paths(runtime_dir, root)
        self.assertIn(str(runtime_dir.resolve()), removed)
        self.assertIn(str(root.resolve()), removed)

    def test_build_notion_hitl_paths_uses_project_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path, question_path, record_path = build_notion_hitl_paths(tmpdir, "需求 A")
        self.assertEqual(output_path.name, "需求_A_原始需求.md")
        self.assertEqual(question_path.name, "需求_A_需求录入_HITL问题.md")
        self.assertEqual(record_path.name, "需求_A_需求录入_HITL记录.md")

    def test_build_requirements_analysis_paths_uses_expected_filenames(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_path, clear_path, ask_path, record_path = build_requirements_analysis_paths(tmpdir, "需求 A")
        self.assertEqual(original_path.name, "需求_A_原始需求.md")
        self.assertEqual(clear_path.name, "需求_A_需求澄清.md")
        self.assertEqual(ask_path.name, "需求_A_与人类交流.md")
        self.assertEqual(record_path.name, "需求_A_人机交互澄清记录.md")

    def test_ensure_requirements_hitl_record_file_migrates_legacy_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_path = Path(tmpdir) / "需求A人机交互澄清记录.md"
            legacy_path.write_text("旧记录\n", encoding="utf-8")
            record_path = ensure_requirements_hitl_record_file(tmpdir, "需求A")
            self.assertEqual(record_path.name, "需求A_人机交互澄清记录.md")
            self.assertFalse(legacy_path.exists())
            self.assertEqual(record_path.read_text(encoding="utf-8"), "旧记录\n")

    def test_render_requirements_analysis_tmux_start_summary_includes_attach_command(self):
        class _Worker:
            runtime_dir = "/tmp/runtime"
            session_name = "agreq-analysis-demo"

        summary = render_requirements_analysis_tmux_start_summary(_Worker())
        self.assertIn("需求澄清智能体已启动", summary)
        self.assertIn("tmux attach -t agreq-analysis-demo", summary)

    def test_render_requirements_analysis_progress_line_contains_status_phase_and_note(self):
        class _Worker:
            def read_state(self):
                return {
                    "status": "running",
                    "agent_state": "BUSY",
                    "health_status": "alive",
                    "note": "turn:requirements_analysis_round_1",
                    "workflow_stage": "requirements_analysis",
                }

        text = render_requirements_analysis_progress_line(worker=_Worker(), requirement_name="需求A", tick=7)
        self.assertIn("⠧ 需求澄清中", text)
        self.assertIn("需求A:running/BUSY", text)
        self.assertIn("health=alive", text)
        self.assertIn("turn:requirements_analysis_round_1", text)

    def test_main_writes_requirement_file_from_stdin_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            stdin = io.StringIO("原始需求正文\n")
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ) as mocked_analysis:
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "text",
                        "--vendor",
                        "codex",
                        "--model",
                        "gpt-5.4",
                        "--effort",
                        "high",
                    ]
                )
            output_path = Path(tmpdir) / "需求A_原始需求.md"
            self.assertEqual(exit_code, 0)
            self.assertTrue(output_path.exists())
            self.assertEqual(output_path.read_text(encoding="utf-8"), "原始需求正文\n")
            self.assertIn("需求录入完成", stdout.getvalue())
            self.assertIn("进入需求澄清阶段", stdout.getvalue())
            self.assertIn("需求澄清完成", stdout.getvalue())
            mocked_analysis.assert_called_once_with(
                str(Path(tmpdir).resolve()),
                "需求A",
                vendor="codex",
                model="gpt-5.4",
                reasoning_effort="high",
                proxy_url="",
                resume_existing=False,
                preserve_ba_worker=False,
            )

    def test_main_requires_overwrite_flag_in_parameter_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "需求A_原始需求.md"
            output_path.write_text("旧内容\n", encoding="utf-8")
            stdout = io.StringIO()
            stdin = io.StringIO("新内容\n")
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "text",
                        "--vendor",
                        "codex",
                        "--model",
                        "gpt-5.4",
                        "--effort",
                        "high",
                        "--yes",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "旧内容\n")
            self.assertIn("未指定 --overwrite", stdout.getvalue())

    def test_main_requires_analysis_selection_in_noninteractive_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            stdin = io.StringIO("原始需求正文\n")
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ) as mocked_analysis:
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "text",
                    ]
                )
            output_path = Path(tmpdir) / "需求A_原始需求.md"
            self.assertEqual(exit_code, 1)
            self.assertTrue(output_path.exists())
            self.assertIn("需求录入完成", stdout.getvalue())
            self.assertIn("需求澄清阶段需要选择厂商", stdout.getvalue())
            mocked_analysis.assert_not_called()

    def test_main_marks_pre_development_requirement_intake_as_true_after_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            stdin = io.StringIO("原始需求正文\n")
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "text",
                        "--vendor",
                        "codex",
                        "--model",
                        "gpt-5.4",
                        "--effort",
                        "high",
                    ]
                )

            self.assertEqual(exit_code, 0)
            record_path = Path(tmpdir) / "需求A_开发前期.json"
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["需求录入"]["需求录入"])
            self.assertTrue(payload["需求澄清"]["需求澄清"])

    def test_collect_request_reuses_existing_requirement_without_prompting_input_type(self):
        parser = build_parser()
        args = parser.parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            with patch("builtins.input", side_effect=[tmpdir, "1"]):
                request = collect_request(args)
        self.assertEqual(request.requirement_name, "需求A")
        self.assertTrue(request.reuse_existing_original_requirement)
        self.assertEqual(request.input_type, "")

    def test_main_reuses_existing_requirement_and_existing_clarification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("原始需求正文\n", encoding="utf-8")
            (root / "需求A_需求澄清.md").write_text("已有需求澄清\n", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "builtins.input",
                side_effect=[tmpdir, "1", "yes"],
            ), patch("sys.stdout", stdout), patch("sys.stdin", _TTYStringIO("")), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
            ) as mocked_analysis:
                exit_code = main([])

            self.assertEqual(exit_code, 0)
            payload = json.loads((root / "需求A_开发前期.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["需求录入"]["需求录入"])
            self.assertTrue(payload["需求澄清"]["需求澄清"])
            self.assertTrue((root / "需求A_人机交互澄清记录.md").exists())
            self.assertIn("复用已有原始需求", stdout.getvalue())
            self.assertIn("复用已有的需求澄清，直接进入需求评审阶段", stdout.getvalue())
            mocked_analysis.assert_not_called()

    def test_main_resumes_existing_clarification_when_user_selects_no(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("原始需求正文\n", encoding="utf-8")
            (root / "需求A_需求澄清.md").write_text("已有需求澄清\n", encoding="utf-8")
            stdout = io.StringIO()

            def _resume_stub(*args, **kwargs):
                ensure_requirements_hitl_record_file(tmpdir, "需求A")
                return self._stub_analysis_result(tmpdir, "需求A")

            with patch(
                "builtins.input",
                side_effect=[tmpdir, "1", "no", "1", "1", "3", ""],
            ), patch("sys.stdout", stdout), patch("sys.stdin", _TTYStringIO("")), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=_resume_stub,
            ) as mocked_analysis:
                exit_code = main([])

            self.assertEqual(exit_code, 0)
            self.assertIn("不直接复用已有需求澄清，将启动需求分析师基于现有澄清继续核验", stdout.getvalue())
            mocked_analysis.assert_called_once_with(
                str(Path(tmpdir).resolve()),
                "需求A",
                vendor="codex",
                model="gpt-5.4",
                reasoning_effort="high",
                proxy_url="",
                resume_existing=True,
                preserve_ba_worker=False,
            )

    def test_main_missing_clarification_prompts_to_launch_requirements_analyst(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("原始需求正文\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch(
                "builtins.input",
                side_effect=[tmpdir, "1", "1", "1", "3", ""],
            ), patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch("sys.stdin", _TTYStringIO("")), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ) as mocked_analysis:
                exit_code = main([])

            self.assertEqual(exit_code, 0)
            self.assertIn(
                "执行摘要: 未检测到可复用的需求澄清，需要启动需求分析师智能体执行需求澄清；请为需求分析师选择厂商、模型、推理强度、代理端口。",
                stdout.getvalue(),
            )
            self.assertEqual(stderr.getvalue().count("警告：文件不存在"), 1)
            mocked_analysis.assert_called_once_with(
                str(Path(tmpdir).resolve()),
                "需求A",
                vendor="codex",
                model="gpt-5.4",
                reasoning_effort="high",
                proxy_url="",
                resume_existing=False,
                preserve_ba_worker=False,
            )

    def test_main_reprompts_input_source_when_file_content_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            empty_file = root / "空文件.md"
            empty_file.write_text("", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "builtins.input",
                side_effect=["1", "重新录入的正文", "EOF", "1", "1", "3", ""],
            ), patch("sys.stdout", stdout), patch("sys.stdin", _TTYStringIO("")), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "file",
                        "--input-value",
                        str(empty_file),
                    ]
                )

            self.assertEqual(exit_code, 0)
            output_path = root / "需求A_原始需求.md"
            self.assertEqual(output_path.read_text(encoding="utf-8"), "重新录入的正文\n")
            self.assertIn("原始需求内容为空，未生成输出文件", stdout.getvalue())
            self.assertIn("请重新选择需求录入方式", stdout.getvalue())

    def test_main_interactive_overwrite_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "需求A_原始需求.md"
            output_path.write_text("旧内容\n", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "builtins.input",
                side_effect=[tmpdir, "2", "需求A", "1", "yes", "新内容", "EOF", "1", "1", "3", ""],
            ), patch("sys.stdout", stdout), patch("sys.stdin", _TTYStringIO("")), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ) as mocked_analysis:
                exit_code = main([])
            self.assertEqual(exit_code, 0)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "新内容\n")
            self.assertIn("需求录入完成", stdout.getvalue())
            self.assertIn("进入需求澄清阶段", stdout.getvalue())
            mocked_analysis.assert_called_once_with(
                str(Path(tmpdir).resolve()),
                "需求A",
                vendor="codex",
                model="gpt-5.4",
                reasoning_effort="high",
                proxy_url="",
                resume_existing=False,
                preserve_ba_worker=False,
            )

    def test_main_reads_notion_via_tmux_status_files_and_prints_tmux_info(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            output_path, question_path, record_path = build_notion_hitl_paths(tmpdir, "需求A")
            runtime_root = Path(tmpdir) / NOTION_RUNTIME_ROOT_NAME
            test_case = self
            monitor_events: list[str] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    monitor_events.append(f"init:{interval_sec}")

                def start(self):
                    monitor_events.append("start")

                def stop(self):
                    monitor_events.append("stop")

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.worker_id = worker_id
                    self.work_dir = Path(work_dir)
                    self.config = config
                    test_case.assertEqual(config.vendor.value, "codex")
                    test_case.assertEqual(config.model, DEFAULT_NOTION_MODEL)
                    test_case.assertEqual(config.reasoning_effort, DEFAULT_NOTION_EFFORT)
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / "requirements-notion-reader-demo"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = "agreq-codex-demo"
                    self.round = 0
                    self.killed = False

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    self.round += 1
                    output_path.write_text("Notion 原始正文", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    self.killed = True
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ), patch("sys.stdout", stdout):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "notion",
                        "--input-value",
                        "https://www.notion.so/demo",
                        "--vendor",
                        "codex",
                        "--model",
                        "gpt-5.4",
                        "--effort",
                        "high",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "Notion 原始正文\n")
            self.assertIn("Notion 临时智能体已启动", stdout.getvalue())
            self.assertIn("tmux attach -t agreq-codex-demo", stdout.getvalue())
            self.assertIn("需求录入完成", stdout.getvalue())
            self.assertEqual(monitor_events, ["init:0.2", "init:0.2", "start", "stop", "start", "stop"])
            self.assertFalse(question_path.exists())
            self.assertFalse(record_path.exists())
            self.assertFalse(runtime_root.exists())

    def test_main_runs_notion_hitl_then_completes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            output_path, question_path, record_path = build_notion_hitl_paths(tmpdir, "需求A")
            runtime_root = Path(tmpdir) / NOTION_RUNTIME_ROOT_NAME
            human_inputs = ["请把 Time Frequency Extension 子页面也纳入范围", "EOF"]
            monitor_events: list[str] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    monitor_events.append(f"init:{interval_sec}")

                def start(self):
                    monitor_events.append("start")

                def stop(self):
                    monitor_events.append("stop")

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / "requirements-notion-reader-demo"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = "agreq-codex-demo"
                    self.round = 0

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    self.round += 1
                    if self.round == 1:
                        question_path.write_text("- [阻断：边界歧义] 是否纳入子页面\n", encoding="utf-8")
                        record_path.write_text("- [待确认] 子页面范围\n", encoding="utf-8")
                    else:
                        self.testcase.assertIn("Time Frequency Extension", prompt)
                        output_path.write_text("Notion 原始正文\n包含子页面", encoding="utf-8")
                        record_path.write_text("- [已确认] 纳入子页面\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    return self.session_name

            FakeTmuxWorker.testcase = self

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch(
                "builtins.input", side_effect=human_inputs
            ), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ), patch("sys.stdout", stdout):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "notion",
                        "--input-value",
                        "https://www.notion.so/demo",
                        "--vendor",
                        "codex",
                        "--model",
                        "gpt-5.4",
                        "--effort",
                        "high",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "Notion 原始正文\n包含子页面\n")
            self.assertIn("HITL 第 1 轮", stdout.getvalue())
            self.assertEqual(monitor_events, ["init:0.2", "init:0.2", "start", "stop", "start", "stop", "start", "stop"])
            self.assertFalse(question_path.exists())
            self.assertFalse(record_path.exists())
            self.assertFalse(runtime_root.exists())

    def test_main_reports_notion_health_failure_with_guidance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            runtime_root = Path(tmpdir) / NOTION_RUNTIME_ROOT_NAME

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / "requirements-notion-reader-demo"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = "agreq-codex-demo"

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    stage_status_path = self.runtime_dir / "notion_status.json"
                    _write_stage_status(
                        stage_status_path,
                        turn_id=completion_contract.turn_id,
                        hitl_round=1,
                        status=NOTION_STATUS_ERROR,
                        output_path=None,
                        question_path=None,
                        record_path=None,
                        summary="No token found",
                        extra={
                            "error": "No token found",
                            "next_step": "配置 NOTION_TOKEN",
                            "verification_command": "bash notion_api_token_run.sh health",
                        },
                    )
                    _write_turn_status(
                        completion_contract.status_path,
                        turn_id=completion_contract.turn_id,
                        stage_status_path=stage_status_path,
                        artifact_paths=[],
                    )
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "sys.stdout", stdout
            ), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "notion",
                        "--input-value",
                        "https://www.notion.so/demo",
                        "--vendor",
                        "codex",
                        "--model",
                        "gpt-5.4",
                        "--effort",
                        "high",
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn("Notion 读取失败", stdout.getvalue())
            self.assertIn("配置 NOTION_TOKEN", stdout.getvalue())
            self.assertTrue(runtime_root.exists())

    def test_main_reprompts_for_input_type_after_notion_failure_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            output_path, question_path, record_path = build_notion_hitl_paths(tmpdir, "需求A")
            runtime_root = Path(tmpdir) / NOTION_RUNTIME_ROOT_NAME

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / "requirements-notion-reader-demo"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = "agreq-codex-demo"

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    question_path.write_text("- 请先为该 Notion 页面授权读取权限\n", encoding="utf-8")
                    output_path.write_text("", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.run_requirements_analysis",
                side_effect=lambda *args, **kwargs: self._stub_analysis_result(tmpdir, "需求A"),
            ), patch("builtins.input", side_effect=["1", "重新录入的正文", "EOF", ""]), patch(
                "sys.stdin", _TTYStringIO("")
            ), patch("sys.stdout", stdout):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--input-type",
                        "notion",
                        "--input-value",
                        "https://www.notion.so/demo",
                        "--vendor",
                        "codex",
                        "--model",
                        "gpt-5.4",
                        "--effort",
                        "high",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(output_path.read_text(encoding="utf-8"), "重新录入的正文\n")
            self.assertIn("请先为该 Notion 页面授权读取权限", stdout.getvalue())
            self.assertIn("请重新选择需求录入方式", stdout.getvalue())

    def test_run_requirements_analysis_runs_hitl_until_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("原始需求正文\n", encoding="utf-8")
            _, clear_path, ask_path, record_path = build_requirements_analysis_paths(root, "需求A")
            runtime_root = root / NOTION_RUNTIME_ROOT_NAME
            stdout = io.StringIO()
            human_inputs = ["补充规则：不处理第4条", "EOF"]
            monitor_events: list[str] = []
            test_case = self

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    monitor_events.append(f"init:{interval_sec}")

                def start(self):
                    monitor_events.append("start")

                def stop(self):
                    monitor_events.append("stop")

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.worker_id = worker_id
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / "requirements-analyst-demo"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = "agreq-analysis-demo"
                    self.round = 0
                    test_case.assertEqual(config.model, DEFAULT_REQUIREMENTS_ANALYSIS_MODEL)
                    test_case.assertEqual(config.reasoning_effort, DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT)

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    self.round += 1
                    if self.round == 1:
                        test_case.assertIn(str(original_path.resolve()), prompt)
                        ask_path.write_text("- 发现的问题：第4条范围不清晰\n", encoding="utf-8")
                        record_path.write_text("- [待确认] 第4条范围\n", encoding="utf-8")
                    else:
                        test_case.assertIn("补充规则：不处理第4条", prompt)
                        clear_path.write_text("需求澄清正文\n", encoding="utf-8")
                        record_path.write_text("- [已确认] 不处理第4条\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch("builtins.input", side_effect=human_inputs), patch("sys.stdout", stdout):
                result = run_requirements_analysis(root, "需求A")

            self.assertEqual(result.requirements_clear_path, str(clear_path.resolve()))
            self.assertEqual(monitor_events, ["init:0.2", "init:0.2", "start", "stop", "start", "stop", "start", "stop"])
            self.assertTrue(clear_path.exists())
            self.assertTrue(record_path.exists())
            self.assertTrue(ask_path.exists())
            self.assertIn("需求澄清智能体已启动", stdout.getvalue())

    def test_run_requirements_analysis_resume_uses_resume_prompt_then_hitl_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("原始需求正文\n", encoding="utf-8")
            _, clear_path, ask_path, record_path = build_requirements_analysis_paths(root, "需求A")
            clear_path.write_text("已有需求澄清正文\n", encoding="utf-8")
            runtime_root = root / NOTION_RUNTIME_ROOT_NAME
            stdout = io.StringIO()
            human_inputs = ["补充限制：只处理工作日", "EOF"]
            observed_prompts: list[str] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    return None

                def start(self):
                    return None

                def stop(self):
                    return None

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / "requirements-analyst-demo"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = "agreq-analysis-demo"
                    self.round = 0

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    self.round += 1
                    observed_prompts.append(prompt)
                    if self.round == 1:
                        ask_path.write_text("- 请补充工作日限制\n", encoding="utf-8")
                        record_path.write_text("- [待确认] 工作日限制\n", encoding="utf-8")
                    else:
                        clear_path.write_text("更新后的需求澄清正文\n", encoding="utf-8")
                        record_path.write_text("- [已确认] 仅处理工作日\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch("builtins.input", side_effect=human_inputs), patch("sys.stdout", stdout):
                result = run_requirements_analysis(root, "需求A", resume_existing=True)

            self.assertEqual(result.requirements_clear_path, str(clear_path.resolve()))
            expected_resume_prompt = resume_requirements_understand(
                fintech_ba,
                original_requirement_md=str(original_path.resolve()),
                requirements_clear_md=str(clear_path.resolve()),
                ask_human_md=str(ask_path.resolve()),
                hitl_record_md=str(record_path.resolve()),
            )
            self.assertEqual(observed_prompts[0], expected_resume_prompt)
            self.assertIn("补充限制：只处理工作日", observed_prompts[1])

    def test_run_requirements_analysis_preserves_live_ba_worker_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("原始需求正文\n", encoding="utf-8")
            _, clear_path, ask_path, record_path = build_requirements_analysis_paths(root, "需求A")
            created_workers: list[object] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    return None

                def start(self):
                    return None

                def stop(self):
                    return None

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / "requirements-analyst-demo"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = "agreq-analysis-demo"
                    self.config = config
                    self.killed = False
                    created_workers.append(self)

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    clear_path.write_text("需求澄清正文\n", encoding="utf-8")
                    record_path.write_text("- [已确认] 完整\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    self.killed = True
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch("sys.stdout", io.StringIO()):
                result = run_requirements_analysis(root, "需求A", preserve_ba_worker=True)

            self.assertIsNotNone(result.ba_handoff)
            self.assertEqual(result.ba_handoff.worker, created_workers[0])
            self.assertFalse(created_workers[0].killed)
            self.assertEqual(result.ba_handoff.vendor, DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR)
            self.assertEqual(result.ba_handoff.model, DEFAULT_REQUIREMENTS_ANALYSIS_MODEL)
            self.assertEqual(result.cleanup_paths, (str(ask_path.resolve()),))

    def test_run_requirements_stage_direct_reuse_existing_clarification_does_not_create_ba_handoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("原始需求正文\n", encoding="utf-8")
            (root / "需求A_需求澄清.md").write_text("已有需求澄清\n", encoding="utf-8")
            stdout = io.StringIO()

            with patch(
                "builtins.input",
                side_effect=["1", "yes"],
            ), patch("sys.stdin", _TTYStringIO("")), patch("sys.stdout", stdout):
                result = run_requirements_stage(["--project-dir", tmpdir], preserve_ba_worker=True)

            self.assertEqual(result.requirement_name, "需求A")
            self.assertIsNone(result.ba_handoff)
            self.assertEqual(result.cleanup_paths, ())
            self.assertIn("复用已有的需求澄清，直接进入需求评审阶段", stdout.getvalue())

    def test_run_requirements_analysis_recreates_dead_ba_when_user_confirms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("原始需求正文\n", encoding="utf-8")
            _, clear_path, ask_path, record_path = build_requirements_analysis_paths(root, "需求A")
            created_workers: list[object] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    return None

                def start(self):
                    return None

                def stop(self):
                    return None

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / f"requirements-analyst-demo-{len(created_workers)}"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = f"agreq-analysis-demo-{len(created_workers)}"
                    self.config = config
                    self.killed = False
                    self.turns = 0
                    created_workers.append(self)

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    self.turns += 1
                    if len(created_workers) == 1:
                        raise RuntimeError("tmux pane died while waiting for turn artifacts")
                    clear_path.write_text("恢复后的需求澄清正文\n", encoding="utf-8")
                    record_path.write_text("- [已确认] 恢复完成\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    self.killed = True
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch(
                "A02_RequirementsAnalysis.prompt_yes_no",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.stdin_is_interactive",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.prompt_vendor",
                return_value="codex",
            ), patch(
                "A02_RequirementsAnalysis.prompt_model",
                return_value="gpt-5.4-mini",
            ), patch(
                "A02_RequirementsAnalysis.prompt_effort",
                return_value="high",
            ), patch(
                "A02_RequirementsAnalysis.prompt_proxy_url",
                return_value="",
            ), patch("sys.stdout", io.StringIO()):
                result = run_requirements_analysis(root, "需求A")

            self.assertEqual(result.requirements_clear_path, str(clear_path.resolve()))
            self.assertEqual(len(created_workers), 2)

    def test_run_requirements_analysis_recreates_dead_ba_when_run_turn_returns_dead_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("原始需求正文\n", encoding="utf-8")
            _, clear_path, ask_path, record_path = build_requirements_analysis_paths(root, "需求A")
            created_workers: list[object] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    return None

                def start(self):
                    return None

                def stop(self):
                    return None

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / f"requirements-analyst-demo-{len(created_workers)}"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = f"agreq-analysis-demo-{len(created_workers)}"
                    self.config = config
                    self.killed = False
                    self.turns = 0
                    created_workers.append(self)

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    self.turns += 1
                    if len(created_workers) == 1:
                        return type(
                            "CommandResult",
                            (),
                            {
                                "ok": False,
                                "clean_output": "tmux pane died while waiting for turn artifacts",
                                "exit_code": 1,
                            },
                        )()
                    clear_path.write_text("恢复后的需求澄清正文\n", encoding="utf-8")
                    record_path.write_text("- [已确认] 恢复完成\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    self.killed = True
                    return self.session_name

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch(
                "A02_RequirementsAnalysis.prompt_yes_no",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.stdin_is_interactive",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.prompt_vendor",
                return_value="codex",
            ), patch(
                "A02_RequirementsAnalysis.prompt_model",
                return_value="gpt-5.4-mini",
            ), patch(
                "A02_RequirementsAnalysis.prompt_effort",
                return_value="high",
            ), patch(
                "A02_RequirementsAnalysis.prompt_proxy_url",
                return_value="",
            ), patch("sys.stdout", io.StringIO()):
                result = run_requirements_analysis(root, "需求A")

            self.assertEqual(result.requirements_clear_path, str(clear_path.resolve()))
            self.assertEqual(len(created_workers), 2)
            self.assertTrue(created_workers[0].killed)

    def test_run_requirements_analysis_recreates_auth_failed_ba_with_new_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("原始需求正文\n", encoding="utf-8")
            _, clear_path, _, record_path = build_requirements_analysis_paths(root, "需求A")
            created_workers: list[object] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    return None

                def start(self):
                    return None

                def stop(self):
                    return None

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / f"requirements-analyst-demo-{len(created_workers)}"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = f"agreq-analysis-demo-{len(created_workers)}"
                    self.config = config
                    self.killed = False
                    created_workers.append(self)

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    if len(created_workers) == 1:
                        raise RuntimeError("API Error: 401 invalid access token or token expired")
                    clear_path.write_text("重新鉴权后的需求澄清正文\n", encoding="utf-8")
                    record_path.write_text("- [已确认] 重新鉴权完成\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    self.killed = True
                    return self.session_name

                def read_state(self):
                    if len(created_workers) == 1:
                        return {
                            "health_status": "provider_auth_error",
                            "health_note": "provider_auth_error",
                        }
                    return {
                        "health_status": "alive",
                        "health_note": "alive",
                    }

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch(
                "A02_RequirementsAnalysis.prompt_yes_no",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.stdin_is_interactive",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.prompt_vendor",
                return_value="codex",
            ), patch(
                "A02_RequirementsAnalysis.prompt_model",
                return_value="gpt-5.4-mini",
            ), patch(
                "A02_RequirementsAnalysis.prompt_effort",
                return_value="high",
            ), patch(
                "A02_RequirementsAnalysis.prompt_proxy_url",
                return_value="",
            ), patch("sys.stdout", io.StringIO()):
                result = run_requirements_analysis(root, "需求A")

            self.assertEqual(result.requirements_clear_path, str(clear_path.resolve()))
            self.assertEqual(len(created_workers), 2)
            self.assertEqual(created_workers[1].config.model, "gpt-5.4-mini")
            self.assertTrue(created_workers[0].killed)

    def test_run_requirements_analysis_recreates_ba_after_agent_ready_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original_path = root / "需求A_原始需求.md"
            original_path.write_text("原始需求正文\n", encoding="utf-8")
            _, clear_path, _, record_path = build_requirements_analysis_paths(root, "需求A")
            created_workers: list[object] = []

            class FakeSpinnerMonitor:
                def __init__(self, *, frame_builder, stream=None, interval_sec=0.2):  # noqa: ANN001
                    return None

                def start(self):
                    return None

                def stop(self):
                    return None

            class FakeTmuxWorker:
                def __init__(self, *, worker_id, work_dir, config, runtime_root, **kwargs):  # noqa: ANN001
                    self.runtime_root = Path(runtime_root)
                    self.runtime_root.mkdir(parents=True, exist_ok=True)
                    self.runtime_dir = self.runtime_root / f"requirements-analyst-demo-{len(created_workers)}"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.session_name = f"agreq-analysis-demo-{len(created_workers)}"
                    self.config = config
                    self.killed = False
                    created_workers.append(self)

                def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
                    return None

                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    if len(created_workers) == 1:
                        raise RuntimeError("Timed out waiting for agent ready.\nmock screen")
                    clear_path.write_text("重建后的需求澄清正文\n", encoding="utf-8")
                    record_path.write_text("- [已确认] 重建完成\n", encoding="utf-8")
                    completion_contract.validator(completion_contract.status_path)
                    return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

                def request_kill(self):
                    self.killed = True
                    return self.session_name

                def read_state(self):
                    return {
                        "health_status": "alive",
                        "health_note": "alive",
                    }

            with patch("A02_RequirementsAnalysis.TmuxBatchWorker", FakeTmuxWorker), patch(
                "A02_RequirementsAnalysis.SingleLineSpinnerMonitor",
                FakeSpinnerMonitor,
            ), patch(
                "A02_RequirementsAnalysis.prompt_yes_no",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.stdin_is_interactive",
                return_value=True,
            ), patch(
                "A02_RequirementsAnalysis.prompt_vendor",
                return_value="codex",
            ), patch(
                "A02_RequirementsAnalysis.prompt_model",
                return_value="gpt-5.4-mini",
            ), patch(
                "A02_RequirementsAnalysis.prompt_effort",
                return_value="high",
            ), patch(
                "A02_RequirementsAnalysis.prompt_proxy_url",
                return_value="",
            ), patch("sys.stdout", io.StringIO()):
                result = run_requirements_analysis(root, "需求A")

            self.assertEqual(result.requirements_clear_path, str(clear_path.resolve()))
            self.assertEqual(len(created_workers), 2)
            self.assertEqual(created_workers[1].config.model, "gpt-5.4-mini")
            self.assertTrue(created_workers[0].killed)


if __name__ == "__main__":
    unittest.main()
