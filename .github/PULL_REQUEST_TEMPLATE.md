## 这个 PR 做了什么

<!-- 一句话说明；关联 Issue 用 Fixes #123 -->

## 改动范围

- [ ] 插件 `plugin/plugin.py`
- [ ] 本地解析器 `resolver/*.js`
- [ ] 文档 / 配置示例
- [ ] CI / 仓库配置

## 自检清单

- [ ] `python3 -m py_compile plugin/plugin.py` 通过（改了插件）
- [ ] `node --check resolver/resolver.js && node --check resolver/agent.js` 通过（改了解析器）
- [ ] **没有**提交真实配置 / token / 服务器地址 / QQ 号
- [ ] 新增配置项已同步到 `config.example.*` 与两份 README
- [ ] 保持零第三方依赖（插件用 NekroAgent 自带能力，解析器用 Node 标准库）

## 怎么验证的

<!-- 贴自检 JSON（删掉 token）、或说明在群里实测的结果 -->
