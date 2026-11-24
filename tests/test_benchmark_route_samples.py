import json
from pathlib import Path


def test_route_sample_references_exist():
    path = Path(__file__).parents[1] / "docs" / "benchmarks" / "routes" / "samples.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for sample in payload["samples"]:
        reference = sample.get("reference")
        if reference:
            assert (path.parent / reference).resolve().is_file(), reference
