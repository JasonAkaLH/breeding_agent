# VCF 输入

支持水稻 VCF 或 VCF.GZ 文件。用户上传新的 VCF/VCF.GZ 时，平台执行层会进行 QTN matching 并生成结构化事实和 Markdown 报告 artifact。

回答用户时可说明：

- VCF/VCF.GZ 是样本变异检测结果文件。
- 多样本文件会默认总览全部样本，但深度解读优先覆盖前三个样本；用户可继续指定某个样本追问。
- 结果解释只基于当前 320-QTN reference，不等同于田间表现保证。

不要要求用户提供内部 reference 路径或脚本参数。
