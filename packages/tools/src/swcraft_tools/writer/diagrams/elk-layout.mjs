// SWCraft's diagram layout bridge: the one Node program the Python generator runs.
//
// stdin:  one JSON array of ELK graphs.
// stdout: an empty line, the line `swcraft-diagram-layout-answer`, and one line holding
//         the JSON array of the same graphs laid out by Eclipse ELK, in the same order,
//         written in ASCII (every character from U+007F up as a \uXXXX escape).
// A failure writes "graph <index>: <ELK's message>" (or what could not be read) to
// stderr and exits 1 with nothing on stdout.
//
// Its one dependency is the elkjs payload staged beside it; it reads no file, opens no
// port and imports nothing else. Called by swcraft_tools/writer/diagrams/layout.py,
// which owns the argv, the Node floor and the rounding of what comes back.

import ELK from "./elkjs/lib/main.js";

// The line the answer follows; layout.py's ANSWER_MARKER, which a test holds equal.
const ANSWER_MARKER = "swcraft-diagram-layout-answer";

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
}

const chunks = [];
for await (const chunk of process.stdin) {
  chunks.push(chunk);
}

let graphs;
try {
  graphs = JSON.parse(Buffer.concat(chunks).toString("utf8"));
} catch (error) {
  fail(`the layout input is not JSON: ${error.message}`);
}

if (graphs !== undefined && !Array.isArray(graphs)) {
  fail("the layout input is not a JSON array of graphs");
} else if (graphs !== undefined) {
  const elk = new ELK();
  const laidOut = [];
  for (const [index, graph] of graphs.entries()) {
    try {
      laidOut.push(await elk.layout(graph));
    } catch (error) {
      fail(`graph ${index}: ${error instanceof Error ? error.message : String(error)}`);
      break;
    }
  }
  if (process.exitCode !== 1) {
    // JSON.stringify leaves every character from U+007F up as it is, U+0085, U+2028 and
    // U+2029 among them, and model text can hold any of those. Escaping them all makes
    // the answer one ASCII line that no reader can split, and the marker line before it
    // tells that line from whatever else a PATH shim prints before or after.
    const answer = JSON.stringify(laidOut).replace(
      /[\u007F-\uFFFF]/g,
      (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
    );
    process.stdout.write(`\n${ANSWER_MARKER}\n${answer}\n`);
  }
}
