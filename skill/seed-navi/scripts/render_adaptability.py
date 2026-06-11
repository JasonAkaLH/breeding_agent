from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


COLOR_STOPS = [
    "#d94b43",
    "#f17653",
    "#f7b267",
    "#f4d98d",
    "#f3efaa",
    "#d7ed8e",
    "#a8dc72",
    "#74c67a",
    "#34a96a",
]


def map_artifact_path(path: str, host_runs_dir: Path, container_runs_dir: str) -> Path:
    if path.startswith(container_runs_dir.rstrip("/") + "/"):
        rel = path[len(container_runs_dir.rstrip("/")) + 1 :]
        return host_runs_dir / rel
    return Path(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_reference_html(output: Path) -> Path | None:
    skill_root = Path(__file__).resolve().parents[1]
    candidate = skill_root / "assets" / "adaptability_leaflet_template.html"
    if candidate.exists() and candidate.is_file():
        return candidate
    return None


def render_reference_leaflet_html(
    *,
    result: dict[str, Any],
    grid: dict[str, Any],
    scatter: dict[str, Any],
    output: Path,
) -> str | None:
    reference = find_reference_html(output)
    if reference is None:
        return None

    html = reference.read_text(encoding="utf-8")
    payload_marker = "__SEED_NAVI_PAYLOAD__"
    if payload_marker not in html:
        return None
    if "const data = {" in html:
        raise RuntimeError("Seed Navi HTML template contains embedded sample data; refusing to render report.")

    payload_json = json.dumps(
        {
            "summary": result.get("summary", {}),
            "grid": grid,
            "scatter": scatter,
            "colors": COLOR_STOPS,
        },
        ensure_ascii=False,
    )
    return html.replace(payload_marker, payload_json, 1)


def render_html(result: dict[str, Any], grid: dict[str, Any], scatter: dict[str, Any], output: Path) -> str:
    reference_html = render_reference_leaflet_html(result=result, grid=grid, scatter=scatter, output=output)
    if reference_html is None:
        raise RuntimeError("Seed Navi built-in HTML template asset is missing; cannot render report.")
    return reference_html


def render_result(result_json: Path, output: Path, host_runs_dir: Path, container_runs_dir: str) -> Path:
    result = read_json(result_json)
    files = result.get("files") or {}
    # Modern Seed Navi stores downloaded BreedCore artifacts in the skill output directory.
    # The host/container mapping is kept only for legacy result JSON files.
    grid_path = map_artifact_path(files["grid_geojson"], host_runs_dir, container_runs_dir)
    scatter_path = map_artifact_path(files["scatter_geojson"], host_runs_dir, container_runs_dir)
    grid = read_json(grid_path)
    scatter = read_json(scatter_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(result, grid, scatter, output), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Seed Navi adaptability result JSON to a self-contained HTML report.")
    parser.add_argument("--result-json", required=True, help="BreedCore /jobs/adaptability result JSON file.")
    parser.add_argument("--output", required=True, help="Output HTML path.")
    parser.add_argument("--host-runs-dir", default="runtime/breedcore/runs", help="Legacy host path mapped to /work/runs.")
    parser.add_argument("--container-runs-dir", default="/work/runs", help="Legacy container runs directory prefix.")
    args = parser.parse_args()

    output = render_result(
        Path(args.result_json),
        Path(args.output),
        Path(args.host_runs_dir),
        args.container_runs_dir,
    )
    print(output)


if __name__ == "__main__":
    main()
