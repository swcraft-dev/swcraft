"""Tests for builder.py — subprocess.run is mocked throughout."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from swcraft_tools.builder import BuildResult, parse_moon_errors, run_build

# parse_moon_errors


class TestParseMoonErrors:
    def test_empty_string_returns_empty_list(self):
        assert parse_moon_errors("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert parse_moon_errors("   \n\n   ") == []

    def test_structural_mode_invalid_raw(self):
        errors = parse_moon_errors("INVALID (structural mode): dist/model.arxml")
        assert len(errors) == 1
        assert errors[0].message == "INVALID (structural mode): dist/model.arxml"
        assert errors[0].path == "dist/model.arxml"

    def test_xsd_mode_invalid_raw(self):
        errors = parse_moon_errors("INVALID (XSD mode): dist/model.arxml")
        assert len(errors) == 1
        assert errors[0].path == "dist/model.arxml"

    def test_structural_mode_moon_prefixed(self):
        # Real captured moon output: task name prefix added by moon
        line = "tools:validate-arxml | INVALID (structural mode): dist/model.arxml"
        errors = parse_moon_errors(line)
        assert len(errors) == 1
        assert errors[0].path == "dist/model.arxml"

    def test_xsd_mode_moon_prefixed(self):
        line = "tools:validate-arxml | INVALID (XSD mode): dist/model.arxml"
        errors = parse_moon_errors(line)
        assert len(errors) == 1
        assert errors[0].path == "dist/model.arxml"

    def test_real_captured_moon_failure_line(self):
        # The failure shape of `moon run tools:validate-arxml` on a corrupted generated ARXML.
        captured = "tools:validate-arxml | INVALID (structural mode): dist/model.arxml"
        errors = parse_moon_errors(captured)
        assert len(errors) == 1
        assert errors[0].message == "INVALID (structural mode): dist/model.arxml"
        assert errors[0].path == "dist/model.arxml"

    def test_valid_line_returns_empty(self):
        assert parse_moon_errors("VALID (structural mode): dist/model.arxml") == []

    def test_unrelated_output_returns_empty(self):
        output = "▮▮▮▮ tools:generate-arxml (cached)\nWrote dist/model.arxml\n"
        assert parse_moon_errors(output) == []

    def test_multiple_invalid_lines(self):
        output = (
            "tools:validate-arxml | INVALID (structural mode): dist/model.arxml\n"
            "tools:validate-arxml | INVALID (XSD mode): dist/other.arxml\n"
        )
        errors = parse_moon_errors(output)
        assert len(errors) == 2
        assert errors[0].path == "dist/model.arxml"
        assert errors[1].path == "dist/other.arxml"

    def test_multiline_moon_output_with_single_error(self):
        output = (
            "▮▮▮▮ tools:download-schema (817ff4a2)\n"
            "tools:download-schema | AUTOSAR XSD not found\n"
            "▮▮▮▮ tools:download-schema (37ms, 817ff4a2)\n"
            "▮▮▮▮ tools:validate-arxml (40cf2e2e)\n"
            "tools:validate-arxml | INVALID (structural mode): dist/model.arxml\n"
            "▮▮▮▮ tools:validate-arxml (95ms, 40cf2e2e)\n"
            "Error: task_runner::run_failed\n"
        )
        errors = parse_moon_errors(output)
        assert len(errors) == 1
        assert errors[0].path == "dist/model.arxml"


# run_build


class TestRunBuild:
    def test_success_exit_code_0(self, tmp_path):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "tools:generate-arxml | Wrote dist/model.arxml\n"
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result) as mock_run:
            result = run_build(str(tmp_path))

        assert isinstance(result, BuildResult)
        assert result.success is True
        assert result.errors == []
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_failure_exit_code_1_structural_invalid(self, tmp_path):
        stdout = (
            "▮▮▮▮ tools:validate-arxml (40cf2e2e)\n"
            "tools:validate-arxml | INVALID (structural mode): dist/model.arxml\n"
            "▮▮▮▮ tools:validate-arxml (95ms, 40cf2e2e)\n"
            "Error: task_runner::run_failed\n"
        )
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = stdout
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result):
            result = run_build(str(tmp_path))

        assert result.success is False
        assert len(result.errors) >= 1
        assert any("dist/model.arxml" in e.path for e in result.errors)

    def test_failure_exit_code_1_xsd_invalid(self, tmp_path):
        stdout = "tools:validate-arxml | INVALID (XSD mode): dist/model.arxml\n"
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = stdout
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result):
            result = run_build(str(tmp_path))

        assert result.success is False
        assert len(result.errors) == 1

    def test_output_captured_in_build_result(self, tmp_path):
        stdout = "tools:generate-arxml | Wrote dist/model.arxml\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = stdout
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result):
            result = run_build(str(tmp_path))

        assert "model.arxml" in result.output

    def test_default_command_is_moon_run_build(self, tmp_path):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result) as mock_run:
            run_build(str(tmp_path))

        call_args = mock_run.call_args
        cmd = call_args[0][0]  # first positional arg (the split command list)
        assert cmd[0] == "moon"
        assert "run" in cmd
        assert ":build" in cmd

    def test_custom_command_passed_to_subprocess(self, tmp_path):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result) as mock_run:
            run_build(str(tmp_path), command="moon run tools:validate-arxml")

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "tools:validate-arxml" in cmd

    def test_command_split_via_shlex(self, tmp_path):
        """Verify subprocess receives a list (shlex.split), not a raw string."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result) as mock_run:
            run_build(str(tmp_path))

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert isinstance(cmd, list)

    def test_stderr_included_in_output(self, tmp_path):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: task_runner::run_failed\n"

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result):
            result = run_build(str(tmp_path))

        assert "run_failed" in result.output

    def test_failure_with_no_invalid_lines_still_fails(self, tmp_path):
        """Non-zero exit code alone marks failure even without INVALID lines."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "something went wrong"
        mock_result.stderr = ""

        with patch("swcraft_tools.builder.subprocess.run", return_value=mock_result):
            result = run_build(str(tmp_path))

        assert result.success is False
