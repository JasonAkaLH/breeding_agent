from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

R_ENV = {
    "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Library/Frameworks/R.framework/Resources/bin",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
}


def find_rscript() -> str:
    for candidate in (
        shutil.which("Rscript"),
        "/usr/local/bin/Rscript",
        "/opt/homebrew/bin/Rscript",
        "/Library/Frameworks/R.framework/Resources/bin/Rscript",
        "/usr/bin/Rscript",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise RuntimeError("Rscript is not available in the backend runtime")


def parse_blocks(payload: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> int | None:
    metadata = metadata or {}
    for source in (payload, metadata):
        value = source.get("blocks") or source.get("reps") or source.get("replications") or source.get("重复数") or source.get("区组数")
        parsed = positive_int(value)
        if parsed is not None:
            return parsed
    query = str(payload.get("query") or "")
    patterns = (
        r"(?:blocks?|区组数|区组|重复数|重复|reps?|replications?)\s*[:：=]?\s*(\d+)",
        r"(\d+)\s*(?:个|次)?(?:区组|重复|rep|reps|blocks?)",
    )
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            parsed = positive_int(match.group(1))
            if parsed is not None:
                return parsed
    return None


def positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def boolean_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y", "on", "是", "开启", "独立", "独立随机"}:
        return True
    if text in {"false", "f", "0", "no", "n", "off", "否", "关闭"}:
        return False
    return default


def string_param(payload: Mapping[str, Any], metadata: Mapping[str, Any], *names: str) -> str | None:
    for source in (payload, metadata):
        for name in names:
            value = source.get(name)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
    return None


def int_param(payload: Mapping[str, Any], metadata: Mapping[str, Any], *names: str, default: int | None = None) -> int | None:
    for source in (payload, metadata):
        for name in names:
            parsed = positive_int(source.get(name))
            if parsed is not None:
                return parsed
    return default


def parse_planter(payload: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    value = string_param(payload, metadata, "planter", "种植路径", "排布方式")
    query = str(payload.get("query") or "")
    if value is None:
        if re.search(r"cartesian|直行|逐行|网格", query, flags=re.IGNORECASE):
            value = "cartesian"
        elif re.search(r"serpentine|蛇形|往返", query, flags=re.IGNORECASE):
            value = "serpentine"
    value = (value or "serpentine").strip().lower()
    aliases = {"蛇形": "serpentine", "往返": "serpentine", "直行": "cartesian", "逐行": "cartesian", "网格": "cartesian"}
    value = aliases.get(value, value)
    if value not in {"serpentine", "cartesian"}:
        raise ValueError("planter must be 'serpentine' or 'cartesian'")
    return value


def parse_site_random(payload: Mapping[str, Any], metadata: Mapping[str, Any]) -> bool:
    value = string_param(payload, metadata, "site_random", "site-random", "多站点独立随机", "独立随机化")
    if value is not None:
        return boolean_value(value, default=False)
    query = str(payload.get("query") or "")
    if re.search(r"多站点.*独立随机|独立随机化|site[-_ ]?random", query, flags=re.IGNORECASE):
        return True
    return False


def first_uploaded_content(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    artifacts = payload.get("uploaded_artifacts")
    if not isinstance(artifacts, list | tuple):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        content = artifact.get("content")
        if content is None:
            continue
        filename = str(artifact.get("filename") or "materials.csv")
        return filename, str(content)
    return None


def material_from_metadata(metadata: Mapping[str, Any]) -> Any | None:
    for key in ("input_data", "material_data", "materials", "data"):
        value = metadata.get(key)
        if value is not None:
            return value
    return None


def write_material_input(payload: Mapping[str, Any], metadata: Mapping[str, Any], workdir: Path) -> Path | None:
    uploaded = first_uploaded_content(payload)
    if uploaded is not None:
        filename, content = uploaded
        suffix = ".json" if filename.lower().endswith(".json") or content.lstrip().startswith(("[", "{")) else ".csv"
        path = workdir / f"materials{suffix}"
        path.write_text(content, encoding="utf-8")
        return path

    value = material_from_metadata(metadata)
    if value is None:
        return None
    if isinstance(value, str):
        suffix = ".json" if value.lstrip().startswith(("[", "{")) else ".csv"
        path = workdir / f"materials{suffix}"
        path.write_text(value, encoding="utf-8")
        return path
    path = workdir / "materials.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def run_process(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=R_ENV,
    )


def load_json_result(path: Path, stdout: str) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8") if path.exists() else stdout
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("R script stdout must be a JSON object")
    return parsed


def rows_count(result: Mapping[str, Any]) -> int:
    out_design = result.get("out_design")
    if isinstance(out_design, list):
        return len(out_design)
    if isinstance(result.get("results"), list):
        total = 0
        for item in result["results"]:
            if isinstance(item, Mapping) and isinstance(item.get("out_design"), list):
                total += len(item["out_design"])
        return total
    return 0


def sets_summary(result: Mapping[str, Any]) -> list[str]:
    params = result.get("parameters")
    if isinstance(params, Mapping) and isinstance(params.get("sets"), list):
        return [str(item) for item in params["sets"]]
    out_design = result.get("out_design")
    if isinstance(out_design, list):
        values = sorted({str(row.get("set")) for row in out_design if isinstance(row, Mapping) and row.get("set") is not None})
        return values
    return []


def missing_input(missing: list[str]) -> dict[str, Any]:
    labels = {"material_data": "材料清单文件", "blocks": "重复数/区组数"}
    human = "、".join(labels.get(item, item) for item in missing)
    return {
        "ok": False,
        "answer": f"还不能生成 RCBD 设计，缺少：{human}。请补充后我再继续。",
        "error": {"type": "missing_input", "message": f"Missing required inputs: {', '.join(missing)}"},
        "missing": missing,
    }


def build_success_answer(result: Mapping[str, Any], *, layout_generated: bool) -> str:
    params = result.get("parameters") if isinstance(result.get("parameters"), Mapping) else {}
    blocks = params.get("blocks") or "未返回"
    planter = params.get("planter") or "serpentine"
    seed = params.get("seed") or params.get("base_seed") or "未返回"
    row_count = rows_count(result)
    sets = sets_summary(result)
    layout_text = "已生成 HTML 田间布局预览。" if layout_generated else "未生成 HTML 田间布局预览。"
    set_text = "、".join(sets) if sets else "未返回"
    return f"RCBD 设计已完成：共 {row_count} 行 fieldbook，blocks={blocks}，planter={planter}，seed={seed}，set={set_text}。{layout_text}"


def main() -> dict[str, Any]:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return {"ok": False, "answer": "脚本输入必须是 JSON object。", "error": {"type": "invalid_input"}}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}

    blocks = parse_blocks(payload, metadata)
    with tempfile.TemporaryDirectory(prefix="mini-rcbd-") as tmp:
        workdir = Path(tmp)
        input_path = write_material_input(payload, metadata, workdir)
        missing: list[str] = []
        if input_path is None:
            missing.append("material_data")
        if blocks is None:
            missing.append("blocks")
        if missing:
            return missing_input(missing)

        planter = parse_planter(payload, metadata)
        seed = int_param(payload, metadata, "seed", "随机种子")
        site_num = int_param(payload, metadata, "site_num", "site-num", "站点数", default=1) or 1
        site_random = parse_site_random(payload, metadata)
        check_constraint = boolean_value(string_param(payload, metadata, "check_position_constraint", "check-position-constraint"), default=True)
        test_constraint = boolean_value(string_param(payload, metadata, "test_position_constraint", "test-position-constraint"), default=True)

        scripts_dir = Path(__file__).resolve().parent
        rscript = find_rscript()
        result_path = workdir / "rcbd_result.json"
        cmd = [
            rscript,
            str(scripts_dir / "run_rcbd_local.R"),
            "--input",
            str(input_path),
            "--blocks",
            str(blocks),
            "--output",
            str(result_path),
            "--planter",
            planter,
            "--site-num",
            str(site_num),
            "--site-random",
            "true" if site_random else "false",
            "--check-position-constraint",
            "true" if check_constraint else "false",
            "--test-position-constraint",
            "true" if test_constraint else "false",
        ]
        if seed is not None:
            cmd.extend(["--seed", str(seed)])

        process = run_process(cmd, timeout=45)
        try:
            result = load_json_result(result_path, process.stdout)
        except Exception as exc:
            return {
                "ok": False,
                "answer": "RCBD 设计未完成：R 脚本没有返回有效 JSON。",
                "error": {"type": "invalid_r_output", "message": str(exc), "stderr": process.stderr[-1000:]},
            }
        if process.returncode != 0 or result.get("ok") is False:
            message = "RCBD 设计未完成。"
            error = result.get("error") if isinstance(result.get("error"), Mapping) else {}
            if error.get("message"):
                message = f"RCBD 设计未完成：{error['message']}"
            result["answer"] = message
            return result

        outputs_root_raw = os.environ.get("MAF_SKILL_OUTPUT_DIR")
        layout_generated = False
        if outputs_root_raw:
            outputs_root = Path(outputs_root_raw)
            outputs_root.mkdir(parents=True, exist_ok=True)
            output_json_path = outputs_root / "rcbd_result.json"
            output_json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            layout_path = outputs_root / "rcbd_layout.html"
            render = run_process(
                [
                    rscript,
                    str(scripts_dir / "render_rcbd_layout_html.R"),
                    "--input",
                    str(output_json_path),
                    "--output",
                    str(layout_path),
                    "--title",
                    "RCBD Field Layout",
                ],
                timeout=15,
            )
            layout_generated = render.returncode == 0 and layout_path.exists()
            if layout_generated:
                result["output_files"] = [
                    {
                        "path": "outputs/rcbd_layout.html",
                        "filename": "rcbd_layout.html",
                        "mime_type": "text/html",
                        "label": "RCBD 田间布局 HTML",
                        "summary": "按 ranges/pass 绘制的交互式田间布局预览。",
                    }
                ]
            else:
                result["layout_error"] = (render.stderr or render.stdout)[-1000:]

        result["answer"] = build_success_answer(result, layout_generated=layout_generated)
        result["layout_html_generated"] = layout_generated
        result["fieldbook_row_count"] = rows_count(result)
        return result


if __name__ == "__main__":
    try:
        output = main()
    except Exception as exc:  # Return structured JSON so the Skill runner can inject a useful failure.
        output = {
            "ok": False,
            "answer": f"RCBD 设计未完成：{exc}",
            "error": {"type": "wrapper_error", "message": str(exc)},
        }
    print(json.dumps(output, ensure_ascii=False))
