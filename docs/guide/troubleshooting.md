<!-- draft: not yet reviewed by the product's author -->

# Troubleshooting

<!-- purpose: When installing, building or checking fails: the symptom, its cause and the fix -->

Each row is a symptom you may meet while installing SWCraft or building an architecture
repository, its usual cause and what to do. A finding of the model check itself is explained on
[The checks](reference/rules.md), a refusal of the assistant on [What a refusal
means](how-to/refusals.md), and a part of the editor that seems to be missing on [In
VS Code](vscode.md#when-something-is-missing).

## Installing { #installing }

<!-- purpose: When the installer refuses, or swcraft or moon is not found after installing -->

| Symptom | Cause and fix |
|---|---|
| `swcraft: command not found` in a terminal | Three causes, and on macOS and Linux the third is the usual one. SWCraft was never installed on this machine ([Install](getting-started.md#install)); or the terminal was opened before the install, and `PATH` is read when a terminal starts — open a new one; or the `export PATH=…` line the installer **printed** was never added to a shell profile, because on macOS and Linux the installer edits no shell file. Run the installer again to see the line, or add `$HOME/.swcraft/current/bin` yourself. |
| `moon: command not found` | moon is not installed or not on `PATH`. Run `proto use` in the project: it reads `.prototools` and installs the version the project names ([An architecture repository](architecture-repository.md#what-the-machine-needs)). |
| The installer refuses before anything is installed, naming a platform or a Python version | The archive was built for another platform: it records the operating system, the processor and the Python version it was made for, and all three must match. Use the archive for this platform. |
| The installer refuses, saying a file does not match its line in `SHA256SUMS` | The archive's own copy of Python is damaged, missing or was unpacked incompletely. Download the archive again and unpack it afresh. |
| The installer refuses with *SWCraft needs Python 3.11* | `--python` names an older interpreter. Leave the option out, and the Python the archive carries is used. |
| `swcraft --version` will not start after the machine's Python was upgraded or removed | The installation was made from an archive of a release that did not yet carry its own Python, and was built on the machine's. Install a current archive. |

## Building { #building }

<!-- purpose: When a command or a build fails: the pin, the output folders, a missing ARXML, an old output -->

| Symptom | Cause and fix |
|---|---|
| A command exits 1 naming two versions | The project's `toolchain_version` and the installed SWCraft disagree. Install the release the project names, or raise the pin if the project is meant to move ([Upgrading](upgrading.md#the-procedure)). The read-only commands print the same sentence and answer anyway. |
| A build exits 0 but the outputs look like the previous release's | It should not happen: `swcraft.yaml` is an input of every cached build task, so raising `toolchain_version` makes them all run again. Check first that SWCraft itself was updated — a project whose pin still names the old release refuses rather than builds — and report it if it was. |
| `clean-outputs` fails naming `dist_root` or `views_root` | The manifest declares an output folder that is the project folder itself, is absolute, or reaches outside the project through `..`. It is refused by name before anything is removed, because a `dist_root: .` would remove the model. Fix the field. |
| The build fails and `dist/` holds no `model.arxml` | Working as designed: the first step moved the previous ARXML aside as `dist/model_old.arxml` and removed the rest of the previous outputs, and the model check stopped this build before anything was generated. Read the message, fix the model, build again. A failure *later* in the build leaves the outputs of the steps that passed, `dist/model.arxml` included. Either way the exit code is the answer, not the folder ([Build the model](architecture-repository.md#build-the-model)). |
| `check-model` fails with `… is not a legal AUTOSAR Identifier` | A name that reaches the ARXML must start with a letter, hold only letters, digits and `_`, and be at most 128 characters. Every offender is listed in one run, with the file, the element, the reason and a name that would conform. Rename by hand: the model refers to elements by name, so a rename touches every file that names it, and `swcraft impact --project swcraft.yaml "<name>"` lists them. A legal name can also be refused for a name SWCraft derives from it — a 112-character component gives a 129-character `<name>_InternalBehavior` — and the message names that derived form; stating a shorter name in the field the model offers for it (a component's `behavior_name`, an event's or an access point's `name`) is a fix too. |
| The ARXML check reports a structural check only | The licensed AUTOSAR XSD for the version your ARXML targets was not found. It is not an error: the structural check runs instead. Point `SWCRAFT_AUTOSAR_XSD_PATH` at the licensed file; its name must be the one its version expects, `AUTOSAR_4-2-2.xsd` or `AUTOSAR_4-3-0.xsd` ([Validate — the gate](getting-started.md#validate-the-gate)). |

## The assistant { #the-assistant }

<!-- purpose: When the assistant reports a change as staged and the model files never change -->

| Symptom | Cause and fix |
|---|---|
| The assistant reports a change as `staged` and the model never changes | Its SWCraft server runs without a review mode, and then it stages and writes nothing — fail closed. In VS Code the extension always starts the server with the review in a SWCraft tab, so this is a chat client configured by hand, or a server started from a terminal without `SWCRAFT_REVIEW`. Set it to `panel` for the SWCraft review tab in VS Code (keep VS Code open on the project), or `elicit` for a client that asks in its own chat. Nothing was lost: a staged change writes no model file ([The assistant](assistant.md#if-the-assistant-reports-changes-you-never-see)). |
