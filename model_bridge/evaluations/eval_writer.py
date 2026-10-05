import json
from pathlib import Path


# Saves scored cases as JSONL and their aggregate summary as JSON.
def write_results(
    output_dir: Path,
    *,
    run_id: str,
    cases: list[dict],
    summary: dict,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / f"{run_id}.jsonl").open("w", encoding="utf-8") as result_file:
        for case in cases:
            result_file.write(json.dumps(case, ensure_ascii=False) + "\n")
    (output_dir / f"{run_id}-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
