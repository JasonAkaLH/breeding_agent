# Project Skills

本目录存放本项目后端可加载的 v2 项目级 Skill bundle。

推荐结构：

```text
skill/<skill-name>/
  SKILL.md                  # 轻量说明，只含 name/description frontmatter
  skill.contract.yaml        # 唯一平台契约
  schemas/*.input.yaml       # 输入 schema、字段来源、缺参问题和校验
  references/*.md            # prompt-facing 按需资料
```

约束与示例见仓库内 `.codex/skills/breeding-skill-builder/references/Skill构建指南.md`。

说明：
- 无 `skill.contract.yaml` 的 Skill 不注册、不执行、不进入 capability 列表。
- 后端默认扫描仓库根目录下的 `skill/**/SKILL.md`，但公开能力只从同目录 contract 注册。
- 主代理只可按 contract resource policy 读取 prompt-facing references；实现目录和内部配置不进入公开 profile。
