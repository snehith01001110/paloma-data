"""Apply a batch of candidate match reviews from the catalog workflow.

Identity correlation routes an exact premise with a divergent name to a human, and
that queue is worked in batches rather than one dispatch at a time.
"""

from __future__ import annotations

import json
import subprocess
import sys

RESOLUTIONS = {"same_place", "not_same_or_stale"}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: resolve_match_reviews.py <reviews.json> <actor>", file=sys.stderr)
        return 2
    path, actor = sys.argv[1], sys.argv[2]

    reviews = json.load(open(path))
    if not isinstance(reviews, list) or not reviews:
        print("reviews must be a non-empty JSON array", file=sys.stderr)
        return 2

    for review in reviews:
        missing = {"review_id", "city", "resolution", "note"} - review.keys()
        if missing:
            print(f"review is missing {sorted(missing)}", file=sys.stderr)
            return 2
        if review["resolution"] not in RESOLUTIONS:
            print(f"bad resolution {review['resolution']!r}", file=sys.stderr)
            return 2

    for review in reviews:
        command = [
            "paloma-data",
            "catalog-review-resolve",
            "--review-id",
            str(review["review_id"]),
            "--city",
            review["city"],
            "--resolution",
            review["resolution"],
            # Never a free-text input, so the trail records who actually decided.
            "--reviewer",
            f"github:{actor}",
            "--note",
            review["note"],
            "--confirm",
            "RESOLVE_MATCH_REVIEW",
        ]
        print(f"::group::match review {review['review_id']}", flush=True)
        subprocess.run(command, check=True)
        print("::endgroup::", flush=True)

    print(f"resolved {len(reviews)} match reviews")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
