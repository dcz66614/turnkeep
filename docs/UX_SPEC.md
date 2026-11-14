# Turnkeep 工作台 UX 规范 (T029)

状态：已冻结（R3）。API 字段缺失时提 schema 变更单由 Muse 裁定，
前端不猜测关系、不伪造进度。

## 原则

- 主界面只显示四件事：正在做什么、可依赖的决定、需要重做的工作、
  最近变更。技术细节（epoch、revision、hash）按需展开，不堆在主操作区。
- `active` 的意思是"当前可用"，不是"事实保证"。未验证的内容一律标
  `未验证`，不写成已确认。
- 错误状态必须给可操作的下一步（例如"重新读取后重算再提交"），
  绝不让用户误以为已经恢复。
- 没有无法关闭的 modal；每个弹窗都有明确的关闭与取消。
- 中文文案可读；英文接口（API 字段、错误码）保持稳定。
- 脚本模拟（demo / 录制）一律打标签 `SCRIPTED_SIMULATION_NO_MODEL_CALLS`，
  不冒充真实模型调用。

## 首发四个区域

1. **项目总览 (Overview)** — 项目列表与切换；当前项目的决定、
   待重做项、最近变更摘要。空项目显示"暂无记录"，不显示假数据。
2. **当前视图 (Current)** — 当前可用的记录（active），每条带来源
   （source id:type）与 revision；点击展开详情、历史与来源。
3. **历史 (History)** — 事件时间线（remember / correct / retract /
   resolve / finish），统一时间格式（ISO 8601，本地时区标注），
   每条事件可追溯到记录 id。
4. **影响与恢复 (Impact & Recovery)** — 需求变更的影响路径
   （AND 父项、taint 传播）、冲突候选隔离区、resume / rebase 操作。
   旧 finish 被拒绝时显示 STALE_SNAPSHOT 的 changed_keys 与下一步。

## 旅程 → 页面映射

| 旅程 | 页面 | 关键状态 |
|---|---|---|
| J01 核心：记住 → 开始 → 纠错 → 旧 finish 被拒 → 恢复 → 重算提交 | Overview + Impact | 正常、STALE_SNAPSHOT、已恢复、已提交（receipt） |
| J02 双客户端接力 | Handoff 面板（Overview 内） | 交接 brief、接收方恢复状态；真实客户端未就绪时显示 NOT_RUN，不伪造 |
| J03 需求更改与恢复 | Impact | 旧值 / 新值 / 来源 / 受影响结果 / 原因；重算后提交 |
| J04 重启和恢复 | History + Overview | 重启后视图与历史一致；从备份恢复后 epoch 与有效性一致 |
| J05 选择性迁移 | Overview（导出/导入） | 选中项目导出，来源、状态、依赖、冲突与信任边界保留 |

## 界面状态（每种状态都有 fixture）

- `empty` — 无记录：显示"暂无记录"，给"记下第一条决定"的入口。
- `loading` — 加载中：骨架屏，不显示旧数据的过期快照。
- `error` — 加载/提交失败：错误码 + 一句话原因 + 下一步操作
  （重试 / 检查本地服务是否在运行）。
- `conflict` — 存在冲突记录：进入候选隔离区，显示全部候选
  （id + revision），必须显式选择后才能 resolve。
- `stale` — 旧 finish 被拒：显示 changed_keys 与新值，操作是
  resume → 重算 → 重新 finish。
- `recovery` — 恢复中：显示 resume 的 diff（changed / added / removed）
  与 rebase 次数（最多 2 次自动 rebase，第 3 次进入隔离）。
- `scripted` — 脚本模拟：所有演示数据打标签，不与真实记录混淆。

## 无障碍

- 语义化 HTML（button / nav / main / section），键盘可操作全部主流程；
  颜色不作为唯一的状态区分（同时有文字标签）。
- 不引入复杂可视化框架；不把数据库名词（表名、列名）当作主操作文案。

## 技术栈

轻量 TypeScript + Vite + 可访问 HTML 组件。状态机与视图模型见
`ui/src/types.ts`；fixture 见 `tests/ui/fixtures/`。
