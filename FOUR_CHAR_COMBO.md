# 四人连招固定脚本（FourCharComboTask）开发记录

> 本文记录固定编队连招脚本的设计、关键检测机制、踩坑点和运行方式，供后续维护和继续开发参考。

## 1. 背景与目标

- 目标：为一套固定编队（1 残虹 / 2 达芙蒂尔 / 3 伊洛伊 / 4 早雾）实现一套**严格时序的固定连招**，完全绕开 `planner` 的动态评分与协作调度。
- 位置固定：`0=残虹 1=达芙蒂尔 2=伊洛伊 3=早雾`（`load_chars` 直接按位置构造，不做队伍组成检测）。
- 只应手动启用；不要和 `AutoCombatTask` 同时开（两者都是触发任务，会抢触发循环）。

## 2. 文件与注册

- 任务实现：`src/tasks/trigger/FourCharComboTask.py`（继承 `BaseCombatTask, TriggerTask`，复用截图/OCR/输入/切人确认，不用 planner）。
- 注册：`src/config.py` 的 `trigger_tasks` 增加：
  ```python
  ["src.tasks.trigger.FourCharComboTask", "FourCharComboTask"],
  ```
- 名称：`四人连招`；默认 `_enabled=False`。

## 3. 运行方式

- 源码仓库：`C:\tool\ok-nte-src`（Z 盘是 ramdisk，重启清空，所以放 C 盘）。
- 虚拟环境：`C:\tool\ok-nte-src\.venv`，Python 3.12.13（项目要求 `>=3.12,<3.13`；本机原本只有 3.14/3.11/3.9，用 `uv sync` 自动拉取 3.12）。
- 启动：`start.bat`（自动请求管理员权限，用 `.venv\Scripts\python.exe main.py`）。
- 日志：`logs\ok-script.log`，每天午夜轮转成 `logs\ok-script.YYYY-MM-DD.log`，保留 7 天。`main_debug.py` 不换文件名，只是级别变 DEBUG。
- 常用命令（在仓库根目录）：
  ```powershell
  .\.venv\Scripts\python.exe -m py_compile src\tasks\trigger\FourCharComboTask.py
  .\.venv\Scripts\python.exe -m unittest tests.TestCombatPlanner
  uv run --with ruff ruff check src\tasks\trigger\FourCharComboTask.py
  ```

## 4. 状态机概览

### 4.1 开局（仅一次，`_opener`）

1. 残虹金 E：长按左键（最低 0.7s，最长 2s，轮询 `zankou_skill_gold`）→ 松开 → 点 E。
2. 切达芙蒂尔 → 点 Q。
3. 切伊洛伊 → 点 E。
4. 切早雾 → 点 Q（可控后）→ 点 E。
5. 切残虹 → 双 Q → 二连 → 切伊洛伊。
6. 伊洛伊 Q + 浮游炮。
7. 残虹二连 → 切达芙蒂尔 → 进入达芙蒂尔循环。

### 4.2 主循环（`_loop_once`，起点=伊洛伊）

- 伊洛伊：Q 可放就放；不可放就垫刀（点 1 次左键 → 切残虹二连 → 切回），直到 Q 可放 → Q + 浮游炮。
- 早雾：Q 可放就放；不可放同样垫刀；可控后点 E → 切残虹。
- 残虹固定步骤（`_zankou_fixed_step`）：
  - Q 亮 → 双 Q → 二连；
  - Q 没亮但 `get_cd("ultimate")` 在 (0,2) 秒 → 留场连点左键直到 Q 亮 → 双 Q → 二连；
  - 否则只二连。
- 达芙蒂尔循环（`_daffodill_until_cycle_full`，直到残虹环合满）：
  - 切达芙蒂尔 → E 能放就放 → 连点左键；从切人完成起算满 2s（真实时间）→ 切残虹二连；
  - 达芙蒂尔 Q 能放 → 点 Q → 可控后立即切残虹二连（不等 2s）；
  - 残虹二连后查环合，满则切伊洛伊（退出循环）。
- 环合满 → 切伊洛伊 → 回到循环起点。

### 4.3 残虹二连（`_zankou_combo`）

长按左键（最低 0.7s）→ 轮询金 E（阈值 `GOLD_THRESHOLD`，最长 2s）→ 松开 → 单击左键 → 等 0.05s → 切人。

### 4.4 残虹双 Q（`_zankou_double_q`）

1. 第一段 Q（连按到注册）；
2. `wait_until(zankou.ultimate_available)` 等 Q 重新亮起（真实表现：第一段动画结束后 Q 灭，可控后重新亮起）；
3. 第二段 Q（连按到注册）；
4. `_wait_controllable`。

### 4.5 伊洛伊浮游炮（`_iroi_q_funnel`）

点 Q（连按到注册）→ 按住左键 → 直到冷却数字开始跳才松手 → 睡 0.3s → 单击左键。

### 4.6 闪避反击反应（声音）

- 自定义 `dodge_action` / `counter_action`：闪避 = `d`+`lshift` + 补一次左键；反击 = 左键。
- 触发后**第一时间连点左键 + 连点切人键**（1.2s，用 `time.sleep` 避免嵌套 sleep_check）。
- 循环点检查标志：
  - 非残虹触发 → 切残虹二连；
  - 残虹触发 → 切达芙蒂尔，Q/E 能放就放否则连点左键，1s 后切回残虹。

## 5. 关键检测机制与结论

| 需求 | 用法 | 结论 |
|---|---|---|
| 当前角色 Q 按钮亮/灭 | `self.box_highlighted("ultimate")`（`box_ultimate` 颜色） | 可用；双 Q 靠“第二段后按钮变灭” |
| 当前角色 Q 剩余冷却 | `self.get_cd("ultimate")` / `has_cd`（OCR 右下角数字，扣冻结时间） | 可用，仅当前角色 |
| 队友 Q 是否就绪 | `char.ultimate_available()`（非当前走 `task.ultimate_available(index)` 模板） | 模板匹配经常 conf=0.0，不稳 |
| 残虹金 E | `find_one(Labels.zankou_skill_gold)` | 无专用滤镜，原图颜色模板匹配，阈值 0.7 时经常漏检；已加置信度日志 |
| 残虹环合满 | `is_cycle_full()`（2560x1440 下 `944,1316` 环形白像素密度） | 读的是**当前角色**的环合；必须在切人**之前**读上一任 |
| 入场技判断 | 切人前读上一任 `is_cycle_full()`，满则给新角色 1.6s 入场窗口 | 入场技动画期间按 Q 无效 → 连按 Q |
| 关卡剩余时间 | 无通用检测 | 读不到（`heist_timer` 只判断是否在劫案中） |
| 战斗 UI 是否可见 | `is_in_team()`（`health_bar_slash` 模板） | **NTE 大招特写期间它一直是 True**，不能用来判断动画 |

## 6. 踩坑点（重要）

1. **`is_in_team()` 不能判断大招动画**：NTE 的 Q 特写期间血条斜杠不消失，`is_in_team()` 一直 True，导致 `wait_until(not is_in_team)` 每次超时 2s。已彻底移除基于它的动画等待。
2. **`check_combat()` 会触发重新索敌**：`_recover_or_end_combat` → `target_enemy` → `middle_click`，会打断长按和输入（日志里频繁 `targeting enemy for 3s`）。紧输入序列用 `skip_sleep_checks(check_combat=True)` 跳过战斗检查，但**保留声音抢占**（不动 `sound_combat_context`）。
3. **Q 会被入场技/切人动画吃掉**：所以 Q 一律“连按到注册”（`_press_q_until_registered`），不要只按一次。
4. **环合满切人触发入场技**：`_switch_to` 必须在切人**之前**读上一任的 `is_cycle_full()`，据此设 1.6s 入场窗口；不是切完再读残虹自己的。
5. **`_advance_entry_flow`/请求发布时机**（planner 相关，本任务不用）：但切人确认有 ~0.3s 延迟，时序敏感的操作要留余量。
6. **切人检测**：`_switch_to_char` 会反复发切人键直到检测成功，天然满足“短时间无法切人就连点切人键”。
7. **外置代码限制**：`custom_chars/external_chars` 只能覆盖单个角色，做不了跨角色固定脚本，所以本脚本是独立任务。
8. **打包版会被更新覆盖**：改 `data/apps/.../repo` 无效，必须跑源码。
9. **Python 版本**：项目要求 3.12，本机默认 3.14，必须用 `.venv` 的 3.12 解释器。
10. **中文标点**：新增/修改 Python 源码注释和字符串时用 ASCII `,` `;`，避免全角标点告警（仓库 AGENTS.md 要求）。
11. **日志脱敏**：不要提交用户日志、截图、账号、本机隐私路径。

## 7. 已知不稳定 / 待办

- **金 E 漏检**：`zankou_skill_gold` 模板匹配稳定性不够，已加 `conf=` 日志。若实测 `best conf` 长期在 0.6 左右，应降低 `GOLD_THRESHOLD` 或给金色 E 增加专用预处理滤镜（参考 `process_feature.py` 里 `ult_ready` 的做法）。
- **队友 Q 模板不稳**：`task.ultimate_available(index)` 经常 conf=0.0，垫刀循环里依赖它可能误判。
- **声音反应激进**：闪避反击即时连点切人较频繁，必要时缩短 `SOUND_IMMEDIATE_SPAM_TIME` 或调低声音阈值。
- **未做真实游戏回归**：所有验证仅到编译/lint/planner 单测，游戏内时序需人工实测。
- **达芙蒂尔 Q 收尾、残虹双 Q 第二段判定**等仍需实测确认。

## 8. 版本管理约定

- 每次改动后执行：
  ```powershell
  git add -A
  git commit -m "<说明>"
  ```
- 不要把 `logs/`、`screenshots/`、`configs/`、`cache/` 等本地生成物提交。
