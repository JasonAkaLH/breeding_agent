# Genotype Data Formats

## Supported Formats

Use these format names in user-facing text and API calls:

- `simple_hapmap`
- `tassel_hapmap`
- `vcf`
- `plink`

`simple_hapmap` is the original script-style HMP format with columns like:

```text
SNPid,chrom,pos,ref,alt,sample...
```

`tassel_hapmap` is TASSEL HapMap with columns like:

```text
rs,alleles,chrom,pos,strand,assembly,center,protLSID,assayLSID,panel,QCcode,sample...
```

Treat `custom_simple_hmp` as a backward-compatible alias, but prefer `simple_hapmap` in new replies.

## Format Inference

- `.vcf` and `.vcf.gz` usually map to `vcf`.
- TASSEL HapMap can be inferred from the `rs, alleles, chrom, pos...` header.
- Simple HapMap can be inferred from the `SNPid, chrom, pos, ref, alt...` header.
- PLINK requires the corresponding PLINK inputs; ask for missing paired files when needed.

If the format cannot be inferred safely, ask only for `simple_hapmap`, `tassel_hapmap`, `vcf`, or `plink`.

## Path Boundary

User-provided Windows or WSL paths should not be sent directly to BrAPI APIs. Upload local files to BreedCore through
`POST /uploads`, then pass the returned `upload_id` to file-backed BreedCore jobs.
