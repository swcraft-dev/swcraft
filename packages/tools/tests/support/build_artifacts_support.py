"""Fakes the artifact builder's test modules share: the source tree, the build runner and the runtime payloads."""

from __future__ import annotations

import functools
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

from conftest import build_artifacts_module

ARTIFACTS = build_artifacts_module()

VERSION = "9.9.9"

#: Wall-clock mtime the staleness fixtures build "the artifacts" at; sources are
#: then aged around it explicitly, so no test depends on filesystem timing.
BUILT_AT = 1_800_000_000.0


# ---------------------------------------------------------------------------
# Fake repository + fake build runner
# ---------------------------------------------------------------------------


def make_source_repo(tmp_path: Path, version: str = VERSION, *, guide: bool = True, elkjs: bool = True) -> Path:
    """A minimal repo tree: the version module, the extension project, the guide.

    Distinct from ``test_clean_install._make_repo_with_wheels``, which builds a tree that already carries the built
    wheels. ``build_all`` refuses a tree without the inputs present by default: the guide (``mkdocs.yml``, one page
    under ``docs/guide/`` and a release note at the tree's version, which the site copies into *Release notes*), the
    root ``LICENSE`` and ``NOTICE`` with the four Python project directories (both files are copied into each project
    before its wheel is built), and the pins file with a staged elkjs payload at the pinned version. ``guide=False``
    and ``elkjs=False`` are for the cases that assert what a missing input reports.
    """
    repo_root = tmp_path / "repo"
    version_module = repo_root / ARTIFACTS.VERSION_MODULE
    version_module.parent.mkdir(parents=True)
    version_module.write_text(f'__version__ = "{version}"\n', encoding="utf-8", newline="\n")
    (repo_root / ARTIFACTS.VSCODE_PROJECT).mkdir(parents=True)
    for name in ARTIFACTS.LICENCE_FILENAMES:
        (repo_root / name).write_text(f"{name} text\n", encoding="utf-8", newline="\n")
    for project in ARTIFACTS.PYTHON_PROJECTS:
        (repo_root / ARTIFACTS.PACKAGES_DIR / project).mkdir(parents=True, exist_ok=True)
    if guide:
        (repo_root / ARTIFACTS.MKDOCS_CONFIG_FILENAME).write_text(
            "site_name: SWCraft Guide\n", encoding="utf-8", newline="\n"
        )
        (repo_root / ARTIFACTS.GUIDE_DIR).mkdir(parents=True)
        (repo_root / ARTIFACTS.GUIDE_DIR / "index.md").write_text("# Guide\n", encoding="utf-8", newline="\n")
        notes = repo_root / ARTIFACTS.RELEASE_NOTES_DIR
        notes.mkdir(parents=True)
        (notes / f"{version}.md").write_text(f"# SWCraft {version}\n", encoding="utf-8", newline="\n")
    write_pin_files(repo_root)
    if elkjs:
        stage_fixture_elkjs_payload(repo_root)
    return repo_root


class FakeRunner:
    """Records every argv and materializes the artifacts a real build would.

    ``fail_on`` receives each argv and returns True for the call that should fail,
    which is how the abort path is exercised. ``package_effect`` replaces what the
    ``npm run package`` step leaves behind -- a declared constructor argument
    rather than a private method reassigned from outside the class, so a case that
    packages nothing (or two archives) says so where the runner is built.
    """

    def __init__(self, repo_root: Path, version: str = VERSION, fail_on=None, package_effect=None) -> None:
        self.repo_root = repo_root
        self.version = version
        self.fail_on = fail_on
        self.package_effect = package_effect
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd) -> None:
        argv = [str(part) for part in argv]
        self.calls.append(argv)

        if self.fail_on is not None and self.fail_on(argv):
            raise subprocess.CalledProcessError(2, argv)

        if argv[1:3] == ["-m", "build"]:
            self._fake_python_build(argv)
        elif argv[1:4] == ["-m", "mkdocs", "build"]:
            self._fake_mkdocs_build(argv)
        elif argv[1:3] == ["-m", ARTIFACTS.GUIDE_CORPUS_MODULE]:
            self._fake_guide_corpus(argv)
        elif argv[:3] == ["npm", "run", "package"]:
            self._fake_npm_package()

    def _fake_python_build(self, argv: list[str]) -> None:
        outdir = Path(argv[argv.index("--outdir") + 1])
        outdir.mkdir(parents=True, exist_ok=True)
        distribution = ARTIFACTS.PROJECT_DISTRIBUTIONS[Path(argv[-1]).name]
        (outdir / f"{distribution}-{self.version}-py3-none-any.whl").write_text(
            f"wheel {distribution}", encoding="utf-8", newline="\n"
        )
        (outdir / f"{distribution}-{self.version}.tar.gz").write_text(
            f"sdist {distribution}", encoding="utf-8", newline="\n"
        )

    def _fake_mkdocs_build(self, argv: list[str]) -> None:
        """What mkdocs leaves behind: a rendered tree, nested, in ``--site-dir``.

        Nested on purpose -- the archive's member paths and their sort order are
        only interesting once the tree has more than one level.
        """
        site_dir = Path(argv[argv.index("--site-dir") + 1])
        (site_dir / "assets").mkdir(parents=True, exist_ok=True)
        (site_dir / "index.html").write_text("<html>guide</html>", encoding="utf-8", newline="\n")
        (site_dir / "assets" / "app.css").write_text("body{}", encoding="utf-8", newline="\n")

    def _fake_guide_corpus(self, argv: list[str]) -> None:
        """What the corpus producer leaves behind: one JSON file, where ``--out`` says."""
        Path(argv[argv.index("--out") + 1]).write_text('{"corpus":"swcraft-guide"}\n', encoding="utf-8", newline="\n")

    def _fake_npm_package(self) -> None:
        if self.package_effect is not None:
            self.package_effect()
            return
        project_root = self.repo_root / ARTIFACTS.VSCODE_PROJECT
        (project_root / f"swcraft-vscode-{self.version}.vsix").write_text("vsix", encoding="utf-8", newline="\n")

    @property
    def npm_calls(self) -> list[list[str]]:
        return [argv[1:] for argv in self.calls if argv[0] == "npm"]


# ---------------------------------------------------------------------------
# The workstation archive
# ---------------------------------------------------------------------------


#: The DOS epoch, the earliest timestamp a zip entry can carry. Every synthetic wheel is stamped with it, because
#: ``writestr`` with a ``str`` name stamps ``time.localtime()`` at 2-second granularity: two fixture calls a second
#: apart produced different wheels, and the byte-identity case went intermittently red.
FIXTURE_WHEEL_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# The runtime payloads: `dev.py runtime`
# ---------------------------------------------------------------------------

#: The archive's three platform tokens, derived from the token mapping itself (one tag
#: each, from the parametrisation above) rather than restated as a list.
ARCHIVE_TOKENS = sorted(
    {ARTIFACTS.workstation_platform_token(tag) for tag in ("win-amd64", "macosx-11.0-arm64", "linux-x86_64")}
)


@functools.cache
def fixture_node_body(token: str) -> bytes:
    """The bytes a fake nodejs.org serves for *token*: a real archive of the publisher's kind, distinct per platform.

    Real rather than a stand-in because the notices read the ``LICENSE`` directly inside its
    leading directory, exactly where nodejs.org keeps it. It carries npm's own ``LICENSE``
    too, **first**, where the real archives nest it (``lib/node_modules/npm/`` in a tarball,
    ``node_modules/npm/`` in the zip): a depth rule loosened to "any ``LICENSE``" would put
    npm's text into Node's notice. Every timestamp is pinned, so the bytes -- and the
    synthetic digest pinned for them -- never vary between calls or hosts.
    """
    row = ARTIFACTS.RUNTIME_PLATFORMS[token]
    root = f"node-v{FIXTURE_NODE_VERSION}-{row['node_token']}"
    nested = "node_modules/npm/LICENSE" if row["node_extension"] == ".zip" else "lib/node_modules/npm/LICENSE"
    members = (
        (f"{root}/{nested}", NPM_LICENCE_DECOY.encode()),
        (f"{root}/LICENSE", f"The Node.js licence, as published for {token}.\n".encode()),
    )
    buffer = io.BytesIO()
    if row["node_extension"] == ".zip":
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, text in members:
                bundle.writestr(zipfile.ZipInfo(name, date_time=FIXTURE_WHEEL_TIMESTAMP), text)
        return buffer.getvalue()
    if row["node_extension"] == ".tar.gz":
        with (
            gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w:", format=tarfile.PAX_FORMAT) as bundle,
        ):
            _add_tar_files(bundle, members)
    else:
        with tarfile.open(fileobj=buffer, mode="w:xz", format=tarfile.PAX_FORMAT) as bundle:
            _add_tar_files(bundle, members)
    return buffer.getvalue()


#: The text of the ``LICENSE`` npm ships nested inside Node's archive -- never Node's notice.
NPM_LICENCE_DECOY = "npm's own licence, nested inside the Node.js archive, which is not Node's.\n"


def _add_tar_files(bundle: tarfile.TarFile, members: Iterable[tuple[str, bytes]]) -> None:
    for name, text in members:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(text), 0o644, 0
        bundle.addfile(info, io.BytesIO(text))


#: The companion extensions the synthetic pins name, with the licence each fixture package
#: declares: one universal build and one built per platform, so both Marketplace URL shapes
#: and both kinds of pin are exercised. Not the real three -- no fake package matches a real digest.
FIXTURE_EXTENSIONS = (
    ("example.universal-ext", "1.0.0", "MIT"),
    ("example.platform-ext", "2.0.0", "EPL-2.0"),
)


def fixture_target(ident: str, token: str) -> str:
    """The Marketplace build the synthetic pins take for *ident* on *token*."""
    if ident == "example.universal-ext":
        return ARTIFACTS.UNIVERSAL_TARGET
    return ARTIFACTS.RUNTIME_PLATFORMS[token]["vscode_target"]


@functools.cache
def fixture_vsix_body(ident: str, target: str) -> bytes:
    """A miniature ``.vsix``: its own ``package.json``, a descriptor naming its licence file, and that file."""
    version, licence = {entry[0]: entry[1:] for entry in FIXTURE_EXTENSIONS}[ident]
    members = (
        (
            "extension.vsixmanifest",
            "<PackageManifest><Metadata><License>extension/LICENSE.txt</License></Metadata></PackageManifest>\n",
        ),
        ("extension/package.json", json.dumps({"name": ident.split(".")[1], "version": version, "license": licence})),
        ("extension/LICENSE.txt", f"The licence text of {ident}, {target} build.\n"),
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, text in members:
            bundle.writestr(zipfile.ZipInfo(name, date_time=FIXTURE_WHEEL_TIMESTAMP), text)
    return buffer.getvalue()


#: The Node version the synthetic pins name. Not the real pin: no fake body matches a
#: real publisher digest, so nothing synthetic should look like the real release either.
FIXTURE_NODE_VERSION = "1.2.3"

#: The elkjs version the synthetic pins name, and the licence its fake ``package.json`` declares --
#: the real package's disjunction, so the notice's choice is exercised on the real shape.
FIXTURE_ELKJS_VERSION = "0.9.9"
FIXTURE_ELKJS_LICENCE = "EPL-2.0 OR GPL-3.0-or-later"


@functools.cache
def fixture_elkjs_body(version: str = FIXTURE_ELKJS_VERSION, licence: str = FIXTURE_ELKJS_LICENCE) -> bytes:
    """A miniature npm tarball of elkjs: ``package/`` with the five payload files and three it must leave behind.

    The extras sit beside the payload files on purpose -- a ``README.md`` at the root, a fourth
    ``lib/`` file and a nested directory -- so a stage that copied the tarball rather than the five
    named files is seen. Timestamps are pinned, so the synthetic digest never varies.
    """
    files = {
        "package/package.json": json.dumps({"name": "elkjs", "version": version, "license": licence}).encode(),
        "package/LICENSE.md": b"Eclipse Public License - v 2.0 (the fixture's text)\n",
        "package/lib/main.js": b"module.exports = require('./elk-api.js');\n",
        "package/lib/elk-api.js": b"// the api\n",
        "package/lib/elk-worker.min.js": b"// the worker\n",
        "package/README.md": b"# elkjs\n",
        "package/lib/elk.bundled.js": b"// not needed\n",
        "package/lib/js/extra.js": b"// not needed either\n",
    }
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w:", format=tarfile.PAX_FORMAT) as bundle,
    ):
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(payload), 0o644, 0
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def stage_fixture_elkjs_payload(repo_root: Path, version: str = FIXTURE_ELKJS_VERSION) -> Path:
    """What ``install_elkjs_payload`` leaves, written directly: the five files at *version*."""
    tree = repo_root / ARTIFACTS.PACKAGE_ELKJS_DIR
    for relative in ARTIFACTS.ELKJS_PAYLOAD_FILES:
        target = tree.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"payload\n")
    (tree / "package.json").write_text(
        json.dumps({"name": "elkjs", "version": version, "license": FIXTURE_ELKJS_LICENCE}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return tree


#: The CPython version and build tag the synthetic pins name. The version keeps the running
#: interpreter's ``major.minor`` -- that is what the fixture ``PLATFORM.json`` records and what
#: the archive compares it with -- and a patch no release has.
FIXTURE_PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.99"
FIXTURE_PYTHON_BUILD_TAG = "20000101"


@functools.cache
def fixture_python_body(token: str) -> bytes:
    """A miniature ``install_only`` tarball for *token*: ``python/`` with the interpreter where that build keeps it.

    gzip's and tar's timestamps are pinned, so the bytes -- and the synthetic digest pinned
    for them -- never vary between calls or hosts.
    """
    row = ARTIFACTS.RUNTIME_PLATFORMS[token]
    minor = ".".join(FIXTURE_PYTHON_VERSION.split(".")[:2])
    files = (
        (row["python_executable"], f"the interpreter for {token}\n".encode(), 0o755),
        (row["python_licence"].format(minor=minor), f"The CPython licence, as built for {token}.\n".encode(), 0o644),
    )
    directories = sorted(
        {
            "python",
            *(
                "python/" + "/".join(path.split("/")[:depth])
                for path, _, _ in files
                for depth in range(1, path.count("/") + 1)
            ),
        }
    )
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w:", format=tarfile.PAX_FORMAT) as bundle,
    ):
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
            bundle.addfile(info)
        for relative, payload, mode in files:
            info = tarfile.TarInfo(f"python/{relative}")
            info.size, info.mode, info.mtime = len(payload), mode, 0
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def write_pin_files(repo_root: Path, *, node_version: str = FIXTURE_NODE_VERSION) -> None:
    """A synthetic pins file for fake payload bodies.

    Synthetic because no fake download can match a real publisher digest: its digests are
    those of :func:`fixture_node_body`, :func:`fixture_python_body`, :func:`fixture_elkjs_body` and
    :func:`fixture_vsix_body`, for every token. It lands in a subdirectory of
    ``scripts/workstation/``, which the archive does not ship.
    """
    write_runtime_pins(
        repo_root,
        {
            "description": "synthetic pins for the hermetic runtime cases",
            "node": {
                "version": node_version,
                "platforms": {
                    token: {"sha256": hashlib.sha256(fixture_node_body(token)).hexdigest()} for token in ARCHIVE_TOKENS
                },
            },
            "python": {
                "version": FIXTURE_PYTHON_VERSION,
                "build_tag": FIXTURE_PYTHON_BUILD_TAG,
                "platforms": {
                    token: {"sha256": hashlib.sha256(fixture_python_body(token)).hexdigest()}
                    for token in ARCHIVE_TOKENS
                },
            },
            "elkjs": {"version": FIXTURE_ELKJS_VERSION, "sha256": hashlib.sha256(fixture_elkjs_body()).hexdigest()},
            "extensions": [
                {
                    "id": ident,
                    "version": version,
                    "platforms": {
                        token: {
                            "target": fixture_target(ident, token),
                            "sha256": hashlib.sha256(
                                fixture_vsix_body(ident, fixture_target(ident, token))
                            ).hexdigest(),
                        }
                        for token in ARCHIVE_TOKENS
                    },
                }
                for ident, version, _ in FIXTURE_EXTENSIONS
            ],
        },
    )


def write_runtime_pins(repo_root: Path, pins: dict) -> None:
    path = repo_root / ARTIFACTS.RUNTIME_PINS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8", newline="\n")
