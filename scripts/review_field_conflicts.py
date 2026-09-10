"""Apply a batch of field conflict reviews from the catalog workflow.

The queue is worked in batches, and one dispatch per conflict would serialise
behind the workflow's concurrency group for as long as the batch is large.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: review_field_conflicts.py <reviews.json> <actor>", file=sys.stderr)
        return 2
    path, actor = sys.argv[1], sys.argv[2]

    reviews = json.load(open(path))
    if not isinstance(reviews, list) or not reviews:
        print("reviews must be a non-empty JSON array", file=sys.stderr)
        return 2

    for review in reviews:
        missing = {"conflict_id", "city", "notes"} - review.keys()
        if missing:
            print(f"review is missing {sorted(missing)}: {review}", file=sys.stderr)
            return 2

    for review in reviews:
        command = [
            "paloma-data",
            "review-field-conflict",
            "--conflict-id",
            str(review["conflict_id"]),
            "--city",
            review["city"],
            # Never a free-text input, so the immutable decision trail records who
            # actually made the call and cannot be attributed to someone who did not.
            "--reviewer",
            f"github:{actor}",
            "--notes",
            review["notes"],
            "--confirm",
            "REVIEW_FIELD_CONFLICT",
        ]
        if review.get("evidence_id"):
            command += ["--selected-evidence-id", review["evidence_id"]]
        print(f"::group::field conflict {review['conflict_id']}", flush=True)
        # One failure stops the batch rather than leaving a partial pass behind.
        subprocess.run(command, check=True)
        print("::endgroup::", flush=True)

    print(f"reviewed {len(reviews)} field conflicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
