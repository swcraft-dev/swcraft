<!-- draft: not yet reviewed by the product's author -->

# The Change Report: what a change does, before you commit

<!-- purpose: Before you commit a model change: what it changes, its links, its findings and what it affects -->

You changed the model. Before you commit, the **Change Report** tells you what the change does to the
architecture: what changed, element by element; which requirement links it gained, lost or left
needing confirmation; the findings it introduced; what it does to the cores and the memory; who uses
an interface it touched; which ARXML elements it moves; and each composition it touches, drawn before
and after.

Every line is computed from the model at two revisions. The same change gives the same report, and
nothing in it is written by AI.

## In VS Code: one click { #in-vs-code-one-click }

<!-- purpose: Making the Change Report in VS Code with one click, against the last commit -->

The SWCraft panel's *Model* section has **Change Report** (and the palette *SWCraft: Change Report*).
Click it: it compares your uncommitted model with the last commit, `HEAD`, writes the report and opens
it in VS Code's Markdown preview. It asks nothing. The command's own output goes to the **SWCraft**
output channel, like every other panel item's.

The report is written into `dist/change-report/` (your manifest's `dist_root`): `change-report.md`,
`change-report-pr.md` — the same report without its *Diagrams* section, the text for a pull request —
`change-report.json` and up to two pictures per composition it draws (a composition the change added has
no *before* picture). Each run replaces them, and
`swcraft clean-outputs` removes them with the rest of `dist/`. Nothing is written into your model.

## On the command line { #on-the-command-line }

<!-- purpose: Running the Change Report on the command line, against a branch, a tag or two revisions -->

```sh
swcraft change-report --project swcraft.yaml                 # the working tree against HEAD
swcraft change-report --project swcraft.yaml main            # the working tree against main
swcraft change-report --project swcraft.yaml v1.2 v1.3       # two revisions
```

The revisions are `swcraft diff`'s: none compares the working tree with `HEAD`, one compares that
revision with the working tree, two compare the two revisions. The command prints the path of the
report it wrote:

```text
Generated report: <project>/dist/change-report/change-report.md
```

| Option | What it does |
|---|---|
| `--format json` | prints the JSON document instead of the path line; the files are written either way |
| `--no-diagrams` | leaves the pictures out (drawing them needs Node.js, which the installation brings) |
| `--fail-on error` | fails the run when the change introduces an error |
| `--fail-on warning` | fails the run when the change introduces an error or a warning |

`--fail-on` decides only the exit code: the report is written first, then one `Error:` line names
the count and the run exits 1. Without it (or with `--fail-on never`, the default) the run succeeds
whatever the change introduces.

The run fails, and writes nothing, when it cannot read the project or a revision (an unknown
revision, a revision that does not hold the model directory, a model file that does not parse), when
an element name is declared twice at either revision — the report cannot pair elements it cannot tell
apart, and `swcraft check-model` names the duplicate —, when the report directory is refused, and when
the project's `toolchain_version` names another release.

**The Change Report is not a gate and not qualification evidence.** `swcraft validate` is the gate.
The report tells you what to look at.

## What the report says { #what-the-report-says }

<!-- purpose: Reading the report's nine parts, in order, and what each one tells you about a change -->

The report has nine parts, always in this order. A part with nothing to say says so in one sentence,
so a reader and a script always meet the same shape.

1. **The header** names the project and the two revisions: `working tree against HEAD (2137383)`.
2. **The verdict** — one line: the errors and warnings the change introduces, then in brackets the
   findings the model already had before the change — `(32 already in the model)` when they are all
   warnings, `(1 error, 32 warnings already in the model)` when both, nothing when there are none —
   how many changes in how many elements, and how many compositions the report draws. So
   `0 warning(s) introduced` never reads as a model without warnings while `check-model` shows some.
3. **What changed** — one row per element the change added, removed or changed, layer by layer. A
   changed element lists what moved in it: `data_elements.temperature.init: 20 → 25`, `+ DetectFade`
   for an added runnable, `− pOld` for a removed port. When a removed and an added element are equal
   but for their name, the row says *possibly a rename of* the old name.
4. **Requirements** — the `satisfies` links the change **gained** and **lost**, the links whose version
   it moved (**re-confirmed** `v1 → v2`), and the links it left **needing confirmation**: a requirement
   whose version went up while links still name the old one, or a link written without a version. A
   requirement's own `derived_from` is not a link here; a change to it is in *What changed*.
5. **Findings** — the `check-model` findings the change introduced, errors first. Findings the model
   already had are not listed.
6. **Resources** — each core's load and each memory's use, before and after, with the budget, the
   difference and the status after the change. A memory's budget reads in whole bytes, rounded down,
   in `change-report.md` and `change-report-pr.md`; the decision stays on the exact figure, and
   `change-report.json` keeps it.
7. **Interface consumers** — for each interface the change touched or removed, every element that
   depends on it.
8. **ARXML** — the ARXML elements the change adds, removes or changes, from the two documents
   `swcraft generate arxml` would write. A revision whose model has an error is not compared —
   `swcraft validate` would refuse it — and the part says which.
9. **Diagrams** — the overview of each composition the change touches, before and after.

## Reading the pictures { #reading-the-pictures }

<!-- purpose: Reading the report's composition pictures: when one is drawn and what its marks mean -->

Only the overview of a composition is drawn, never its internal block diagram: a large model's has far
too many connections to read a change in. A composition is drawn when the change touches it — it was
added, removed or changed, or a component or a connector inside it was added or removed.

The **before** picture is unmarked: it is where you started. Every mark is on the **after** picture,
which is laid out from the changed model plus what the change removed:

| On the after picture | Means |
|---|---|
| a green outline | a component or composition the change added |
| a red dashed outline | one the change removed — still drawn, so you see where it was |
| a green line | connectors were added between the two |
| a red line | connectors were removed between the two |
| a blue line | both: some connectors removed and others added, for example a renamed connector |

The arrowhead takes its line's colour. A component that is in both pictures is never marked, whatever
changed inside it — a port, a runnable, a link: those are in *What changed*. The number on a line is
the number of connectors it carries after the change (before it, for a line the change removed).

## The JSON document { #the-json-document }

<!-- purpose: Reading the report as a program: the fields of the JSON document and its version -->

`change-report.json` (and `--format json`) carries the same report for a program. It starts with
`"schemaVersion": 1` and `"kind": "change-report"`; a program should refuse a version it does not
know. `verdict` holds the counts (`existingErrors` and `existingWarnings` are the findings already in
the model), `changes` is exactly the `layers` object `swcraft diff --format json`
prints, and `requirements`, `findings`, `resources`, `consumers`, `arxml` and `diagrams` hold the other
parts. The figures in `resources` are the report's own strings, so an exact value stays exact.

## In a pipeline: the report as a pull request comment { #in-a-pipeline-the-report-as-a-pull-request-comment }

<!-- purpose: Posting the report as a pull request comment and failing a pipeline on an introduced error -->

The same command runs in CI. Compare the branch with the point where it left the base branch, fail
the job on an introduced error, and post `change-report-pr.md` as a comment. **Posting is your
pipeline's, not SWCraft's**: SWCraft writes the file and never talks to your forge.

The pictures are build outputs that no repository holds, so `change-report-pr.md` names none of
them: no *Diagrams* section, no legend, no picture link. The templates post that file and keep the
whole folder — `change-report.md` with its pictures — as the job's artifact; drawing needs Node.js,
which the installation brings. The templates assume the manifest at the repository's root and
`dist_root: dist`; change the two paths if yours differ.

**GitHub Actions** (Gitea Actions reads the same file; post with its API instead of `gh`):

```yaml
name: change-report
on: pull_request
jobs:
  change-report:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      # Install the SWCraft toolchain here, as your other jobs do.
      - name: Change report
        run: |
          base="$(git merge-base "origin/${{ github.base_ref }}" HEAD)"
          swcraft change-report --project swcraft.yaml "$base" --fail-on error
      - name: Post the report
        if: always() && hashFiles('dist/change-report/change-report-pr.md') != ''
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh pr comment "${{ github.event.pull_request.number }}" --body-file dist/change-report/change-report-pr.md
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: change-report
          path: dist/change-report/
```

**GitLab CI**:

```yaml
change-report:
  stage: test
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    GIT_DEPTH: "0"   # the whole history, so the merge base below can be read
  script:
    # Install the SWCraft toolchain here, as your other jobs do.
    # GitLab names the merge base itself; GIT_DEPTH above makes sure the clone holds it.
    - swcraft change-report --project swcraft.yaml "$CI_MERGE_REQUEST_DIFF_BASE_SHA" --fail-on error
  after_script:
    - >
      test -f dist/change-report/change-report-pr.md &&
      curl --fail --request POST --header "PRIVATE-TOKEN: $REPORT_TOKEN"
      --data-urlencode "body@dist/change-report/change-report-pr.md"
      "$CI_API_V4_URL/projects/$CI_PROJECT_ID/merge_requests/$CI_MERGE_REQUEST_IID/notes"
  artifacts:
    when: always
    paths:
      - dist/change-report/
```

A forge refuses a comment above its size limit (GitHub's is 65 536 characters), which the *What
changed* table of a very large change can pass; the artifact keeps the whole report either way.
`REPORT_TOKEN` is a project access token you create with the `api` scope and store as a masked CI
variable.

**Azure DevOps**: run the same command in the pull request's pipeline and post
`change-report-pr.md` as a pull request thread comment through its REST API
(`pullRequests/{id}/threads`).

## Through the assistant: what changed { #through-the-assistant-what-changed }

<!-- purpose: How the assistant's model_diff answers what changed between two revisions -->

The assistant answers *what changed* with its `model_diff` tool, which reads the revisions as `swcraft
diff` does: `rev_a` alone (`HEAD` by default) against the working tree, or two revisions against each
other. Its `summary` is `{"response": "diff", "rev_a", "rev_b", "layers", "totals"}`, where `layers`
holds only the layers that changed, each with `added`, `removed` and `changed` as a `count` and their
`identities`; an empty diff has no layer and zero totals. The `full` form is the text `swcraft diff`
prints. An answer too large for one reply is refused, naming the command that prints the diff whole,
and for the `full` text the `summary` too.

## What it does not reach { #what-it-does-not-reach }

<!-- purpose: What the Change Report cannot tell you, so you know where its answer ends -->

- **Both revisions are read with today's manifest.** The report uses the `swcraft.yaml` of your
  working tree for both sides — its ARXML version and its `dist_root`.
- **A finding is compared by its check and its message.** A finding whose message names an element
  the change renamed, or carries a count the change moved, is listed as introduced although the model
  had its like before.
- **A line the overview does not draw moves no picture.** A delegation, or a connector the diagrams
  report as not drawn, is in *What changed* only.
- **The pictures need Node.js.** Without it the report is written without them and says why; the
  command prints the same reason as a `Warning:` line.
- **The ARXML part compares elements, not meaning.** It lists the elements whose written form differs;
  whether the new form is right is `swcraft validate`'s question.
- **The files are replaced one after another.** If the system refuses to replace one of them midway
  (a file held open on Windows), the folder can hold a mix of the two runs; run it again.
