from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Mapping


LOCAL_BREEDCORE_URL = "http://127.0.0.1:8010"
LOCAL_BREEDSTAT2_URL = "http://127.0.0.1:8020"
CONTAINER_BREEDCORE_URL = "http://breedcore:8000"
CONTAINER_BREEDSTAT2_URL = "http://breedstat2:8000"
DEFAULT_MAF = 0.000001
ALLOWED_FILE_TYPES = {"simple_hapmap", "tassel_hapmap", "vcf", "plink"}
SUPPORTED_EXTENSIONS = {".hmp", ".txt", ".vcf", ".gz", ".ped", ".map", ".bed", ".bim", ".fam"}


def json_response(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))


def failure(answer: str, *, missing: list[str] | None = None, error_type: str = "pop_gene_error") -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "is_error": True,
        "answer": answer,
        "error": {"type": error_type, "message": answer},
    }
    if missing:
        result["missing"] = missing
    return result


def safe_run_id(value: Any) -> str:
    text = str(value or "").strip() or "pop_gene"
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text[:80] or "pop_gene"


def running_in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def default_breedcore_url() -> str:
    return CONTAINER_BREEDCORE_URL if running_in_container() else LOCAL_BREEDCORE_URL


def default_breedstat2_url() -> str:
    return CONTAINER_BREEDSTAT2_URL if running_in_container() else LOCAL_BREEDSTAT2_URL


def breedcore_base_url() -> str:
    return (os.environ.get("POP_GENE_BREEDCORE_URL") or os.environ.get("BREEDCORE_URL") or default_breedcore_url()).rstrip("/")


def breedstat2_base_url() -> str:
    return (
        os.environ.get("POP_GENE_BREEDSTAT2_URL") or os.environ.get("BREEDSTAT2_URL") or default_breedstat2_url()
    ).rstrip("/")


def is_loopback_url(value: str) -> bool:
    try:
        host = urlparse(value).hostname
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def configured_breedcore_url(payload: Mapping[str, Any] | None = None) -> str:
    payload = payload or {}
    explicit = str(payload.get("breedcore_url") or payload.get("breedcore_base_url") or "").strip()
    if explicit:
        if running_in_container() and is_loopback_url(explicit):
            return breedcore_base_url()
        return explicit.rstrip("/")
    return breedcore_base_url()


def configured_breedstat2_url(payload: Mapping[str, Any] | None = None) -> str:
    payload = payload or {}
    explicit = str(payload.get("breedstat2_url") or payload.get("breedstat2_base_url") or "").strip()
    if explicit:
        if running_in_container() and is_loopback_url(explicit):
            return breedstat2_base_url()
        return explicit.rstrip("/")
    return breedstat2_base_url()


def breeding_runtime_root() -> Path:
    return Path(os.environ.get("BREEDING_RUNTIME_ROOT") or "/runtime")


def default_breedcore_upload_dir() -> Path:
    return breeding_runtime_root() / "breedcore" / "uploads"


def get_json(url: str, timeout: int = 20) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        content = response.read().decode("utf-8")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return data


def post_json(url: str, payload: Mapping[str, Any], timeout: int = 900) -> dict[str, Any]:
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read().decode("utf-8")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return data


def multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def upload_to_breedcore(
    base_url: str,
    input_file: Path,
    file_type: str | None,
    source: str,
    *,
    run_id: str | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    src = input_file.expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    parsed = urlparse(base_url.rstrip("/") + "/uploads")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Invalid BreedCore URL: {base_url}")

    boundary = f"----pop-gene-{uuid.uuid4().hex}"
    fields = {"source": source}
    if file_type:
        fields["file_type"] = file_type
    if run_id:
        fields["run_id"] = run_id

    field_parts = [multipart_field(boundary, key, value) for key, value in fields.items()]
    file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{src.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_length = sum(len(part) for part in field_parts) + len(file_header) + src.stat().st_size + len(footer)

    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = connection_cls(parsed.netloc, timeout=timeout)
    target = parsed.path or "/uploads"
    if parsed.query:
        target += f"?{parsed.query}"
    try:
        conn.putrequest("POST", target)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(content_length))
        conn.endheaders()
        for part in field_parts:
            conn.send(part)
        conn.send(file_header)
        with src.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(footer)
        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Cannot upload file to BreedCore at {base_url}: {exc}") from exc
    finally:
        conn.close()

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"BreedCore /uploads returned non-JSON response: {body[:500]}") from exc
    if response.status >= 400:
        raise RuntimeError(f"BreedCore /uploads failed with HTTP {response.status}: {json.dumps(result, ensure_ascii=False)}")
    if not isinstance(result, dict) or not result.get("upload_id"):
        raise RuntimeError("BreedCore /uploads did not return upload_id.")
    return result


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return data


def artifact_filename(artifact: Mapping[str, Any], default: str = "genotype.hmp.txt") -> str:
    raw = str(artifact.get("filename") or default)
    name = Path(raw).name.replace("\x00", "")
    if not name or name in {".", ".."}:
        return default
    return name


def decode_artifact_content(artifact: Mapping[str, Any]) -> bytes | None:
    if isinstance(artifact.get("content"), str):
        return str(artifact["content"]).encode("utf-8")
    if isinstance(artifact.get("content_base64"), str):
        try:
            return base64.b64decode(str(artifact["content_base64"]), validate=True)
        except Exception:
            return None
    return None


def input_from_resource_manifest(payload: Mapping[str, Any]) -> Path | None:
    raw = payload.get("resource_manifest_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    manifest_path = Path(raw).expanduser()
    if not manifest_path.exists() or not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, list):
        return None
    for item in files:
        if not isinstance(item, Mapping):
            continue
        mount_path = item.get("mount_path")
        if not isinstance(mount_path, str) or not mount_path.strip():
            continue
        candidate = Path(mount_path).expanduser()
        suffixes = [suffix.lower() for suffix in candidate.suffixes]
        if candidate.exists() and candidate.is_file() and (
            candidate.suffix.lower() in SUPPORTED_EXTENSIONS or ".vcf" in suffixes
        ):
            return candidate.resolve()
    return None


def resolve_input_file(payload: Mapping[str, Any], work_dir: Path) -> Path | None:
    manifest_input = input_from_resource_manifest(payload)
    if manifest_input is not None:
        return manifest_input

    artifacts = payload.get("uploaded_artifacts")
    if isinstance(artifacts, list | tuple):
        for item in artifacts:
            if not isinstance(item, Mapping):
                continue
            content = decode_artifact_content(item)
            if content is None:
                continue
            path = work_dir / artifact_filename(item)
            path.write_bytes(content)
            return path

    for key in ("genotype_file", "file_path", "path", "input_file"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
    return None


def infer_file_type(path: Path) -> str | None:
    suffixes = [item.lower() for item in path.suffixes]
    if ".vcf" in suffixes:
        return "vcf"
    if path.suffix.lower() in {".bed", ".bim", ".fam", ".ped", ".map"}:
        return "plink"
    try:
        header = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[0]
    except Exception:
        return None
    lowered = header.lower().replace("\t", ",")
    if lowered.startswith("rs,alleles,chrom,pos"):
        return "tassel_hapmap"
    if lowered.startswith("snpid,chrom,pos,ref,alt"):
        return "simple_hapmap"
    return None


def resolve_file_type(payload: Mapping[str, Any], input_file: Path) -> str | None:
    explicit = str(payload.get("file_type") or payload.get("input_format") or payload.get("format") or "").strip().lower()
    if explicit == "custom_simple_hmp":
        explicit = "simple_hapmap"
    if explicit in ALLOWED_FILE_TYPES:
        return explicit
    return infer_file_type(input_file)


def copy_to_breedcore_upload(input_file: Path) -> tuple[Path, str]:
    upload_dir = Path(
        os.environ.get("POP_GENE_BREEDCORE_UPLOAD_DIR")
        or os.environ.get("BREEDCORE_HOST_UPLOAD_DIR")
        or default_breedcore_upload_dir()
    ).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / input_file.name
    if input_file.resolve() != target.resolve():
        shutil.copyfile(input_file, target)
    return target, f"/work/uploads/{target.name}"


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def breedcore_input_ref(input_file: Path, file_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if truthy(payload.get("use_legacy_input_path")) or truthy(os.environ.get("POP_GENE_USE_LEGACY_INPUT_PATH")):
        _, container_path = copy_to_breedcore_upload(input_file)
        return {"path": container_path, "file_type": file_type}
    upload = upload_to_breedcore(
        configured_breedcore_url(payload),
        input_file,
        file_type,
        "pop-gene",
        run_id=str(payload.get("run_id") or "") or None,
    )
    return {"upload_id": upload["upload_id"], "file_type": upload.get("file_type") or file_type}


def ensure_services(*, payload: Mapping[str, Any], need_tree: bool = False) -> dict[str, Any]:
    health = {"breedcore": get_json(f"{configured_breedcore_url(payload)}/health")}
    if need_tree:
        health["breedstat2"] = get_json(f"{configured_breedstat2_url(payload)}/health")
    return health


def load_renderer(module_name: str) -> Any:
    path = Path(__file__).resolve().parent / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load renderer: {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def render_to_file(*, renderer: str, result_json: Path, html_file: Path) -> None:
    module = load_renderer(renderer)
    result = module.load_result(str(result_json))
    if renderer == "render_admixture_plot":
        html = module.render_html(result, result_json.resolve())
    elif renderer == "render_pca_plot":
        html = module.render_html(result, "群体基因型 PCA 分析图")
    else:
        html = module.render_html(result)
    html_file.parent.mkdir(parents=True, exist_ok=True)
    html_file.write_text(html, encoding="utf-8")


def build_output_dirs(run_id: str) -> dict[str, Path]:
    root = Path(os.environ.get("MAF_SKILL_OUTPUT_DIR") or Path("outputs") / "pop-gene" / run_id).resolve()
    dirs = {
        "root": root,
        "api": root / "api",
        "plots": root / "plots",
        "reports": root / "reports",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def output_artifact_path(path: Path, output_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(output_root.resolve())
    except ValueError:
        return f"outputs/{path.name}"
    return str(Path("outputs") / relative).replace(os.sep, "/")


def workspace_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "BrAPI").exists() and (parent / "skills").exists():
            return parent
    return Path.cwd()


def resolve_runtime_path(path_value: Any) -> Path | None:
    if not path_value:
        return None
    raw = str(path_value).replace("\\", "/")
    direct = Path(raw)
    if direct.exists():
        return direct
    root = workspace_root()
    if raw.startswith("/work/runs/"):
        mapped = root / "BrAPI" / "runtime" / "breedcore" / "runs" / raw[len("/work/runs/") :]
        return mapped if mapped.exists() else None
    if raw.startswith("/work/uploads/"):
        mapped = root / "BrAPI" / "runtime" / "breedcore" / "uploads" / raw[len("/work/uploads/") :]
        return mapped if mapped.exists() else None
    return None


def copy_if_available(src: Path | None, dst: Path) -> Path | None:
    if src is None or not src.exists() or not src.is_file():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    return dst


def localize_admixture_q_files(result: dict[str, Any], dirs: Mapping[str, Path]) -> dict[str, Any]:
    files = result.get("files")
    if not isinstance(files, dict):
        return result
    q_files = files.get("q_files")
    if not isinstance(q_files, dict):
        return result

    target_dir = dirs["root"] / "artifacts" / "admixture"
    localized_q_files: dict[str, str] = {}
    fam_source = resolve_runtime_path(str(files.get("plink_prefix") or "") + ".fam") if files.get("plink_prefix") else None

    for raw_k, raw_q_path in sorted(q_files.items(), key=lambda item: int(item[0])):
        q_source = resolve_runtime_path(raw_q_path)
        if q_source is None:
            continue
        k = str(int(raw_k))
        q_target = target_dir / f"plink.{k}.Q"
        copied_q = copy_if_available(q_source, q_target)
        if copied_q is None:
            continue
        localized_q_files[k] = str(copied_q)
        if fam_source is None:
            fam_source = q_source.with_name("plink.fam")

    if not localized_q_files:
        return result

    copy_if_available(fam_source, target_dir / "plink.fam")
    files["q_files"] = localized_q_files
    files["plink_fam"] = str(target_dir / "plink.fam")
    result["files"] = files
    return result


def read_fam_sample_ids(fam_path: Path) -> list[str]:
    sample_ids: list[str] = []
    for line in fam_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        fields = line.split()
        if fields:
            sample_ids.append(fields[1] if len(fields) > 1 else fields[0])
    return sample_ids


def read_q_rows(q_path: Path) -> list[list[float]]:
    rows: list[list[float]] = []
    for line in q_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        fields = line.split()
        if fields:
            rows.append([float(value) for value in fields])
    return rows


def ancestry_rows_for_k(sample_ids: list[str], q_rows: list[list[float]], mixed_threshold: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sample_id, q_values in zip(sample_ids, q_rows):
        if not q_values:
            continue
        max_index = max(range(len(q_values)), key=lambda idx: q_values[idx])
        row: dict[str, Any] = {
            "sample_id": sample_id,
            "population": None,
            "assignment": f"Q{max_index + 1}",
            "max_q": float(q_values[max_index]),
            "is_mixed": float(q_values[max_index]) < mixed_threshold,
        }
        for index, value in enumerate(q_values, start=1):
            row[f"Q{index}"] = float(value)
        rows.append(row)
    rows.sort(key=lambda item: (str(item["assignment"]), -float(item["max_q"]), str(item["sample_id"])))
    return rows


def embed_admixture_k_tables(result: dict[str, Any]) -> dict[str, Any]:
    files = result.get("files")
    if not isinstance(files, dict):
        return result
    q_files = files.get("q_files")
    if not isinstance(q_files, dict):
        return result
    fam_path = resolve_runtime_path(files.get("plink_fam"))
    if fam_path is None or not fam_path.exists():
        return result
    sample_ids = read_fam_sample_ids(fam_path)
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    mixed_threshold = float(summary.get("mixed_threshold", 0.8) or 0.8)

    structure_by_k: dict[str, list[dict[str, Any]]] = {}
    for raw_k, raw_q_path in sorted(q_files.items(), key=lambda item: int(item[0])):
        q_path = resolve_runtime_path(raw_q_path)
        if q_path is None or not q_path.exists():
            continue
        rows = ancestry_rows_for_k(sample_ids, read_q_rows(q_path), mixed_threshold)
        if rows:
            structure_by_k[str(int(raw_k))] = rows

    if structure_by_k:
        tables = result.get("tables") if isinstance(result.get("tables"), dict) else {}
        tables["structure_by_k"] = structure_by_k
        result["tables"] = tables
    return result


def requested_k_values(result: Mapping[str, Any], payload: Mapping[str, Any]) -> list[int]:
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    kmin = int(payload.get("kmin") or summary.get("kmin") or 2)
    kmax = int(payload.get("kmax") or summary.get("kmax") or kmin)
    return list(range(kmin, kmax + 1))


def existing_structure_k_values(result: Mapping[str, Any]) -> set[int]:
    tables = result.get("tables") if isinstance(result.get("tables"), Mapping) else {}
    structure_by_k = tables.get("structure_by_k")
    if not isinstance(structure_by_k, Mapping):
        return set()
    values: set[int] = set()
    for key in structure_by_k:
        try:
            values.add(int(key))
        except (TypeError, ValueError):
            continue
    return values


def supplement_admixture_structure_by_k(
    result: dict[str, Any],
    prepared_genotype_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    missing_k = [k for k in requested_k_values(result, payload) if k not in existing_structure_k_values(result)]
    if not missing_k:
        return result

    tables = result.get("tables") if isinstance(result.get("tables"), dict) else {}
    structure_by_k = tables.get("structure_by_k") if isinstance(tables.get("structure_by_k"), dict) else {}
    warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []

    for k in missing_k:
        single = post_json(
            f"{configured_breedcore_url(payload)}/jobs/admixture",
            {
                "input": {"prepared_genotype_id": prepared_genotype_id},
                "mode": str(payload.get("mode") or "unsupervised"),
                "kmin": k,
                "kmax": k,
                "k": k,
                "selection_method": "min_cv",
                "max_parallel_k": 1,
                "mixed_threshold": float(payload.get("mixed_threshold") or 0.8),
                "params": {},
            },
            timeout=1800,
        )
        raise_if_job_failed(single, f"admixture K={k}")
        single_tables = single.get("tables") if isinstance(single.get("tables"), Mapping) else {}
        rows = single_tables.get("structure_barplot")
        if isinstance(rows, list) and rows:
            structure_by_k[str(k)] = rows
        else:
            warnings.append(f"ADMIXTURE K={k} did not return tables.structure_barplot.")

    if structure_by_k:
        tables["structure_by_k"] = structure_by_k
        result["tables"] = tables
    if warnings:
        result["warnings"] = warnings
    return result


def prepare_genotype(*, input_file: Path, file_type: str, maf: float, dirs: Mapping[str, Path], payload: Mapping[str, Any]) -> dict[str, Any]:
    input_ref = breedcore_input_ref(input_file, file_type, payload)
    result = post_json(
        f"{configured_breedcore_url(payload)}/jobs/prepare-genotype",
        {"input": input_ref, "maf": maf, "params": {}},
    )
    write_json(dirs["api"] / "prepare_genotype_result.json", result)
    if result.get("ok") is False:
        raise RuntimeError("prepare-genotype returned ok=false")
    return result


def prepared_id_from(result: Mapping[str, Any]) -> str:
    summary = result.get("summary")
    if isinstance(summary, Mapping) and summary.get("prepared_genotype_id"):
        return str(summary["prepared_genotype_id"])
    raise RuntimeError("prepare-genotype did not return summary.prepared_genotype_id")


def raise_if_job_failed(result: Mapping[str, Any], label: str) -> None:
    if result.get("ok") is not False:
        return
    error = result.get("error") if isinstance(result.get("error"), Mapping) else {}
    message = error.get("message") or result.get("message") or f"{label} returned ok=false"
    code = error.get("code")
    if code:
        raise RuntimeError(f"{label} failed [{code}]: {message}")
    raise RuntimeError(f"{label} failed: {message}")


def run_pca(prepared_genotype_id: str, dirs: Mapping[str, Path], payload: Mapping[str, Any]) -> dict[str, Any]:
    result = post_json(
        f"{configured_breedcore_url(payload)}/jobs/pca",
        {"input": {"prepared_genotype_id": prepared_genotype_id}, "num_eigs": 5, "params": {}},
    )
    json_file = dirs["api"] / "pca_result.json"
    write_json(json_file, result)
    raise_if_job_failed(result, "pca")
    render_to_file(renderer="render_pca_plot", result_json=json_file, html_file=dirs["plots"] / "pca_plot.html")
    return result


def run_admixture(prepared_genotype_id: str, dirs: Mapping[str, Path], payload: Mapping[str, Any]) -> dict[str, Any]:
    result = post_json(
        f"{configured_breedcore_url(payload)}/jobs/admixture",
        {
            "input": {"prepared_genotype_id": prepared_genotype_id},
            "mode": "unsupervised",
            "kmin": int(payload.get("kmin") or 2),
            "kmax": int(payload.get("kmax") or 8),
            "selection_method": "min_cv",
            "max_parallel_k": 1,
            "mixed_threshold": float(payload.get("mixed_threshold") or 0.8),
            "params": {},
        },
        timeout=1800,
    )
    raise_if_job_failed(result, "admixture")
    result = localize_admixture_q_files(result, dirs)
    result = embed_admixture_k_tables(result)
    result = supplement_admixture_structure_by_k(result, prepared_genotype_id, payload)
    json_file = dirs["api"] / "admixture_result.json"
    write_json(json_file, result)
    render_to_file(renderer="render_admixture_plot", result_json=json_file, html_file=dirs["plots"] / "admixture_plot.html")
    return result


def run_distance(prepared_genotype_id: str, dirs: Mapping[str, Path], payload: Mapping[str, Any]) -> dict[str, Any]:
    result = post_json(
        f"{configured_breedcore_url(payload)}/jobs/genetic-distance",
        {
            "input": {"prepared_genotype_id": prepared_genotype_id},
            "method": "ibs",
            "include_ordination": True,
            "ordination_method": "pcoa",
            "n_components": 2,
            "top_pairs_limit": 30,
            "params": {},
        },
        timeout=1200,
    )
    json_file = dirs["api"] / "genetic_distance_result.json"
    write_json(json_file, result)
    raise_if_job_failed(result, "genetic-distance")
    render_to_file(renderer="render_distance_plot", result_json=json_file, html_file=dirs["plots"] / "distance_pcoa_plot.html")
    return result


def run_tree(distance_result: Mapping[str, Any], dirs: Mapping[str, Path], payload: Mapping[str, Any]) -> dict[str, Any]:
    tables = distance_result.get("tables")
    if not isinstance(tables, Mapping) or not isinstance(tables.get("distance_matrix"), (Mapping, list)):
        raise RuntimeError("genetic-distance result did not include tables.distance_matrix")
    result = post_json(
        f"{configured_breedstat2_url(payload)}/phylogenetics/tree",
        {
            "distance_matrix": tables["distance_matrix"],
            "method": str(payload.get("tree_method") or "bionj").lower(),
            "output": "newick",
            "params": {},
        },
        timeout=900,
    )
    json_file = dirs["api"] / "tree_result.json"
    write_json(json_file, result)
    raise_if_job_failed(result, "phylogenetics/tree")
    render_to_file(renderer="render_tree_plot", result_json=json_file, html_file=dirs["plots"] / "tree_plot.html")
    return result


def summary_counts(prepare_result: Mapping[str, Any], fallback: Mapping[str, Any] | None = None) -> tuple[Any, Any]:
    summary = prepare_result.get("summary") if isinstance(prepare_result.get("summary"), Mapping) else {}
    fallback_summary = fallback.get("summary") if fallback and isinstance(fallback.get("summary"), Mapping) else {}
    return (
        summary.get("sample_count_output") or summary.get("sample_count") or fallback_summary.get("sample_count"),
        summary.get("marker_count_output") or summary.get("marker_count") or fallback_summary.get("input_marker_count"),
    )


def answer_for(analysis: str, prepare_result: Mapping[str, Any], jobs: Mapping[str, Mapping[str, Any]], dirs: Mapping[str, Path]) -> str:
    sample_count, marker_count = summary_counts(prepare_result, jobs.get("pca"))
    lines = [f"已完成 {analysis} 群体遗传分析。"]
    if sample_count or marker_count:
        lines.append(f"本次分析包含 {sample_count or 'NA'} 份材料、{marker_count or 'NA'} 个可用标记。")
    if "pca" in jobs:
        eigen = jobs["pca"].get("tables", {}).get("eigenvalues", []) if isinstance(jobs["pca"].get("tables"), Mapping) else []
        if isinstance(eigen, list) and len(eigen) >= 2:
            pc1 = eigen[0].get("variance_ratio")
            pc2 = eigen[1].get("variance_ratio")
            if isinstance(pc1, (int, float)) and isinstance(pc2, (int, float)):
                lines.append(f"PCA: PC1 解释 {pc1 * 100:.2f}%，PC2 解释 {pc2 * 100:.2f}%，PC1+PC2 合计 {(pc1 + pc2) * 100:.2f}%。")
    if "admixture" in jobs:
        recommended = jobs["admixture"].get("summary", {}).get("recommended_k") if isinstance(jobs["admixture"].get("summary"), Mapping) else None
        if recommended:
            lines.append(f"ADMIXTURE: 推荐 K={recommended}，请结合 CV error 趋势和育种来源信息解释祖源组分。")
    if "genetic_distance" in jobs:
        lines.append("遗传距离/PCoA: 已生成距离热图、PCoA 和近缘材料表，可用于筛查近重复或亲缘很近的材料。")
    if "tree" in jobs:
        lines.append("系统发育树: 已根据遗传距离生成树图，拓扑受标记集、距离定义和聚类方法影响。")
    lines.append(f"用户可见 HTML 图已写入：{dirs['root']}")
    return "\n".join(lines)


def write_session(
    *,
    run_id: str,
    analysis: str,
    input_file: Path,
    file_type: str,
    prepare_result: Mapping[str, Any],
    jobs: Mapping[str, Mapping[str, Any]],
    dirs: Mapping[str, Path],
) -> Path:
    html_plots = {}
    for key, filename in {
        "pca": "pca_plot.html",
        "admixture": "admixture_plot.html",
        "genetic_distance": "distance_pcoa_plot.html",
        "tree": "tree_plot.html",
    }.items():
        path = dirs["plots"] / filename
        if path.exists():
            html_plots[key] = str(path)
    session = {
        "session_id": run_id,
        "analysis": analysis,
        "input": {
            "original_file": str(input_file),
            "file_type": file_type,
            "prepared_genotype_id": prepared_id_from(prepare_result),
        },
        "output": {
            "output_dir": str(dirs["root"]),
            "html_plots": html_plots,
            "reports": {},
        },
        "jobs": {key: {"job_id": value.get("job_id"), "status": value.get("status"), "summary": value.get("summary")} for key, value in jobs.items()},
        "available_next_steps": ["admixture", "genetic_distance", "tree", "report"],
    }
    session_file = dirs["root"] / ".pop-gene-session.json"
    write_json(session_file, session)
    return session_file


def normalize_analysis(payload: Mapping[str, Any]) -> str:
    value = str(payload.get("analysis") or payload.get("workflow") or payload.get("mode") or "default").strip().lower()
    aliases = {
        "": "default",
        "pca": "default",
        "pcoa": "genetic_distance",
        "distance": "genetic_distance",
        "genetic-distance": "genetic_distance",
        "full": "report",
        "complete": "report",
    }
    return aliases.get(value, value)


def output_plot_specs_for_analysis(analysis: str) -> list[tuple[str, str]]:
    specs = {
        "default": [("PCA HTML 图", "pca_plot.html")],
        "admixture": [("ADMIXTURE HTML 图", "admixture_plot.html")],
        "genetic_distance": [("遗传距离和 PCoA HTML 图", "distance_pcoa_plot.html")],
        "tree": [("系统发育树 HTML 图", "tree_plot.html")],
        "report": [
            ("PCA HTML 图", "pca_plot.html"),
            ("ADMIXTURE HTML 图", "admixture_plot.html"),
            ("遗传距离和 PCoA HTML 图", "distance_pcoa_plot.html"),
            ("系统发育树 HTML 图", "tree_plot.html"),
        ],
    }
    return specs.get(analysis, specs["default"])


def output_files_for_analysis(analysis: str, dirs: Mapping[str, Path]) -> list[dict[str, str]]:
    output_files = []
    for label, filename in output_plot_specs_for_analysis(analysis):
        path = dirs["plots"] / filename
        if not path.exists():
            continue
        output_files.append(
            {
                "path": output_artifact_path(path, dirs["root"]),
                "filename": path.name,
                "mime_type": "text/html",
                "label": label,
            }
        )
    return output_files


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        json_response(failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0
    if not isinstance(payload, dict):
        json_response(failure("脚本输入必须是 JSON object。", error_type="invalid_stdin"))
        return 0

    with tempfile.TemporaryDirectory(prefix="pop-gene-input-") as tmp:
        work_dir = Path(tmp)
        input_file = resolve_input_file(payload, work_dir)
        if input_file is None:
            json_response(failure("请提供基因型数据文件或上传文件。", missing=["genotype_file"], error_type="missing_input"))
            return 0
        if input_file.suffix.lower() not in SUPPORTED_EXTENSIONS and ".vcf" not in [s.lower() for s in input_file.suffixes]:
            json_response(failure("基因型文件扩展名不在支持范围内。", error_type="unsupported_file"))
            return 0

        file_type = resolve_file_type(payload, input_file)
        if file_type is None:
            json_response(
                failure(
                    "无法自动识别输入格式，请指定 simple_hapmap、tassel_hapmap、vcf 或 plink。",
                    missing=["file_type"],
                    error_type="missing_file_type",
                )
            )
            return 0

        analysis = normalize_analysis(payload)
        run_id = safe_run_id(payload.get("run_id") or f"{input_file.stem}-{analysis}")
        dirs = build_output_dirs(run_id)
        need_tree = analysis in {"tree", "report"}

        try:
            ensure_services(payload=payload, need_tree=need_tree)
            prepare_result = prepare_genotype(
                input_file=input_file,
                file_type=file_type,
                maf=float(payload.get("maf") or DEFAULT_MAF),
                dirs=dirs,
                payload=payload,
            )
            prepared_genotype_id = prepared_id_from(prepare_result)
            jobs: dict[str, Mapping[str, Any]] = {}

            if analysis in {"default", "report"}:
                jobs["pca"] = run_pca(prepared_genotype_id, dirs, payload)
            if analysis in {"admixture", "report"}:
                jobs["admixture"] = run_admixture(prepared_genotype_id, dirs, payload)
            distance_result: Mapping[str, Any] | None = None
            if analysis in {"genetic_distance", "tree", "report"}:
                distance_result = run_distance(prepared_genotype_id, dirs, payload)
                jobs["genetic_distance"] = distance_result
            if analysis in {"tree", "report"}:
                if distance_result is None:
                    distance_result = run_distance(prepared_genotype_id, dirs, payload)
                    jobs["genetic_distance"] = distance_result
                jobs["tree"] = run_tree(distance_result, dirs, payload)

            write_session(
                run_id=run_id,
                analysis=analysis,
                input_file=input_file,
                file_type=file_type,
                prepare_result=prepare_result,
                jobs=jobs,
                dirs=dirs,
            )
        except Exception as exc:
            json_response(failure(f"群体遗传分析执行失败：{exc}", error_type="analysis_failed"))
            return 0

    output_files = output_files_for_analysis(analysis, dirs)
    json_response(
        {
            "ok": True,
            "answer": answer_for(analysis, prepare_result, jobs, dirs),
            "analysis": analysis,
            "run_id": run_id,
            "prepared_genotype_id": prepared_id_from(prepare_result),
            "output_dir": str(dirs["root"]),
            "output_files": output_files,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
