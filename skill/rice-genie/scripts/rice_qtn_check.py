#!/usr/bin/env python
"""Extract rice QTN calls from VCF and summarize phenotype annotations."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_QTN_PATH = Path(__file__).resolve().parents[1] / "assets" / "202507_RiceQTNs.sqlite"

CATALOG_MATRIX_SCHEMA_NOTE = {
    "format": "catalog_matrix",
    "description": (
        "qtn_catalog stores fixed QTN annotations once. "
        "samples.{sample}.calls stores per-sample call states. "
        "Reconstruct a full row by joining qtn_catalog[i] with samples.{sample}.calls[i]."
    ),
    "row_alignment": "The row index is the stable join key between qtn_catalog and every sample calls array.",
    "field_locations": {
        "qtn_catalog": "metadata.qtn_catalog_fields",
        "sample_calls": "metadata.sample_call_fields",
    },
}

REVIEW_NOTE_CODES = {
    "ABSENT": "QTN position absent from VCF",
    "MULTI_ALT": "multi-ALT site",
    "INDEL_GT": "indel represented with VCF padding/symbolic allele; genotype type inferred from GT",
    "GT_MISSING": "sample genotype is missing",
    "GT_COMPLEX": "sample genotype is complex",
}

OUTPUT_COLUMNS = [
    "sample",
    "qtn_id",
    "gene_name",
    "chr",
    "pos",
    "vcf_ref",
    "vcf_alt",
    "qtn_ref_genotype",
    "qtn_alt_genotype",
    "gt",
    "observed_genotype",
    "sample_genotype_type",
    "检测材料基因型",
    "call_class",
    "match_status",
    "detected_variant",
    "favorable_detected_variant",
    "trait_type",
    "phenotype",
    "regulation_direction",
    "favorable_label_cn",
    "favorable_label_en",
    "qual",
    "filter",
    "dp",
    "gq",
    "review_note",
]

RESULT_COLUMNS = [
    "qtn_id",
    "gene_name",
    "chr",
    "pos",
    "sample_genotype_type",
    "favorable_detected_variant",
    "trait_type",
    "phenotype",
    "regulation_direction",
    "favorable_label_cn",
    "favorable_label_en",
    "review_note",
]

QTN_CATALOG_COLUMNS = [
    "qtn_id",
    "gene_name",
    "chr",
    "pos",
    "trait_type",
    "phenotype",
    "regulation_direction",
    "favorable_label_cn",
    "favorable_label_en",
]

SAMPLE_CALL_COLUMNS = [
    "sample_genotype_type",
    "favorable_detected_variant",
    "review_note",
]

def norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_chr(value: str) -> str:
    text = norm(value)
    if not text:
        return text
    lower = text.lower()
    if lower.startswith("chr"):
        suffix = text[3:]
    else:
        suffix = text
    return "Chr" + suffix


def open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def parse_info(info_text: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    if not info_text or info_text == ".":
        return info
    for item in info_text.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            info[key] = value
        else:
            info[item] = "true"
    return info


def parse_vcf(path: Path) -> Tuple[List[str], Dict[Tuple[str, int], dict]]:
    samples: List[str] = []
    records: Dict[Tuple[str, int], dict] = {}
    with open_text(path) as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith("##"):
                continue
            if line.startswith("#CHROM"):
                columns = line.split("\t")
                samples = columns[9:]
                continue

            parts = line.split("\t")
            if len(parts) < 8:
                continue
            chrom, pos_text, _id, ref, alt, qual, filt, info_text = parts[:8]
            pos = int(pos_text)
            fmt_keys = parts[8].split(":") if len(parts) > 8 else []
            sample_values = {}
            for sample, raw_value in zip(samples, parts[9:]):
                values = raw_value.split(":")
                sample_values[sample] = {
                    key: values[i] if i < len(values) else ""
                    for i, key in enumerate(fmt_keys)
                }
                sample_values[sample]["_raw"] = raw_value
            records[(normalize_chr(chrom), pos)] = {
                "chrom": normalize_chr(chrom),
                "pos": pos,
                "ref": ref,
                "alt": alt,
                "qual": qual,
                "filter": filt,
                "info": parse_info(info_text),
                "format_keys": fmt_keys,
                "samples": sample_values,
            }
    return samples, records


def load_qtn_rows(path: Path = DEFAULT_QTN_PATH) -> List[dict]:
    query = """
        SELECT
            source_sheet AS sheet,
            source_row,
            qtn_id,
            gene_name,
            trait_category,
            gene_id_msu7,
            gene_id_rap,
            alt_allele_function,
            gene_description,
            chr,
            pos,
            ref_genotype,
            alt_genotype,
            alt_reg_direction,
            trait_type,
            phenotype,
            regulation_direction,
            favorable_cn,
            favorable_en,
            reference
        FROM qtn_reference
        ORDER BY row_order, qtn_id
    """

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(query)]

    qtn_rows: List[dict] = []
    for row in rows:
        row["chr"] = normalize_chr(row.get("chr", ""))
        row["pos"] = int(row["pos"])
        for key, value in list(row.items()):
            if value is None:
                row[key] = ""
        qtn_rows.append(row)
    return qtn_rows


def parse_gt(gt: str, ref: str, alt: str) -> Tuple[str, str, List[str]]:
    if not gt or gt in {".", "./.", ".|."}:
        return "missing", "", []
    separator = "/" if "/" in gt else "|"
    tokens = gt.split(separator)
    alt_values = [] if alt in {"", "."} else alt.split(",")
    allele_lookup = [""] + alt_values
    allele_lookup[0] = ref
    observed: List[str] = []
    for token in tokens:
        if token == ".":
            observed.append("")
            continue
        try:
            idx = int(token)
        except ValueError:
            observed.append("")
            continue
        observed.append(allele_lookup[idx] if idx < len(allele_lookup) else "")

    if any(not allele for allele in observed):
        call_class = "missing"
    elif all(allele == ref for allele in observed):
        call_class = "homo_ref"
    elif len(set(observed)) == 1 and observed[0] != ref:
        call_class = "homo_alt"
    elif ref in observed:
        call_class = "het"
    else:
        call_class = "complex"
    return call_class, separator.join(observed), observed


def matches_genotype(observed: Sequence[str], expected: str) -> bool:
    expected = norm(expected)
    if not expected or not observed or any(not item for item in observed):
        return False
    observed_set = set(observed)
    if len(observed_set) == 1 and next(iter(observed_set)) == expected:
        return True
    return expected in observed_set


def is_symbolic_allele(value: str) -> bool:
    text = norm(value)
    return text.startswith("<") and text.endswith(">")


def is_special_alt(value: str) -> bool:
    text = norm(value)
    return text == "*" or is_symbolic_allele(text)


def is_indel_marker(qtn: dict, record: dict) -> bool:
    alleles = [
        qtn.get("ref_genotype", ""),
        qtn.get("alt_genotype", ""),
        record.get("ref", ""),
    ]
    alleles.extend([] if record.get("alt", ".") in {"", "."} else record.get("alt", "").split(","))
    for allele in alleles:
        text = norm(allele)
        if not text or text in {".", "NA", "Ref"}:
            continue
        if text == "*" or is_symbolic_allele(text) or len(text) != 1:
            return True
    return False


def gt_category(gt: str) -> str:
    if not gt or gt in {".", "./.", ".|."}:
        return "missing"
    tokens = re.split(r"[\/|]", gt)
    if any(token == "." for token in tokens):
        return "missing"
    try:
        allele_indexes = [int(token) for token in tokens]
    except ValueError:
        return "missing"
    if all(index == 0 for index in allele_indexes):
        return "wild"
    if len(set(allele_indexes)) > 1:
        return "heterozygous"
    return "mutant"


def genotype_type_cn(match_status: str, call_class: str, genotype_category: str = "") -> str:
    if genotype_category == "wild" or match_status == "matches_reference_genotype":
        return "野生型"
    if genotype_category == "mutant" or match_status == "matches_variant_genotype":
        return "突变型"
    if genotype_category == "heterozygous" or match_status == "heterozygous_indel" or call_class == "het":
        return "杂合型"
    if genotype_category == "missing" or match_status in {"missing", "not_in_vcf"} or call_class == "missing":
        return "缺失型"
    return "未知"


def is_favorable(label_cn: str, label_en: str) -> bool:
    text = f"{label_cn} {label_en}".lower()
    favorable_tokens = ["有利", "优良", "superior", "favorable", "beneficial"]
    uncertain_tokens = ["视情况", "unknown", "context", "na", "n/a"]
    return any(token in text for token in favorable_tokens) and not any(
        token in text for token in uncertain_tokens
    )


def call_one(qtn: dict, sample: str, record: Optional[dict]) -> dict:
    row = {
        "sample": sample,
        "qtn_id": qtn["qtn_id"],
        "gene_name": qtn["gene_name"],
        "chr": qtn["chr"],
        "pos": qtn["pos"],
        "vcf_ref": "",
        "vcf_alt": "",
        "qtn_ref_genotype": qtn["ref_genotype"],
        "qtn_alt_genotype": qtn["alt_genotype"],
        "gt": "",
        "observed_genotype": "",
        "sample_genotype_type": "",
        "检测材料基因型": "",
        "call_class": "not_in_vcf",
        "match_status": "not_in_vcf",
        "detected_variant": "0",
        "favorable_detected_variant": "0",
        "trait_type": qtn["trait_type"],
        "phenotype": qtn["phenotype"],
        "regulation_direction": qtn["regulation_direction"],
        "favorable_label_cn": qtn["favorable_cn"],
        "favorable_label_en": qtn["favorable_en"],
        "qual": "",
        "filter": "",
        "dp": "",
        "gq": "",
        "review_note": "",
    }
    if record is None:
        row["review_note"] = "ABSENT"
        return row

    sample_data = record["samples"].get(sample, {})
    gt = sample_data.get("GT", "")
    call_class, observed_text, observed = parse_gt(gt, record["ref"], record["alt"])
    indel_marker = is_indel_marker(qtn, record)
    genotype_category = gt_category(gt)
    ref_match = matches_genotype(observed, qtn["ref_genotype"])
    alt_match = matches_genotype(observed, qtn["alt_genotype"])

    review_notes = []
    if record["alt"] and "," in record["alt"]:
        review_notes.append("MULTI_ALT")
    if indel_marker and (
        is_special_alt(record["alt"])
        or is_special_alt(qtn["alt_genotype"])
        or record["alt"] == "."
        or len(record["ref"]) != 1
        or (record["alt"] not in {"", "."} and any(len(alt) != 1 for alt in record["alt"].split(",")))
    ):
        review_notes.append("INDEL_GT")
    if call_class == "missing":
        review_notes.append("GT_MISSING")
    elif call_class == "complex":
        review_notes.append("GT_COMPLEX")

    if genotype_category == "mutant":
        match_status = "matches_variant_genotype"
    elif genotype_category == "wild":
        match_status = "matches_reference_genotype"
    elif genotype_category == "heterozygous":
        match_status = "heterozygous"
    elif genotype_category == "missing":
        match_status = "missing"
    elif alt_match:
        match_status = "matches_variant_genotype"
    elif ref_match:
        match_status = "matches_reference_genotype"
    elif call_class == "missing":
        match_status = "missing"
    else:
        match_status = "unmatched_genotype"

    sample_genotype_type_cn = genotype_type_cn(match_status, call_class, genotype_category)
    favorable = (
        sample_genotype_type_cn == "突变型"
        and is_favorable(qtn["favorable_cn"], qtn["favorable_en"])
    )
    row.update(
        {
            "vcf_ref": record["ref"],
            "vcf_alt": record["alt"],
            "gt": gt,
            "observed_genotype": observed_text,
            "sample_genotype_type": sample_genotype_type_cn,
            "检测材料基因型": sample_genotype_type_cn,
            "call_class": call_class,
            "match_status": match_status,
            "detected_variant": "1" if alt_match else "0",
            "favorable_detected_variant": "1" if favorable else "0",
            "qual": record["qual"],
            "filter": record["filter"],
            "dp": sample_data.get("DP", record["info"].get("DP", "")),
            "gq": sample_data.get("GQ", sample_data.get("RGQ", "")),
            "review_note": "; ".join(review_notes),
        }
    )
    return row


def summarize(rows: Sequence[dict]) -> List[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["sample"], row["trait_type"] or "未分类")].append(row)

    summary = []
    for (sample, trait_type), items in sorted(grouped.items()):
        total = len(items)
        detected = sum(int(item["detected_variant"]) for item in items)
        favorable = sum(int(item["favorable_detected_variant"]) for item in items)
        review = sum(1 for item in items if item["review_note"])
        examples = []
        for item in items:
            if item["favorable_detected_variant"] == "1":
                examples.append(f"{item['qtn_id']}:{item['gene_name']}:{item['phenotype']}")
            if len(examples) >= 5:
                break
        summary.append(
            {
                "sample": sample,
                "trait_type": trait_type,
                "total_qtn": total,
                "detected_variant_count": detected,
                "favorable_detected_variant_count": favorable,
                "favorable_ratio_of_total": f"{(favorable / total):.4f}" if total else "0",
                "favorable_ratio_of_detected": f"{(favorable / detected):.4f}" if detected else "0",
                "review_needed_count": review,
                "favorable_examples": " | ".join(examples),
            }
        )
    return summary


def compact_calls(rows: Sequence[dict], include_debug_fields: bool = False) -> List[dict]:
    if include_debug_fields:
        return [dict(row) for row in rows]
    return [{column: row.get(column, "") for column in RESULT_COLUMNS} for row in rows]


def qtn_catalog(qtn_rows: Sequence[dict]) -> List[list]:
    return [
        [
            qtn.get("qtn_id", ""),
            qtn.get("gene_name", ""),
            qtn.get("chr", ""),
            qtn.get("pos", ""),
            qtn.get("trait_type", ""),
            qtn.get("phenotype", ""),
            qtn.get("regulation_direction", ""),
            qtn.get("favorable_cn", ""),
            qtn.get("favorable_en", ""),
        ]
        for qtn in qtn_rows
    ]


def sample_call_matrix(rows: Sequence[dict]) -> List[list]:
    return [[row.get(column, "") for column in SAMPLE_CALL_COLUMNS] for row in rows]


def write_csv(path: Path, rows: Sequence[dict], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def is_scalar(value) -> bool:
    return not isinstance(value, (dict, list))


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def format_json(value, indent: int = 0) -> str:
    space = " " * indent
    child_space = " " * (indent + 2)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = list(value.items())
        lines = ["{"]
        for index, (key, item) in enumerate(items):
            comma = "," if index < len(items) - 1 else ""
            lines.append(f"{child_space}{json_text(key)}: {format_json(item, indent + 2)}{comma}")
        lines.append(f"{space}}}")
        return "\n".join(lines)

    if isinstance(value, list):
        if not value:
            return "[]"
        if all(is_scalar(item) for item in value):
            return json_text(value)
        if all(isinstance(item, list) and all(is_scalar(cell) for cell in item) for item in value):
            lines = ["["]
            for index, item in enumerate(value):
                comma = "," if index < len(value) - 1 else ""
                lines.append(f"{child_space}{json_text(item)}{comma}")
            lines.append(f"{space}]")
            return "\n".join(lines)
        lines = ["["]
        for index, item in enumerate(value):
            comma = "," if index < len(value) - 1 else ""
            lines.append(f"{child_space}{format_json(item, indent + 2)}{comma}")
        lines.append(f"{space}]")
        return "\n".join(lines)

    return json_text(value)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_json(data) + "\n", encoding="utf-8")


def safe_filename(value: str) -> str:
    text = norm(value) or "sample"
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = text.strip(" .")
    return text or "sample"


def md_cell(value) -> str:
    text = norm(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def write_report(path: Path, calls: Sequence[dict], summary: Sequence[dict], args) -> None:
    by_sample = defaultdict(list)
    for row in calls:
        by_sample[row["sample"]].append(row)

    lines = [
        "# 水稻体检 QTN 解读报告",
        "",
        "## 输入信息",
        "",
        f"- VCF 文件: `{args.vcf}`",
        "- QTN 参考版本: `fixed_320_qtn_reference`",
        f"- 分析样本数: {len(by_sample)}",
        "",
        "## 样本总体概览",
        "",
        "| 样本 | QTN总数 | 检测到变异基因型 | 有利变异/优良等位基因型 | 占全部QTN比例 | 占检测变异比例 | 需复核 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sample, rows in sorted(by_sample.items()):
        total = len(rows)
        detected = sum(int(row["detected_variant"]) for row in rows)
        favorable = sum(int(row["favorable_detected_variant"]) for row in rows)
        review = sum(1 for row in rows if row["review_note"])
        lines.append(
            f"| {sample} | {total} | {detected} | {favorable} | "
            f"{(favorable / total * 100 if total else 0):.2f}% | "
            f"{(favorable / detected * 100 if detected else 0):.2f}% | {review} |"
        )

    lines.extend(
        [
            "",
            "## 按性状类型汇总",
            "",
            "| 样本 | 性状类型 | QTN总数 | 检测到变异 | 有利变异/优良等位基因型 | 占性状QTN比例 | 表型示例 |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for item in summary:
        ratio = float(item["favorable_ratio_of_total"]) * 100
        lines.append(
            f"| {md_cell(item['sample'])} | {md_cell(item['trait_type'])} | {item['total_qtn']} | "
            f"{item['detected_variant_count']} | {item['favorable_detected_variant_count']} | "
            f"{ratio:.2f}% | {md_cell(item['favorable_examples'])} |"
        )

    lines.extend(["", "## 有利变异/优良等位基因型明细", ""])
    for sample, rows in sorted(by_sample.items()):
        favorable_rows = [row for row in rows if row["favorable_detected_variant"] == "1"]
        lines.extend(
            [
                f"### {sample}",
                "",
                "| QTN | 基因 | 性状类型 | 功能表现型 | 观察基因型 | 有利标记 |",
                "|---|---|---|---|---|---|",
            ]
        )
        if not favorable_rows:
            lines.append("| - | - | - | 未检测到可直接判定的有利变异 | - | - |")
        else:
            for row in favorable_rows:
                lines.append(
                    f"| {md_cell(row['qtn_id'])} | {md_cell(row['gene_name'])} | "
                    f"{md_cell(row['trait_type'])} | {md_cell(row['phenotype'])} | "
                    f"{md_cell(row['observed_genotype'])} | "
                    f"{md_cell(row['favorable_label_cn'] or row['favorable_label_en'])} |"
                )
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vcf", required=True, type=Path)
    parser.add_argument("--outdir", default=Path("outputs"), type=Path)
    parser.add_argument("--out-prefix", default="rice_qtn")
    parser.add_argument("--sample", action="append", help="Sample ID to analyze. Repeat for multiple samples.")
    parser.add_argument("--csv", action="store_true", help="Also write CSV files for tabular inspection.")
    parser.add_argument("--split-samples", action="store_true", help="Also write one qtn_calls JSON file per sample.")
    parser.add_argument("--summary-json", action="store_true", help="Also write a standalone trait_summary JSON file.")
    parser.add_argument("--report-md", action="store_true", help="Also write a Markdown report.")
    parser.add_argument(
        "--include-debug-fields",
        action="store_true",
        help="Keep raw VCF/QTN matching fields in JSON outputs. CSV debug output always keeps these fields.",
    )
    args = parser.parse_args(argv)

    if not DEFAULT_QTN_PATH.exists():
        raise SystemExit(f"QTN reference database not found: {DEFAULT_QTN_PATH}")

    samples, vcf_records = parse_vcf(args.vcf)
    if not samples:
        raise SystemExit("No sample columns found in VCF.")
    selected_samples = args.sample or samples
    missing_samples = [sample for sample in selected_samples if sample not in samples]
    if missing_samples:
        raise SystemExit(f"Sample(s) not found in VCF: {', '.join(missing_samples)}")

    qtn_rows = load_qtn_rows()
    calls = []
    for qtn in qtn_rows:
        record = vcf_records.get((qtn["chr"], qtn["pos"]))
        for sample in selected_samples:
            calls.append(call_one(qtn, sample, record))

    summary = summarize(calls)
    calls_by_sample = defaultdict(list)
    for row in calls:
        calls_by_sample[row["sample"]].append(row)
    args.outdir.mkdir(parents=True, exist_ok=True)
    result_json_path = args.outdir / f"{args.out_prefix}.gene_check.json"
    summary_json_path = args.outdir / f"{args.out_prefix}.trait_summary.json"
    summary_path = args.outdir / f"{args.out_prefix}.trait_summary.csv"
    report_path = args.outdir / f"{args.out_prefix}.report.md"

    metadata = {
        "vcf": str(args.vcf),
        "qtn_reference": "fixed_320_qtn_reference",
        "samples": selected_samples,
        "qtn_records": len(qtn_rows),
        "qtn_calls": len(calls),
    }
    if args.include_debug_fields:
        metadata.update(
            {
                "qtn_call_schema": "debug",
                "qtn_call_fields": OUTPUT_COLUMNS,
                "review_note_codes": REVIEW_NOTE_CODES,
            }
        )
        result = {
            "metadata": metadata,
            "trait_summary": summary,
            "samples": {
                sample: {"qtn_calls": compact_calls(sample_calls, True)}
                for sample, sample_calls in sorted(calls_by_sample.items())
            },
        }
    else:
        metadata.update(
            {
                "qtn_call_schema": "catalog_matrix",
                "qtn_catalog_fields": QTN_CATALOG_COLUMNS,
                "sample_call_fields": SAMPLE_CALL_COLUMNS,
                "review_note_codes": REVIEW_NOTE_CODES,
            }
        )
        result = {
            "metadata": metadata,
            "schema_note": CATALOG_MATRIX_SCHEMA_NOTE,
            "qtn_catalog": qtn_catalog(qtn_rows),
            "trait_summary": summary,
            "samples": {
                sample: {"calls": sample_call_matrix(sample_calls)}
                for sample, sample_calls in sorted(calls_by_sample.items())
            },
        }
    write_json(result_json_path, result)
    if args.split_samples or args.csv:
        for sample, sample_calls in sorted(calls_by_sample.items()):
            sample_prefix = safe_filename(sample)
            if args.split_samples:
                sample_json_path = args.outdir / f"{sample_prefix}.qtn_calls.json"
                if args.include_debug_fields:
                    split_result = {
                        "sample": sample,
                        "qtn_call_schema": "debug",
                        "review_note_codes": REVIEW_NOTE_CODES,
                        "qtn_calls": compact_calls(sample_calls, True),
                    }
                else:
                    split_result = {
                        "sample": sample,
                        "qtn_call_schema": "catalog_matrix",
                        "schema_note": CATALOG_MATRIX_SCHEMA_NOTE,
                        "review_note_codes": REVIEW_NOTE_CODES,
                        "qtn_catalog_fields": QTN_CATALOG_COLUMNS,
                        "qtn_catalog": qtn_catalog(qtn_rows),
                        "sample_call_fields": SAMPLE_CALL_COLUMNS,
                        "calls": sample_call_matrix(sample_calls),
                    }
                write_json(sample_json_path, split_result)
            if args.csv:
                sample_csv_path = args.outdir / f"{sample_prefix}.qtn_calls.csv"
                write_csv(sample_csv_path, sample_calls, OUTPUT_COLUMNS)
    if args.summary_json:
        write_json(summary_json_path, summary)
    if args.csv:
        write_csv(summary_path, summary, list(summary[0].keys()) if summary else [])
    if args.report_md:
        write_report(report_path, calls, summary, args)

    print(f"Samples analyzed: {', '.join(selected_samples)}")
    print(f"QTN records: {len(qtn_rows)}")
    print(f"QTN calls: {len(calls)}")
    print(f"Wrote: {result_json_path}")
    if args.split_samples:
        for sample in sorted(calls_by_sample):
            sample_prefix = safe_filename(sample)
            print(f"Wrote: {args.outdir / f'{sample_prefix}.qtn_calls.json'}")
    if args.summary_json:
        print(f"Wrote: {summary_json_path}")
    if args.csv:
        for sample in sorted(calls_by_sample):
            sample_prefix = safe_filename(sample)
            print(f"Wrote: {args.outdir / f'{sample_prefix}.qtn_calls.csv'}")
        print(f"Wrote: {summary_path}")
    if args.report_md:
        print(f"Wrote: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
