# Third-party notices

SWCraft is licensed under the Apache License, Version 2.0 (`LICENSE`, `NOTICE`). This file names
what SWCraft did not write: first the third-party content inside this repository, then what a
release archive bundles beside SWCraft. It is written by hand. Nothing here says whether those
licences suit your use.

## 1. Third-party content in this repository

### LikeC4's diagram palette (MIT)

The colours of the diagram tab's light and dark themes are LikeC4 1.60.0's published palette, with
two changes of SWCraft's own (`vscode_extension/src/likec4Palette.ts` names the values, the files they
were read from and the changes). The values were read as text from the npm packages `@likec4/core`
1.60.0 and `@likec4/diagram` 1.60.0, each released by the LikeC4 project under the MIT License, whose
licence text and copyright notice are the `LICENSE` file of each package. No LikeC4 code is included,
installed or run.

### The WATERS 2015 figures (cited data)

The specimen generator (`packages/specimens/`) draws the shape of its synthetic models on figures
published in Kramer, Ziegenbein and Hamann, "Real World Automotive Benchmarks For Free", WATERS 2015
(`CITATION.cff` cites it). The figures are data, cited table by table in
`packages/specimens/PROVENANCE.md`; no text of the paper is included.

### AUTOSAR identifiers (used by reference)

SWCraft writes and reads AUTOSAR ARXML, so its code and documents name AUTOSAR element, attribute and
requirement identifiers. They are used by reference only: no AUTOSAR specification text and no AUTOSAR
schema is included. The licensed AUTOSAR XSDs are never in this repository; a user who has them points
the validation at them (`README.md`, *AUTOSAR schema*).

## 2. What a release archive bundles

A release's workstation archive carries these parts beside SWCraft's own wheels and `.vsix`. Each
version below is the one `scripts/workstation/pins/runtime-pins.json` pins for this release
(`packages/tools/tests/test_third_party_notices.py` holds the table equal to that file). Every archive carries
its own complete `THIRD-PARTY-NOTICES.md`, generated from the parts themselves, with each part's
declared licence and its licence text as published, and with every Python package of the wheel closure.

| Part | Version | Declared licence |
|---|---|---|
| Node.js | 22.23.2 | MIT, with the licences of the components it bundles (its `LICENSE` file) |
| CPython, as python-build-standalone publishes it | 3.11.16 (release 20260901) | PSF-2.0, with the licences of the libraries it bundles (its `LICENSE.txt` file) |
| elkjs | 0.12.0 | EPL-2.0 (SWCraft distributes it under EPL-2.0; five of its files ride inside the `swcraft-tools` wheel) |
| `redhat.vscode-yaml` | 2.0.0 | MIT |
| `redhat.vscode-xml` | 0.29.3 | EPL-2.0 |
| The Python wheel closure | the versions each release resolves | each package's own, listed with its licence in the archive's notices |
