"""The open files hold no private address, home path, e-mail address or dead link, and name no closed path.

The open half of the open copy's content gates (the closed half is ``scripts/public_snapshot.py check
--deny-list`` and gitleaks). It scans **the expansion of ``public-paths.txt`` only**, read through
``public_snapshot.expansion`` at ``HEAD``, so it answers the same in the full checkout and in the public tree; a
file holding a NUL byte is binary and skipped. It fails on:

- an **RFC 1918 IPv4 address**;
- an **absolute home path** (``/Users/<name>``, ``/home/<name>``, ``<drive>:\\Users\\<name>``) whose user name is
  not a placeholder of :data:`PLACEHOLDER_USERS`, a variable or an elision;
- an **e-mail address** outside :data:`ALLOWED_EMAIL_DOMAINS` and :data:`ALLOWED_EMAIL_LOCAL_PARTS`;
- in Markdown, an **inline link or reference definition whose relative target is not in the expansion** (code is
  not a link; a target under ``docs/guide/release-notes/`` resolves when its open source exists).

A hit that is right where it is goes in :data:`ALLOWED_HITS` with its reason. :func:`closed_path_scan` fails on a
text file naming what :data:`_CLOSED_PATH` matches, each as a path of its own; a file that must name one to refuse,
exclude or configure it is in :data:`CLOSED_PATH_MECHANISM_FILES`. An allow-list entry no hit uses fails.

What this does not reach: a link in any other format, a ``#fragment``, an absolute URL (the deny-list's), a
public IP address, a home path spelled with ``~`` or a variable, a split or obfuscated e-mail address, an image or
binary file, and a private fact in plain words. The closed-path scan does not reach a bare ``CLAUDE.md`` or
``.claude/``, the root ``.vscode/mcp*.json``, ``.github/agents/`` and ``.github/copilot-instructions.md`` (a user
project's paths too), a path spelled in parts or built at run time, a pointer in words, and a mechanism file.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import unquote

import pytest
from conftest import REPO_ROOT, public_snapshot_module
from scratch_git import quiet_maintenance
from symlink_support import symlink_or_skip

# ---------------------------------------------------------------------------
# The allow-lists (data)
# ---------------------------------------------------------------------------

#: User names that stand for "some user" in a path: test vectors and examples, never a person.
#: Compared case-insensitively. ``john`` is the ``John Smith`` of a path with a space in it.
PLACEHOLDER_USERS = frozenset(
    {
        "a",
        "architect",
        "dev",
        "john",
        "me",
        "other",
        "runner",
        "someone",
        "u",
        "user",
        "username",
        "x",
        "you",
    }
)

#: Domains an e-mail address may use: the example domains of RFC 2606 and RFC 6761, matched as
#: the domain itself or a suffix after a dot.
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example", "invalid", "test", "localhost")

#: Local parts an e-mail address may use with any domain: an address nobody reads.
ALLOWED_EMAIL_LOCAL_PARTS = ("noreply", "no-reply")


def _dotted(*octets: int) -> str:
    """An IPv4 address built at run time, so this file holds none it would report."""
    return ".".join(str(octet) for octet in octets)


#: The one folder the guide build generates from open sources: it copies docs/release-notes/ into the
#: guide and writes the index (build_artifacts.py's GUIDE_RELEASE_NOTES_DIR), and the folder is
#: gitignored. A link into it resolves when it names the index or a note the source folder holds.
_GUIDE_RELEASE_NOTES = "docs/guide/release-notes/"
_RELEASE_NOTES_SOURCE = "docs/release-notes/"

#: Hits that are right where they are, as ``{(kind, path, value): reason}``.
ALLOWED_HITS: Mapping[tuple[str, str, str], str] = {
    ("private-address", "vscode_extension/src/guideServer.test.ts", _dotted(10, 0, 0, 9)): (
        "a test vector: the guide server's URL parser must refuse an announcement on a private address"
    ),
    ("email", "packages/tools/tests/test_build_report.py", "@".join(("a", "b.com"))): (
        "a test vector: the report escapes a bare address so Markdown does not autolink it"
    ),
}

#: The public files whose logic must name a closed path, as ``{path: reason}``: every line of such a
#: file is exempt from the closed-path scan, so a pointer in one is the reviewer's to catch.
CLOSED_PATH_MECHANISM_FILES: Mapping[str, str] = {
    "ruff.toml": "the lint configuration the work repository shares: per-file rules of closed files",
    "scripts/dev.py": "`test-internal` runs the closed tests: `INTERNAL_DIR`, its plan, its help and refusals",
    "scripts/history_markers.py": "the history-marker gate's closed-path patterns and its `KEPT` entries",
    "scripts/quality_baseline.json": "the size ratchet's keys of closed test files, kept unmeasured where absent",
    "scripts/quality_ratchet.py": "`TEST_FILE_TREES` and `CLOSED_TEST_TREES`: the closed test tree it measures",
    "scripts/tests/test_dev_script.py": "`test-internal`'s refusal texts and synthetic `internal/` arguments",
    "scripts/tests/test_history_markers.py": "synthetic closed paths the history-marker gate must find",
    "scripts/tests/test_moon_dev_parity.py": "the `test-internal` row of the dev-to-moon reason map",
    "scripts/tests/test_public_snapshot.py": "synthetic closed paths `check` reports; the closed-prefix witness",
    "scripts/tests/test_quality_ratchet.py": "synthetic closed files the ratchet must classify or leave unmeasured",
    "packages/mcp/tests/test_removed_tool_names.py": "the document exclusions its closed half applies",
    "packages/tools/tests/support/guide_fixtures.py": "`FORBIDDEN_VOCABULARY`: a guide page may not name the path",
    "packages/tools/tests/test_guide_reference_model.py": "a synthetic citation the reference gate must refuse",
    "packages/tools/tests/test_guide_surface.py": "the forbidden-vocabulary term and its refused sample",
    "packages/tools/tests/test_language.py": "the closed Cyrillic test-data file left to the closed half",
    "packages/tools/tests/test_template_contents.py": "a path a scaffolded project's instructions must not name",
    "packages/tools/tests/test_user_facing_surfaces.py": "`PLAN_PATH_PATTERN`: what it refuses; its synthetic lines",
    "packages/tools/tests/test_public_tree_hygiene.py": "this scan: its pattern and its synthetic closed paths",
}

# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_PRIVATE_ADDRESS = re.compile(
    rf"(?<![\w.])(?:10(?:\.{_OCTET}){{3}}|172\.(?:1[6-9]|2\d|3[01])(?:\.{_OCTET}){{2}}"
    rf"|192\.168(?:\.{_OCTET}){{2}})(?![\w.])"
)
_HOME_PATH = re.compile(r"(?<!\w)(?:/Users/|/home/|[A-Za-z]:\\{1,2}Users\\{1,2})(?P<name>[A-Za-z0-9_.-]*)")
_EMAIL = re.compile(
    r"(?<![\w.%+\\-])(?P<local>[A-Za-z0-9._%+-]+)@(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w-])"
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+).+?\1", re.DOTALL)
_INLINE_LINK = re.compile(r"\]\(\s*<?(?P<target>[^)\s>]+)>?(?:\s+[\"'(][^)]*)?\)")
_REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(?P<target>[^\s>]+)>?", re.MULTILINE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CLOSED_PATH = re.compile(
    r"(?<![\w./\\:-])(?:docs[/\\](?:concepts|adr|contracts|checklists|scenarios|plans|agent)(?![\w-])"
    r"|internal[/\\]|\.gitea(?![\w-])|\.claude[/\\]rules(?![\w-])|CLAUDE\.md`{0,2}\s*§)"
)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_placeholder_user(name: str) -> bool:
    return not name or name.startswith(".") or name.casefold() in PLACEHOLDER_USERS


def _email_allowed(local: str, domain: str) -> bool:
    domain = domain.casefold()
    return local.casefold() in ALLOWED_EMAIL_LOCAL_PARTS or any(
        domain == allowed or domain.endswith(f".{allowed}") for allowed in ALLOWED_EMAIL_DOMAINS
    )


def _markdown_prose(text: str) -> str:
    """*text* with fenced blocks and code spans blanked, every line and offset kept."""
    lines, fence = [], ""
    for line in text.split("\n"):
        opening = _FENCE.match(line)
        if fence:
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence):
                fence = ""
            lines.append("")
        elif opening:
            fence = opening.group(1)
            lines.append("")
        else:
            lines.append(line)
    prose = "\n".join(lines)
    return _CODE_SPAN.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), prose)


def _link_resolves(source: str, target: str, opened: frozenset[str], folders: frozenset[str]) -> bool:
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if resolved in opened or resolved.rstrip("/") in folders:
        return True
    if resolved.startswith(_GUIDE_RELEASE_NOTES):
        name = resolved[len(_GUIDE_RELEASE_NOTES) :]
        return name == "index.md" or f"{_RELEASE_NOTES_SOURCE}{name}" in opened
    return False


def _dead_links(path: str, text: str, opened: frozenset[str], folders: frozenset[str]) -> Iterable[tuple[int, str]]:
    prose = _markdown_prose(text)
    for pattern in (_INLINE_LINK, _REFERENCE):
        for match in pattern.finditer(prose):
            raw = match.group("target")
            if _SCHEME.match(raw) or raw.startswith(("#", "//")):
                continue
            target = unquote(raw.split("#", 1)[0].split("?", 1)[0])
            if target and not _link_resolves(path, target, opened, folders):
                yield _line_of(prose, match.start()), raw


def _folders(paths: Iterable[str]) -> frozenset[str]:
    found = set()
    for path in paths:
        parts = path.split("/")[:-1]
        found.update("/".join(parts[: index + 1]) for index in range(len(parts)))
    return frozenset(found)


def hygiene_scan(
    repo: Path, allowed: Mapping[tuple[str, str, str], str] = ALLOWED_HITS
) -> tuple[list[str], set[tuple[str, str, str]]]:
    """Every hit in the expansion of *repo*'s allow-list at ``HEAD``, and the allow-list keys used.

    A finding reads ``<path>:<line>: <kind> <value>``.
    """
    opened = frozenset(public_snapshot_module().expansion(repo, "HEAD"))
    folders = _folders(opened)
    findings: list[str] = []
    used: set[tuple[str, str, str]] = set()

    def report(kind: str, path: str, line: int, value: str) -> None:
        key = (kind, path, value)
        if key in allowed:
            used.add(key)
        else:
            findings.append(f"{path}:{line}: {kind} {value}")

    for path in sorted(opened):
        target = repo / path
        if target.is_symlink() or not target.is_file():
            continue
        data = target.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for match in _PRIVATE_ADDRESS.finditer(text):
            report("private-address", path, _line_of(text, match.start()), match.group())
        for match in _HOME_PATH.finditer(text):
            if not _is_placeholder_user(match.group("name")):
                report("home-path", path, _line_of(text, match.start()), match.group())
        for match in _EMAIL.finditer(text):
            if not _email_allowed(match.group("local"), match.group("domain")):
                report("email", path, _line_of(text, match.start()), match.group())
        if path.endswith(".md"):
            for line, link in _dead_links(path, text, opened, folders):
                report("dead-link", path, line, link)
    return findings, used


def _text_files(repo: Path) -> Iterable[tuple[str, str]]:
    """``(path, text)`` of every text file of *repo*'s expansion at ``HEAD``: no link, no NUL byte."""
    for path in sorted(public_snapshot_module().expansion(repo, "HEAD")):
        target = repo / path
        if target.is_symlink() or not target.is_file():
            continue
        data = target.read_bytes()
        if b"\0" not in data:
            yield path, data.decode("utf-8", errors="replace")


def closed_path_scan(
    repo: Path, mechanism: Mapping[str, str] = CLOSED_PATH_MECHANISM_FILES
) -> tuple[list[str], set[str], int]:
    """Every closed path an open text file of *repo* names.

    Returns the findings, the *mechanism* files that named one, and the number of text files read. A finding reads
    ``<path>:<line>: closed-path <value>``."""
    findings: list[str] = []
    used: set[str] = set()
    read = 0
    for path, text in _text_files(repo):
        read += 1
        for match in _CLOSED_PATH.finditer(text):
            if path in mechanism:
                used.add(path)
            else:
                findings.append(f"{path}:{_line_of(text, match.start())}: closed-path {match.group()}")
    return findings, used, read


# ---------------------------------------------------------------------------
# This repository
# ---------------------------------------------------------------------------


def test_no_open_file_holds_a_private_address_a_home_path_an_email_or_a_dead_link() -> None:
    findings, used = hygiene_scan(REPO_ROOT)
    assert findings == [], "\n".join(findings)
    assert set(ALLOWED_HITS) - used == set(), "an allow-list entry no hit uses: remove it"


#: The fewest text files the expansion holds (it holds well above, nearly all text): a scan that read
#: fewer has lost the tree, not cleaned it.
_TEXT_FILES_FLOOR = 2_000


def test_no_open_file_names_a_closed_path() -> None:
    findings, used, read = closed_path_scan(REPO_ROOT)
    assert read >= _TEXT_FILES_FLOOR, read
    assert findings == [], "\n".join(findings)
    assert set(CLOSED_PATH_MECHANISM_FILES) - used == set(), "a mechanism entry that names no closed path: remove it"


def test_every_mechanism_entry_names_an_open_file_and_a_reason() -> None:
    opened = set(public_snapshot_module().expansion(REPO_ROOT, "HEAD"))
    for path, reason in CLOSED_PATH_MECHANISM_FILES.items():
        assert path in opened, path
        assert reason.strip(), path


def test_every_allow_list_entry_names_an_open_file_and_a_reason() -> None:
    opened = set(public_snapshot_module().expansion(REPO_ROOT, "HEAD"))
    for (kind, path, value), reason in ALLOWED_HITS.items():
        assert kind in {"private-address", "home-path", "email", "dead-link"}, kind
        assert path in opened, path
        assert value and reason.strip(), (kind, path)


# ---------------------------------------------------------------------------
# The scan's own cases, on a temporary repository
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def _repo(
    tmp_path: Path,
    files: Mapping[str, str],
    allow_list: str = "docs/\ntools/\npublic-paths.txt\n",
    *,
    binary: Mapping[str, bytes] | None = None,
    links: Mapping[str, str] | None = None,
) -> Path:
    """A one-commit repository holding the given files and the allow-list.

    It holds *files*, *binary* files, symbolic *links* (``{link: target}``) and the allow-list, which opens ``docs/``
    and ``tools/``."""
    repo = tmp_path / "source"
    repo.mkdir()
    base = {"docs/index.md": "index\n", "tools/__init__.py": ""}
    for relative, content in {**base, **files, "public-paths.txt": allow_list}.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    for relative, data in (binary or {}).items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for relative, link_target in (links or {}).items():
        symlink_or_skip(repo / relative, Path(link_target))
    identity = {"GIT_AUTHOR_NAME": "Someone", "GIT_COMMITTER_NAME": "Someone"}
    identity |= {"GIT_AUTHOR_EMAIL": "someone@example.com", "GIT_COMMITTER_EMAIL": "someone@example.com"}
    for args in (("init", "--quiet"), ("add", "--all"), ("commit", "--quiet", "-m", "source")):
        subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env={**os.environ, **identity},
        )
        if args[0] == "init":
            quiet_maintenance(repo)
    return repo


def _findings(tmp_path: Path, files: Mapping[str, str]) -> list[str]:
    """The findings of *files* with no allow-list entry."""
    return hygiene_scan(_repo(tmp_path, files), {})[0]


@pytest.mark.parametrize(
    "address",
    [_dotted(10, 1, 2, 3), _dotted(172, 16, 0, 1), _dotted(172, 31, 255, 254), _dotted(192, 168, 1, 20)],
)
def test_a_private_address_is_found(tmp_path: Path, address: str) -> None:
    assert _findings(tmp_path, {"tools/a.py": f'HOST = "{address}"\n'}) == [f"tools/a.py:1: private-address {address}"]


@pytest.mark.parametrize(
    "text",
    [_dotted(172, 15, 0, 1), _dotted(172, 32, 0, 1), _dotted(8, 8, 8, 8), "1" + _dotted(10, 1, 2, 3), "0.23.0"],
)
def test_a_public_address_or_a_version_is_not(tmp_path: Path, text: str) -> None:
    assert _findings(tmp_path, {"tools/a.py": f'VALUE = "{text}"\n'}) == []


@pytest.mark.parametrize(
    "prefix",
    ["/Users/", "/home/", "C:\\Users\\", "D:\\\\Users\\\\"],
    ids=["macos", "linux", "windows", "windows-escaped"],
)
def test_a_home_path_with_a_real_user_name_is_found(tmp_path: Path, prefix: str) -> None:
    path = prefix + "jdoe" + prefix[-1] + "work"
    findings = _findings(tmp_path, {"docs/a.md": f"see `{path}`\n"})
    assert findings == [f"docs/a.md:1: home-path {prefix}jdoe"]


@pytest.mark.parametrize("name", sorted(PLACEHOLDER_USERS) + ["...", "$USER", "{home}", "<you>", "%USERNAME%", ""])
def test_a_home_path_with_a_placeholder_user_name_is_not(tmp_path: Path, name: str) -> None:
    files = {"tools/a.py": f'PATHS = ["/home/{name}/x", "/Users/{name.upper()}/x", "C:\\\\Users\\\\{name}\\\\x"]\n'}
    assert _findings(tmp_path, files) == []


def test_an_email_address_is_found(tmp_path: Path) -> None:
    address = "@".join(("jane.doe", "corp-mail.de"))
    assert _findings(tmp_path, {"docs/a.md": f"write to {address}.\n"}) == [f"docs/a.md:1: email {address}"]


@pytest.mark.parametrize(
    "address",
    [
        *("@".join(("someone", domain)) for domain in ALLOWED_EMAIL_DOMAINS),
        "@".join(("someone", "mail.example.org")),
        *("@".join((local, "users.github.com")) for local in ALLOWED_EMAIL_LOCAL_PARTS),
    ],
)
def test_an_example_or_noreply_address_is_not(tmp_path: Path, address: str) -> None:
    assert _findings(tmp_path, {"docs/a.md": f"write to {address}.\n"}) == []


@pytest.mark.parametrize(
    "text",
    ["react@18.3.1", "actions/checkout@v4", "`@types/node`", "\\n@AGENTS.md"],
)
def test_a_package_pin_or_an_import_line_is_no_email_address(tmp_path: Path, text: str) -> None:
    assert _findings(tmp_path, {"tools/a.py": f'VALUE = "{text}"\n'}) == []


def test_a_dead_link_and_a_link_into_a_closed_path_are_found(tmp_path: Path) -> None:
    files = {
        "docs/a.md": "One [gone](missing.md).\n\nTwo [plan](../closed/plan.md#top).\n\n[ref]: ./also-gone.md\n",
        "closed/plan.md": "closed\n",
    }
    assert _findings(tmp_path, files) == [
        "docs/a.md:1: dead-link missing.md",
        "docs/a.md:3: dead-link ../closed/plan.md#top",
        "docs/a.md:5: dead-link ./also-gone.md",
    ]


def test_a_resolving_link_an_external_one_an_anchor_and_code_are_not(tmp_path: Path) -> None:
    files = {
        "docs/a.md": (
            "[b](b.md#part) [tools](../tools/) [root](/tools/x.py) [b](b.md 'title') [web](https://example.com/x)\n"
            "[mail](mailto:someone@example.com) [here](#here) `[title](link)`\n\n"
            "```md\n[inside](a-fence.md)\n```\n"
        ),
        "docs/b.md": "b\n",
        "tools/x.py": "x = 1\n",
    }
    assert _findings(tmp_path, files) == []


def test_a_link_into_a_generated_folder_resolves_by_its_open_source(tmp_path: Path) -> None:
    files = {
        "docs/guide/upgrading.md": (
            "[all](release-notes/index.md) [one](release-notes/1.0.0.md) [none](release-notes/9.9.9.md)\n"
        ),
        "docs/release-notes/1.0.0.md": "notes\n",
    }
    assert _findings(tmp_path, files) == ["docs/guide/upgrading.md:1: dead-link release-notes/9.9.9.md"]


def test_a_file_holding_a_nul_byte_is_skipped_and_the_same_text_is_not(tmp_path: Path) -> None:
    """Binary data is not scanned as text; the counter-case shows the skip is the NUL byte's."""
    address = _dotted(192, 168, 5, 5)
    repo = _repo(
        tmp_path,
        {"tools/text.py": f'HOST = "{address}"\n'},
        binary={"tools/data.bin": f"HOST = {address}\n".encode() + b"\0\1"},
    )
    assert hygiene_scan(repo, {})[0] == [f"tools/text.py:1: private-address {address}"]


def test_a_symbolic_link_is_skipped(tmp_path: Path) -> None:
    """A link is not followed into its target's text: the target is scanned under its own path."""
    address = _dotted(192, 168, 6, 6)
    files = {"tools/target.py": f'HOST = "{address}"\n'}
    repo = _repo(tmp_path, files, links={"tools/link.py": "target.py"})
    assert hygiene_scan(repo, {})[0] == [f"tools/target.py:1: private-address {address}"]


def test_only_the_expansion_is_scanned(tmp_path: Path) -> None:
    address = _dotted(192, 168, 7, 7)
    home = "/home/" + "jdoe"
    files = {"closed/notes.md": f"{address} [x](gone.md) {home}\n", "tools/a.py": "x = 1\n"}
    assert _findings(tmp_path, files) == []


@pytest.mark.parametrize("key", sorted(ALLOWED_HITS))
def test_each_allow_list_entry_silences_its_own_hit_and_no_other(tmp_path: Path, key: tuple[str, str, str]) -> None:
    kind, path, value = key
    folder = path.split("/", 1)[0] + "/"
    files = {path: f"{value}\n", "docs/other.md": f"{value}\n"}
    allow_list = f"{folder}\ndocs/\npublic-paths.txt\n" if folder != "docs/" else "docs/\npublic-paths.txt\n"
    repo = _repo(tmp_path, files, allow_list)
    unallowed, _ = hygiene_scan(repo, {})
    assert f"{path}:1: {kind} {value}" in unallowed
    findings, used = hygiene_scan(repo, {key: ALLOWED_HITS[key]})
    assert findings == [f"docs/other.md:1: {kind} {value}"]
    assert used == {key}


@pytest.mark.parametrize(
    "text",
    [
        "see docs/concepts/module-map.md",
        "`docs/adr/021-the-open-copy.md`",
        "(docs/contracts/editing-protocol/)",
        "docs/checklists/x.md",
        "docs/scenarios/05.md",
        "docs/plans/completed/x.md",
        "docs/agent/x.md",
        "internal/tests/test_x.py",
        "internal\\scripts\\run.py",
        ".gitea/workflows/verify.yml",
        ".claude/rules/tests.md",
        "CLAUDE.md § Invariants",
        "`CLAUDE.md` § *Language*",
        "``CLAUDE.md`` § Invariants",
    ],
)
def test_a_closed_path_is_found(tmp_path: Path, text: str) -> None:
    findings, _, read = closed_path_scan(_repo(tmp_path, {"tools/a.py": f"# {text}\n"}), {})
    assert read == 4  # docs/index.md, public-paths.txt, tools/__init__.py and the file
    assert len(findings) == 1 and findings[0].startswith("tools/a.py:1: closed-path "), findings


@pytest.mark.parametrize(
    "text",
    [
        "tests/fixtures/contracts/editing-protocol/x.json",
        "tests/scenarios/run.py",
        "swcraft_tools/internal/x.py",
        "docs/guide/vscode.md",
        "docs/release-notes/0.1.0.md",
        "docs/contractsx",
        "internal tests",
        "the internals/ folder",
        ".claude/settings.json",
        "the project's CLAUDE.md",
        ".github/agents/swcraft-architect.agent.md",
        "gitea.example.org",
        "node:internal/modules/esm/resolve:274",
    ],
)
def test_an_open_path_or_a_user_project_path_is_not(tmp_path: Path, text: str) -> None:
    assert closed_path_scan(_repo(tmp_path, {"tools/a.py": f"# {text}\n"}), {})[0] == []


def test_a_mechanism_entry_silences_its_own_file_and_no_other(tmp_path: Path) -> None:
    files = {"tools/deny.py": "DENY = ('internal/',)\n", "tools/other.py": "# internal/tests/x.py\n"}
    repo = _repo(tmp_path, files)
    findings, used, _ = closed_path_scan(repo, {"tools/deny.py": "a deny list"})
    assert findings == ["tools/other.py:1: closed-path internal/"]
    assert used == {"tools/deny.py"}
    assert closed_path_scan(repo, {})[0] == ["tools/deny.py:1: closed-path internal/", findings[0]]


def test_the_closed_path_scan_reads_only_the_expansion(tmp_path: Path) -> None:
    files = {"closed/notes.md": "see docs/concepts/x.md\n", "tools/a.py": "x = 1\n"}
    assert closed_path_scan(_repo(tmp_path, files), {})[0] == []
