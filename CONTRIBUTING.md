# 参与开发

欢迎 Issue 和 PR。这个项目很小，规则也只有几条。

## 提 Issue 前

1. 先看 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)，八成能自己解决
2. 跑一次自检，把返回 JSON 贴上（**记得删掉 token**）：

   ```bash
   curl "http://<server>:8022/plugins/local_video.video_forward/resolver/self-test?token=<token>&url=<url>&download=true"
   ```

3. 说明环境：NekroAgent 版本、NapCat 版本、操作系统、部署形态（`allow_resolver_local_path` 是 true 还是 false）

## 提 PR 前

- 保持**零第三方依赖**（插件只用 NekroAgent 自带能力，解析器只用 Node 标准库）
- 插件改动请先本地 `python3 -m py_compile plugin/plugin.py`
- 解析器改动请先 `node --check resolver/resolver.js && node --check resolver/agent.js`
  （CI 也会跑这两条）
- **绝对不要提交**：`config.json`、`config.yaml`、`downloads/`、日志、`.browser-profile*`、
  任何真实 token / 服务器地址 / QQ 号。CI 里有一道扫描会拦。
- 新增配置项时，同步更新 `plugin/config.example.yaml`、`resolver/config.example.json` 和两份 README

## 代码风格

- Python：4 空格缩进，类型标注尽量写，中文注释写清「为什么」而不是「做了什么」
- JavaScript：与现有文件一致（CommonJS、2 空格缩进、单引号）
- 提交信息建议 `feat: / fix: / docs: / chore:` 前缀

## 本地怎么测

插件：装到 NekroAgent 的 `plugins/workdir/video_forward/`，重启容器，看
`GET /plugins/local_video.video_forward/` 返回的 `version` 与 `resolver.online`。

解析器：`node resolver.js once "<公开链接>" "<Edge 路径>"` 可以**不经过服务器**单独跑一次解析，
输出完整 JSON —— 调试解析逻辑时最方便。

## 许可证

本项目采用 [PolyForm Noncommercial 1.0.0](LICENSE)：个人使用、修改、再分发允许，**禁止商业用途**。

提交 PR 即表示你同意以该许可发布你的贡献（并确认你有权这样做）。
