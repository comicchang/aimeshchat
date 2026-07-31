# codeagent-py 贡献规范

## 脱敏规则（硬性）

- **禁止**在测试、文档、配置、示例中使用真实内部域名/主机名
- 使用 `dev.example.com`、`build.example.com`、`cloud.example.com` 等占位符
- SSH alias 使用 `example.com` 域下的虚构名称
- 推送前必须 `grep -rE 'internal|corp|\.local$' tests/ src/` 确认无残留

## 代码风格

- Python 3.10+
- 类型注解（from __future__ import annotations）
- 中文注释、英文 docstring
- 测试用 pytest，mock 优先

## 提交规范

- feat/fix/refactor/test/docs 前缀
- 每个 commit 对应一个逻辑变更
- 测试必须通过再提交
