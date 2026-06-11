# Trial Data Format

Seed Navi accepts maize variety trial data as Excel or CSV:

- `.xlsx`
- `.xls`
- `.csv`

The table should contain variety trial records that allow BreedCore to identify varieties, years, sites/locations, and
yield or performance measurements. If column names are not recognized by the backend, report the backend error and ask
for a corrected trial table.

Before BreedCore is called, the skill layer reads the uploaded table locally to list candidate varieties. This local
stage recognizes variety columns named `品种测试名`, `品种`, `品种名称`, `Variety`, or `Name`; year columns named `年份` or
`Year` are optional and are used only to show candidate year coverage.

Do not ask the user for a separate output directory. Output placement is controlled by the skill runtime.

## Region Names

Current production behavior is fixed to 东北中晚熟区. Use canonical internal value `zhongwanshu`. Do not ask users to
choose an ecological region during normal analysis. The `region` field remains reserved for future multi-region updates.

If the user explicitly requests another ecological region, explain that Seed Navi currently only enables 东北中晚熟区.
