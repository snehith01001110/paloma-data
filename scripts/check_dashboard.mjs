import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../site/index.html", import.meta.url), "utf8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);

if (!scriptMatch) {
  throw new Error("site/index.html must contain one inline dashboard script");
}

// Compile without running so a syntax error cannot reach GitHub Pages.
new Function(scriptMatch[1]);

const declaredIds = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
const duplicateIds = declaredIds.filter(
  (id, index) => declaredIds.indexOf(id) !== index,
);
const idSet = new Set(declaredIds);
const referencedIds = [...scriptMatch[1].matchAll(/getElementById\(['"]([^'"]+)['"]\)/g)]
  .map((match) => match[1]);
const missingIds = [...new Set(referencedIds.filter((id) => !idSet.has(id)))];

if (duplicateIds.length || missingIds.length) {
  throw new Error(JSON.stringify({ duplicateIds, missingIds }));
}

console.log(
  `Dashboard contract OK: ${idSet.size} IDs, ${new Set(referencedIds).size} referenced`,
);
