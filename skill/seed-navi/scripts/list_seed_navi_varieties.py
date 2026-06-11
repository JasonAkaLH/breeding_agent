from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from run_seed_navi import (
    SUPPORTED_EXTENSIONS,
    candidate_varieties,
    failure,
    format_candidate_varieties,
    local_variety_result,
    resolve_input_file,
    visible_text_fields,
)


def run_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    work_dir = Path(".").resolve()
    input_file = resolve_input_file(payload, work_dir)
    if input_file is None:
        return failure("请提供玉米品种试验 Excel 或 CSV 文件。", missing=["trial_file"], error_type="missing_input")
    if input_file.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return failure("Seed Navi 目前只支持 .xlsx、.xls 或 .csv 试验文件。", error_type="unsupported_file")

    control_variety = str(payload.get("control_variety") or payload.get("control") or payload.get("ck") or "CK").strip() or "CK"
    top_n = int(payload.get("top_n") or 20)
    result = local_variety_result(input_file, control_variety, top_n)
    if not result.get("ok"):
        return failure(
            "未能在试验表中识别品种列。请确认表格包含品种测试名/品种/Variety/Name 等列，或直接提供目标品种。",
            missing=["target_variety"],
            error_type="local_variety_list_failed",
            diagnostics={"local_variety_result": result},
    )

    candidates = candidate_varieties(result)
    missing = ["target_variety"]
    answer = (
        "已在 skill 层本地识别试验表中的候选品种。当前分析生态区：东北中晚熟区。请选择一个目标品种。\n\n"
        f"{format_candidate_varieties(candidates)}"
    )
    return {
        "ok": False,
        "needs_user_input": True,
        **visible_text_fields(answer),
        "missing": missing,
        "candidate_varieties": candidates,
        "structured_content": {
            "candidate_varieties": candidates,
            "variety_detection": result.get("summary"),
            "next_required_fields": missing,
        },
        "variety_detection": result.get("summary"),
    }


def main() -> int:
    if not sys.stdin.isatty():
        try:
            payload = json.load(sys.stdin)
        except Exception:
            print(json.dumps(failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"), ensure_ascii=False))
            return 0
        print(json.dumps(run_from_payload(payload), ensure_ascii=False, separators=(",", ":")))
        return 0

    parser = argparse.ArgumentParser(description="List Seed Navi candidate varieties from a local trial Excel/CSV file.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--control-variety", default="CK")
    parser.add_argument("--region", default=None)
    parser.add_argument("--top-n", type=int, default=20)
    args = parser.parse_args()
    payload = {
        "file_path": args.input,
        "control_variety": args.control_variety,
        "region": args.region,
        "top_n": args.top_n,
    }
    print(json.dumps(run_from_payload(payload), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
