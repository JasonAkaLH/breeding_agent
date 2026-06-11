# Material List Format

## Accepted Files

Use CSV or Excel (`.csv`, `.xls`, `.xlsx`). Each row represents one trial
material or check material.

Recommended columns:

| Column | Meaning | Rules |
| --- | --- | --- |
| `ped_id` | Material/sample identifier | Required, non-empty text or number. Treat as an identifier, not a continuous numeric value. |
| `hyb_check` | Check/material-type marker | Required. Interpretation depends on design type. |
| `set` | Trial group/set | Required, non-empty text or number. Materials in the same group share the same `set`. |

## `hyb_check` Rules

- RCBD: `0` means ordinary test material; non-`0` means check.
- Diagonal: `2` means diagonal check material; `0` or `1` means non-diagonal material.
- Interval: `0` means ordinary test material; non-`0` means CK/check.

## Example

| ped_id | hyb_check | set |
| --- | --- | --- |
| A001 | 0 | A |
| A002 | 0 | A |
| CK_A | 1 | A |
| B001 | 0 | B |
| B002 | 0 | B |
| CK_B | 1 | B |

For Diagonal design, diagonal check rows should use `hyb_check = 2`.

For Interval design, each CK `ped_id` must be unique within the same `set`.
Different sets may reuse the same CK name.
