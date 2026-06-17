# Field Design Workflows

## RCBD

Required inputs:

- Material list.
- `blocks`: number of blocks/repeats.

Optional inputs:

- `planter`: `serpentine` by default; `cartesian` when the user wants simple
  row-major order.
- `seed`: random seed.
- `site_num`: number of trial sites. Treat Chinese “试点” as trial site, not
  material `set`.

Final answer should include the block count, seed, planting path, first 10 rows
of planting order, fieldbook, and HTML layout preview. If `site_num > 1`, the
formal fieldbook is a multi-sheet Excel workbook with one sheet per site; the
chat preview table should show only `site1`.

## Diagonal

Required inputs:

- Material list.
- `ncols`: field column count.

Optional inputs:

- `ck_ratio`: `A`, `B`, or `C`; default is `A`.
- `planter`: default `serpentine`.
- `randomize`: default `true`; use `false` only when the user asks to preserve
  original list order.
- `seed`: random seed.
- `site_num`: number of trial sites. If users say “多做几个试点/地点”,
  generate multiple independent site designs.

Check-ratio levels:

- `A`: target 5% to 10% checks.
- `B`: target 10% to 15% checks.
- `C`: target 15% to 20% checks.

If the design auto-upgrades the requested check-ratio level, tell the user the
requested level, used level, upgrade status, and actual check percentage.

## Interval

User-facing stages:

```text
材料清单 + ncols -> 识别 CK -> 追问对照位置约束 -> 生成设计
```

Required first-step inputs:

- Material list.
- `ncols`: field column count.

If the material list and design type are available but `ncols` is missing, ask
only for the field column count in Chinese:

```text
已收到材料清单，设计类型为间比法设计。还需要你补充田块列数 ncols，例如：列数10。
```

Do not say the first step is ready, do not announce that processing will start,
and do not ask for CK placement parameters before `ncols` is provided. When the
material list and `ncols` are available, run the skill immediately so the
system can detect CK materials and return `status=needs_ck_parameters`.

If CK placement parameters are missing, list detected CK materials and ask for:

```text
Download the CK position template, fill 起始位置 and 间隔数量, then upload the
completed CSV/XLS/XLSX file.
```

Name this missing item `对照位置约束`. It is a dynamic follow-up after CK
detection, not an initial schema-required material field and not a variety
specification. Do not call it `品种规格`, `品种参数`, `处理编号`, or any generic
material/specification field.

The generated template columns are:

```text
CK编号,材料编号,组别,起始位置,间隔数量
```

User-facing explanation:

- `对照编号`: the number assigned in the detected CK table.
- `起始位置`: the first planting-order position where this CK appears.
- `间隔数量`: how many ordinary test materials appear between two occurrences of
  this CK.
- Do not recommend manually typing compact strings such as `1,1,10;2,5,10`.
  For many checks, the official flow is template download, two-column fill, and
  file upload.

Before the final Interval design run, show a confirmation table with
`对照编号`, `材料编号`, `组别`, `起始位置`, and `间隔数量`. Do not proceed if any CK is
missing placement parameters or if CK positions overlap.

After the first Interval result, ask whether the user needs multiple independent
repeats. If repeats are requested, each repeat should be generated as a separate
result with a different seed unless the user explicitly asks to merge them.

If the user requests multiple trial sites instead of repeats, keep one design
per site. Reuse the same CK placement constraints and design parameters, then
change only the random seed for each site.

## Multi-Site Trial Design

When the user asks for “多个试点”, “多个地点”, “做几次”, or “几套不一样的结果”,
interpret it as multiple trial sites:

- `site_num` is the number of trial sites.
- Do not confuse trial sites with the material `set` column.
- Use site names `site1`, `site2`, `site3`, ...
- Use the base `seed` for `site1`, `seed + 1` for `site2`, and so on.
- Generate a separate design for each site with the same material list and
  design parameters.
- For `site_num > 1`, export the complete fieldbook as an Excel workbook where
  each sheet is one site. Do not expose a combined all-site table as the primary
  user-facing fieldbook.
- The chat preview table may show `site1` only.
- Render HTML with site tabs so users can switch among sites.

## User-Facing Output

Use these preview columns when available:

- RCBD: `plots`, `r`, `ped_id`, `ranges`, `pass`, `set`, `hyb_check`.
- Diagonal: `plots`, `ped_id`, `ranges`, `pass`, `set`, `hyb_check`
  or `design_check`.
- Interval: `plots`, `r`, `ped_id`, `ranges`, `pass`, `set`, `hyb_check`,
  when available.

Do not show raw JSON by default.
