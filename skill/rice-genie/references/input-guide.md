# Rice Genie Input Guide

## Accepted Inputs

- `.vcf`: rice variant call file.
- `.vcf.gz`: gzipped rice variant call file.
- `.gene_check.json` or compatible JSON result from a previous gene-check run.

For multi-sample VCF files, the user may provide:

- `sample`: one material/sample name.
- `samples`: multiple material/sample names separated by commas, semicolons, or
  whitespace.

If no sample filter is provided, summarize the available samples and apply the
standard report behavior.

## Interpretation Basis

The workflow matches rice variant sites against a fixed QTN reference and
interprets detected genotype categories. Interpretations are evidence-bounded:
they indicate potential trait signals, not guaranteed field performance.

## Common Input Issues

- File is not VCF, gzipped VCF, or compatible JSON.
- Requested sample name is absent from the VCF.
- Genotype field is missing or complex.
- Chromosome naming does not match expected coordinates.
- Indel records use padded or symbolic VCF representation; genotype type may
  still be inferred from `GT`.
