# germ_search 参数示例

`germ_search` 要求所有参数都传入。未使用的过滤条件也要传默认值。

## 最小默认搜索

```json
{
  "cropId": "",
  "pageNum": 1,
  "pageSize": 10,
  "germNamesList": "[]",
  "searchTraitData": "[]",
  "traitValueData": "{}",
  "searchFieldData": "[]"
}
```

## 按种质名称精确搜索

`germNamesList` 是 JSON 数组字符串：

```json
{
  "cropId": "",
  "pageNum": 1,
  "pageSize": 10,
  "germNamesList": "[\"种质名称1\",\"种质名称2\"]",
  "searchTraitData": "[]",
  "traitValueData": "{}",
  "searchFieldData": "[]"
}
```

## 按作物搜索

```json
{
  "cropId": "123",
  "pageNum": 1,
  "pageSize": 10,
  "germNamesList": "[]",
  "searchTraitData": "[]",
  "traitValueData": "{}",
  "searchFieldData": "[]"
}
```

如果用户给的是作物名称，先用作物列表匹配 `cropId`。

## 按性状搜索

先查询作物性状，拿到 `traitCode` 和 `traitType`。例如株高 `PH` 为数值型，在 10 到 20 之间：

```json
{
  "cropId": "123",
  "pageNum": 1,
  "pageSize": 10,
  "germNamesList": "[]",
  "searchTraitData": "[{\"traitCode\":\"PH\",\"traitType\":1}]",
  "traitValueData": "{\"PH-number1\":\"10\",\"PH-number2\":\"20\"}",
  "searchFieldData": "[]"
}
```

常见性状值格式：

- 数值范围：`{"PH-number1":"10","PH-number2":"20"}`
- 文本或列表值：`{"COLOR":"red"}`
- 日期范围：`{"PlantDate-time1":"2025-01-01","PlantDate-time2":"2025-01-02"}`

## 按扩展字段搜索

先查询扩展字段定义，拿到字段 `key`。例如 `a.accession_name` 包含 `test`：

```json
{
  "cropId": "",
  "pageNum": 1,
  "pageSize": 10,
  "germNamesList": "[]",
  "searchTraitData": "[]",
  "traitValueData": "{}",
  "searchFieldData": "[{\"key\":\"a.accession_name\",\"value\":\"test|include\"}]"
}
```

扩展字段操作符：

- 比较：`=`、`!=`、`>`、`>=`、`<`、`<=`
- 包含：`include`、`notinclude`
- 前缀：`startwith`、`notstartwith`
- 后缀：`endwith`、`notendwith`

`value` 通常写成 `查询值|操作符`。
