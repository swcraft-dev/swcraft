"""The real-Node probe ``conftest.py`` imports: its answer parsing, and that no ``node`` changes nothing."""

from __future__ import annotations

import os
import sys

from node_on_path import REAL_NODE_DIRECTORY, node_directory_from_answer, real_node_directory


def test_an_existing_file_answers_its_directory(tmp_path):
    binary = tmp_path / "bin" / "node"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    assert node_directory_from_answer(0, os.fspath(binary) + "\n") == os.fspath(binary.parent)


def test_an_answer_that_names_no_existing_file_is_none(tmp_path):
    assert node_directory_from_answer(0, "") is None
    assert node_directory_from_answer(0, "  \n") is None
    assert node_directory_from_answer(0, os.fspath(tmp_path)) is None
    assert node_directory_from_answer(0, os.fspath(tmp_path / "missing")) is None
    assert node_directory_from_answer(0, "node") is None


def test_a_non_zero_exit_is_none_whatever_it_answered():
    assert node_directory_from_answer(1, sys.executable) is None


def test_no_node_on_path_changes_nothing():
    assert real_node_directory(which=lambda name: None) is None


def test_the_found_directory_is_first_on_path():
    if REAL_NODE_DIRECTORY is not None:
        assert os.environ["PATH"].split(os.pathsep)[0] == REAL_NODE_DIRECTORY
