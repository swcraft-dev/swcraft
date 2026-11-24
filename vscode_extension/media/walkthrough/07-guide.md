# The guide is already on your machine

**Open Guide**, in the `…` at the top of the SWCraft view (or `SWCraft: Open Guide` in the Command
Palette), serves the SWCraft user guide from your own install and shows it inside VS Code, in an
editor tab. (VS Code shows that `…` when the pointer is over the SWCraft view or the view has
focus.) Nothing is downloaded, and there is no address to remember:

- **You stay in the editor.** The guide opens in an editor tab beside your model; the tab's
  toolbar can move it to your own browser when you want the full window. When VS Code cannot show
  it in a tab, it opens in your default browser instead.
- **Search and navigation work**, because the pages are served rather than opened as files. The
  same folder opened straight off disk has neither — its search index cannot load and every
  internal link degrades to a folder listing.
- **It describes the release you have installed.** The guide ships *with* SWCraft, so it cannot
  drift away from the commands on your machine the way a bookmarked site can.
- **It needs no network at all**, which makes it the copy that still works on a locked-down or
  disconnected machine.

The command line does exactly the same thing: `swcraft docs`. Open Guide runs it with the
browser-opening left to VS Code, which is what makes it work in a Remote SSH, WSL or Codespaces
window — there the guide is served on the remote machine and VS Code forwards the address, so the
tab in front of you shows it.

## One server per window

The first Open Guide starts the little server; every one after that reuses it and simply opens
the address again. It stops on its own when you close the window, and there is nothing to shut down
by hand.

## If it says there is no guide to serve

Some installs carry no guide payload — a partial install, or a copy assembled by hand. The
message names the folder it looked in and the release archive to unpack, and the full text is in
the guide's own **SWCraft Guide** output channel, which opens by itself on that path:

> This copy of SWCraft carries no user guide to serve.

Reinstalling the `swcraft-tools` package from a release is the ordinary fix. Otherwise take the
`swcraft-guide-<version>.zip` from the release, unpack it anywhere, set `SWCRAFT_GUIDE_SITE` to
the unpacked folder and choose Open Guide again.

## What the guide is, and what it is not

It is the reference for describing an architecture with SWCraft: the model layers field by field,
the rules the loader enforces, every CLI command and every tool the agent can reach.

**No page of the guide is qualification evidence.** It explains what the tools do; `swcraft
validate` is the sole authoritative gate for the model and for the ARXML check, and nothing you
read on a served page changes what that gate says.
