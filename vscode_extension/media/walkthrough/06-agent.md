# Talk to the model

Two pieces have to be in place, and only one of them is a file. The **MCP server** is registered
by the SWCraft extension itself — for every open folder whose root holds a `swcraft.yaml`, with no
configuration anywhere and nothing to repair. The **agent profile**,
`.github/agents/swcraft-architect.agent.md`, is what **Onboard Project** copies in — or the notice
a project without one gets when the window opens, **Add the SWCraft Architect agent**; VS Code
picks it up from that folder.

## What the checkmark means

The step's **Open Copilot Chat** button opens the Chat view, and that is what ticks it. It does
*not* mean the conversation below happened: the chat and the MCP server are a view SWCraft does not
host and a subprocess VS Code — not this extension — starts, so neither is visible to it. Read on
past the tick.

## Selecting the agent

Open **Copilot Chat**, then use the **chat-mode dropdown** in the chat box — the picker that
chooses between the built-in modes and any custom agent found in `.github/agents/` — and pick
**SWCraft Architect**. This is the step people miss: the built-in modes are general assistants.
Copilot's Agent mode reaches the SWCraft tools too, but beside the terminal and file editing, so a
question whose answer is large can end with it offering to run a script over your model files —
with nothing of SWCraft in between. Ask model questions in SWCraft Architect.

The profile declares `tools: ['swcraft/*', 'view']` and nothing else. `view` is a read-only file
tool, granted for one purpose: Copilot saves a SWCraft answer too large to hand over whole to a
temporary file, and the agent reads it there. The agent has no file-edit, no file-create and no
terminal tool, so it cannot hand-edit a layer file even if asked to. The project's model guard,
`.github/hooks/swcraft.json`, denies a file tool's read of the model directory, so while the guard
runs the agent reads the model only through the `swcraft` MCP server. The guard has limits — it
does not run where `swcraft` is not on your path or hooks are switched off in Copilot — listed
under *The model guard* in the user guide's VS Code page.

## The permission prompts

The first time the agent calls each MCP tool, VS Code asks whether to run it. Answer **Allow in
this Workspace** once per tool and the prompts stop for this project. The server publishes a small
fixed set of tools — a dozen or so — so expect a few prompts early in a first session and none
afterwards.

**That permission is not the review.** It authorizes *running a tool*; it says nothing about the
change the tool computed. Read the next section before you decide it did.

## Where a change is actually reviewed

The extension's server definition sets `SWCRAFT_REVIEW=panel` in the server's environment. That is
the review boundary:

- the agent stages a change set and the server computes the **delta**;
- the server puts that delta to you in a **SWCraft review tab** that opens by itself in this
  window, not in the chat — the reviewed delta is the committed delta;
- only your click on **Approve** commits it; **Reject** abandons it, and the question waits until
  you answer it, the assistant cancels it, or the assistant's session ends. A tab you closed
  unanswered is reopened from **Pending Change Sets** in the SWCraft panel.

The same tab reviews every assistant that uses this project's SWCraft server, `swcraft agent` in a
terminal included — so keep this window open on the project while one works: without it nobody
can answer, and the change set waits.

Without that variable the server starts in its fail-closed `stage-only` default: the agent can
stage a change set and nothing it stages is ever applied. The extension always sets it, so the
case to look at instead is a **second** SWCraft server you configured by hand, in your own MCP
configuration or user settings — that one carries whatever environment you gave it.

One caveat worth stating rather than leaving you to find: VS Code *merges* a definition's
environment over the extension host's own. SWCraft sets exactly two keys, `SWCRAFT_REVIEW` and
`PYTHONUTF8` — the second because a Windows Python writes a piped stdout in the locale encoding
without it, and an MCP server's whole conversation is a pipe. Those two are not the only keys the
server ends up with, and no provider can make them so.

## How it works, in three tools

| Tool | What it does |
|---|---|
| `discover` | what this model *can* express — every layer, kind and field, derived from the schema — plus the **baseline token** |
| `read` | what it currently *holds*, addressed by layer, element and path |
| `change_set` | the **only** way to change anything, and it must carry a current baseline token |

A change set stated against a stale baseline is refused rather than re-based onto a state its
author never saw. A refusal here is the product working.

## Without Copilot Chat

Nothing else changes. The SWCraft panel, every command in it and the CLI are unaffected — the MCP
server is a separate process that VS Code starts, not part of the extension. The same server is
also reachable outside the editor:

```sh
swcraft mcp-serve --project swcraft.yaml     # export SWCRAFT_REVIEW=panel yourself
swcraft agent chat --project swcraft.yaml
```

## And the standing rule

The agent is not a gate. Whatever it reports, **`swcraft validate` is the authoritative gate** —
run it, or **Build**, which ends with it, after a change set commits.
