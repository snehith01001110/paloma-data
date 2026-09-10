"""Record a batch of manual establishment observations from the catalog workflow.

Facts a person verified online -- a closure a consumer source has not caught up to,
say -- have no other route into the ledger from CI.
"""

from __future__ import annotations

import json
import subprocess
import sys

REQUIRED = {"establishment_id", "city", "field_name", "value", "notes", "evidence_urls"}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: observe_establishment_fields.py <observations.json> <actor>", file=sys.stderr)
        return 2
    path, actor = sys.argv[1], sys.argv[2]

    observations = json.load(open(path))
    if not isinstance(observations, list) or not observations:
        print("observations must be a non-empty JSON array", file=sys.stderr)
        return 2

    for observation in observations:
        missing = REQUIRED - observation.keys()
        if missing:
            print(f"observation is missing {sorted(missing)}", file=sys.stderr)
            return 2
        if not observation["evidence_urls"]:
            print("every observation needs at least one evidence url", file=sys.stderr)
            return 2

    for observation in observations:
        command = [
            "paloma-data",
            "observe-establishment-field",
            "--establishment-id",
            observation["establishment_id"],
            "--city",
            observation["city"],
            "--field-name",
            observation["field_name"],
            "--value",
            observation["value"],
            # Never a free-text input, so the ledger records who actually observed this.
            "--reviewer",
            f"github:{actor}",
            "--notes",
            observation["notes"],
            "--confirm",
            "RECORD_ESTABLISHMENT_OBSERVATION",
        ]
        for url in observation["evidence_urls"]:
            command += ["--evidence-url", url]
        if observation.get("lease_days"):
            command += ["--lease-days", str(observation["lease_days"])]
        print(f"::group::{observation['field_name']} for {observation['establishment_id']}", flush=True)
        subprocess.run(command, check=True)
        print("::endgroup::", flush=True)

    print(f"recorded {len(observations)} establishment observations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
