---
name: mini-breedstat-rcbd
description: 生成育种田间试验的 RCBD 随机完全区组设计，并输出田间 fieldbook 与 HTML 布局预览。Use when 用户上传材料清单并要求 RCBD、随机完全区组、随机区组田间设计、重复数/区组数、对照材料位置约束、多站点独立随机化或田间布局预览。
triggers:
  - 随机区组
  - 随机区组设计
  - 随机区组试验
  - 随机区组试验设计
  - RCBD
  - RCBD设计
  - RCBD随机区组
  - 生成RCBD
  - 做RCBD
  - 随机完全区组
  - 随机完全区组设计
  - 完全随机区组
  - 完全随机区组设计
  - 随机区组田间设计
  - 区组随机化
  - 重复区组设计
  - 田间fieldbook
  - 生成fieldbook
  - 田间布局预览
  - 田间小区排布
  - 小区排布
  - 田间试验布局
  - 对照位置约束
  - 多站点随机区组
  - 多点随机区组
  - 多环境随机区组
  - randomized complete block design
  - randomized complete block
  - rcbd design
  - fieldbook
  - plot layout
  - field layout
  - multi-site rcbd
outputs:
  required:
    - answer
  files:
    - extensions: [.html]
      mime_types: [text/html]
scripts:
  - name: run_rcbd
    path: scripts/run_rcbd.py
    runtime: python
    auto_run: true
    timeout_seconds: 60
    inputs:
      required:
        - query
    outputs:
      required:
        - answer
parameters:
  blocks:
    type: integer
    required: true
    aliases: [blocks, 区组数, 区组, 重复数, 重复, reps, replications]
    patterns:
      - '(?:blocks?|区组数|区组|重复数|重复|reps?|replications?)\s*[:：=]?\s*(\d+)'
      - '(\d+)\s*(?:个|次)?(?:区组|重复|rep|reps|blocks?)'
  material_data:
    type: artifact
    required: true
    source: artifact
    aliases: [材料清单, 材料文件, 试验材料, input_data]
  planter:
    type: string
    required: false
    default: serpentine
    enum: [serpentine, cartesian]
    aliases: [planter, 种植路径, 排布方式]
  seed:
    type: integer
    required: false
    aliases: [seed, 随机种子]
    patterns:
      - '(?:seed|随机种子)\s*[:：=]?\s*(\d+)'
  site_num:
    type: integer
    required: false
    default: 1
    aliases: [site_num, site-num, 站点数, 多站点]
    patterns:
      - '(?:site[_-]?num|站点数|多站点)\s*[:：=]?\s*(\d+)'
      - '(\d+)\s*(?:个)?站点'
  site_random:
    type: string
    required: false
    default: "false"
    enum: ["true", "false"]
    aliases: [site_random, site-random, 多站点独立随机, 独立随机化]
---

# Mini BreedStat RCBD

## Use when
- 用户要求基于上传的 CSV/JSON 材料清单生成 RCBD 随机完全区组设计。
- 用户提到随机区组、重复数/区组数、对照材料、田间 fieldbook、田间布局 HTML 或多站点独立随机化。

## Workflow
1. 优先使用自动脚本 `run_rcbd`；不要要求主代理直接运行 R、Shell 或读取本地路径。
2. 确认用户已上传材料清单，并给出 `blocks` / 重复数 / 区组数。
3. 材料清单支持：
   - 推荐 CSV 列：`plot_id,hyb_check,set`。
   - 兼容 JSON/CSV 列：`ped_id,design_check,set`。
   - 如果缺少 `set`，脚本按单 set 补 `set = "A"`。
4. 默认参数：`planter = "serpentine"`，`site_num = 1`，`site_random = false`，对照与测试材料位置约束均开启。
5. 脚本成功时，根据脚本返回的事实回答：设计是否完成、行数、区组数、set、seed、是否生成 HTML 布局文件。

## Output
- 使用中文 Markdown 简洁回答。
- 先说明 RCBD 设计状态，再列出关键参数和生成结果。
- 如果 `output_files` 中包含 HTML，说明可下载查看田间布局预览。
- 打印 fieldbook 摘要时保持脚本返回的 `out_design` 种植顺序；不要按 `ranges, pass` 重新排序。
- 解释字段时使用：`plots`=种植顺序 plot，`r`=区组/重复，`trt`=材料 ID，`ranges`=物理行，`pass`=物理列，`design_check != 0` 为对照。

## Boundaries
- 不连接 OpenCPU、Docker、数据库或外部服务。
- 不编造上传文件中不存在的材料、set 或对照标记。
- 缺少材料清单或 `blocks` 时，只问一个最关键的补充问题。
- 用户只要求查看已固定 fieldbook 的布局时，不要重新随机化；只有用户明确要求新设计时才重新生成。
