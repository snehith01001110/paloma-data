"""Apply a batch of manual hard-gate attestations from the catalog workflow.

A venue whose ABC licence is the wrong class for the open-evidence shortcut can only
clear verification on a reviewed attestation, and that had no route from CI.
"""

from __future__ import annotations

import json
import subprocess
import sys

REQUIRED = {"candidate_id", "city", "note", "evidence_urls"}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: attest_candidates.py <attestations.json> <actor>", file=sys.stderr)
        return 2
    path, actor = sys.argv[1], sys.argv[2]

    attestations = json.load(open(path))
    if not isinstance(attestations, list) or not attestations:
        print("attestations must be a non-empty JSON array", file=sys.stderr)
        return 2

    for item in attestations:
        missing = REQUIRED - item.keys()
        if missing:
            print(f"attestation is missing {sorted(missing)}", file=sys.stderr)
            return 2
        if not item["evidence_urls"]:
            print("every attestation needs at least one evidence url", file=sys.stderr)
            return 2
        if item.get("outcome", "pass") not in {"pass", "fail"}:
            print(f"bad outcome {item['outcome']!r}", file=sys.stderr)
            return 2

    for item in attestations:
        command = [
            "paloma-data",
            "catalog-attest",
            "--candidate-id",
            item["candidate_id"],
            "--city",
            item["city"],
            "--outcome",
            item.get("outcome", "pass"),
            # Never a free-text input, so the attestation trail names who actually vouched.
            "--reviewer",
            f"github:{actor}",
            "--note",
            item["note"],
            "--confirm",
            "MANUAL_ATTESTATION",
        ]
        for url in item["evidence_urls"]:
            command += ["--evidence-url", url]
        if item.get("venue_type"):
            command += ["--venue-type", item["venue_type"]]
        print(f"::group::attest {item['candidate_id']}", flush=True)
        subprocess.run(command, check=True)
        print("::endgroup::", flush=True)

    print(f"attested {len(attestations)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
