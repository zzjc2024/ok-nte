# 四人连招固定脚本（FourCharComboTask）开发记录 / 交接文档

> 这是本项目的**权威交接文档**。新对话请先完整读本文，再动代码。
> 最后更新：见 git 提交历史（`git log --oneline -5`）。

---

## 0. 一句话背景

为一套固定编队（1 残虹 / 2 达芙蒂尔 / 3 伊洛伊 / 4 早雾）实现**严格时序的固定连招**，**完全绕开 planner** 的动态评分与协作调度。`AutoCombatTask` 保持原样，给其他编队用；本脚本作为独立触发任务，手动启用。

---

## 1. 权威连招规范（用户口述整理，改动前必须先对齐这里）

### 1.1 开局（每场战斗仅一次）

> **入战前预判（脚本行为）**：未入战时若残虹在场，脚本**只轮询检测金 E，不做任何键鼠操作**（不自动长按、不点击、不切人）。**长按由玩家自己预判敌人出现提前操作**；脚本一旦检测到金 E，立即点 E、切达芙蒂尔，并在**入战前**等待达芙蒂尔 Q 可用后自动放 Q；随后检测到进入战斗才继续第 4 步（切伊洛伊放 E）。对应 `_precombat_gold_e` / `_precombat_daffodill_q`，入战前放过的达芙蒂尔 Q 在 `_opener` 里会跳过。

1. 默认由**残虹**（固定 1 号位）进入战斗。
2. **残虹金 E**：长按左键，约 0.5~1.5s 后 E 变金色（99% 情况 0.7s，不会超过 0.8s）；变金瞬间**松开再点 E**。
   - 若此时怪物发动强攻击，会触发工具的声音自动闪避；**闪避反击也会让 E 变金**，此时直接点 E。
   - 之后残虹**不再放 E**（除循环里可能因闪避再次金 E）。
3. 切**达芙蒂尔**，点 Q（若已在入战前放过则跳过）。
4. 达芙蒂尔**可控后**，切**伊洛伊**，点 E。
5. 立即切**早雾**，点 Q。
6. 早雾**可控后**，点 E，**观察到 E 进 CD 立即切残虹**（不等动画收尾）。
7. 残虹**放两次 Q**（升级效果：第一段 Q 后图标仍亮，第二段后才会灭）。
8. 残虹**可控后**，做**残虹二连**。
9. 切**伊洛伊**，点 Q → **触发浮游炮**。
10. 切**残虹二连**。
11. 切**达芙蒂尔**，按 E，然后连点左键；达芙蒂尔**在场 1.5 秒后**切残虹二连；再切达芙蒂尔连点；1.5 秒后切残虹二连……**循环直到残虹环合值满**。
12. 切**伊洛伊** → 进入主循环起点。

### 1.2 主循环（起点 = 伊洛伊）

- **伊洛伊**：
  - 切到伊洛伊后先点 **E**（获取增伤，观察到 E 进 CD 即继续）；
  - Q 可放 → 直接放 Q → 触发浮游炮（Q 顺带回血）；
  - Q 不可放 → 点按 1 次左键 → 切残虹二连 → 切回伊洛伊，重复直到 Q 可放 → 放 Q → 触发浮游炮。
- **早雾**：
  - Q 可放 → 点 Q；不可放 → 同样垫刀（点 1 次左键 → 残虹二连 → 切回早雾），直到 Q 可放 → 点 Q；
  - 点 **E**，**观察到 E 进 CD 立即切残虹**（不等动画收尾）。
- **残虹固定步骤**（双 Q **只在这里放**，读 `get_cd("ultimate")`）：
  - Q 亮 → 双 Q → 二连；
  - Q 没亮但 CD < 2s → **留场连点左键直到 Q 亮** → 双 Q → 二连；
  - CD ≥ 2s → 只做二连。
- **达芙蒂尔循环**（重复直到残虹环合满）：
  - 达芙蒂尔 Q/E **能放就放**，不能就**连点普攻**；
  - 达芙蒂尔**在场 1.5 秒**（真实时间，含大招动画）→ 切残虹二连；
  - **若达芙蒂尔 Q 可放**：放完 Q（**可控后**）**立即**切残虹二连，不等 1.5 秒。
- 残虹环合满 → 切**伊洛伊** → 回到主循环起点。

### 1.3 残虹二连（核心操作）

**长按左键（最低 0.7s）→ 检测到 E 变金色 → 松开 → 等 0.1s → 单击左键 → 等 0.05s → 立即切人。**
- 闪避反击导致 E 变金也走这套。
- 长按过早/过晚都不行；没检测到金 E 不许切人（当前实现会 warning，但仍会继续，需实测调）。

### 1.4 残虹双 Q

1. 第一段 Q（连按到注册）；
2. `wait_until(ultimate_available)` **等 Q 重新亮起**（真实表现：第一段动画结束后 Q 是灭的，**可控之后 Q 会亮起**）；
3. 第二段 Q（连按到注册）；
4. `_wait_controllable`。

### 1.5 浮游炮（伊洛伊）

点 Q → **特写一结束就开始长按左键** → **直到冷却数字开始跳才松手** → 睡 0.3s → **单击左键**。

### 1.6 闪避反击反应

- 闪避动作 = 原闪避（`d`+`lshift`）**+ 补一次左键**（反击）。
- 触发后**第一时间连点左键 + 连点切人键**（切人短时间可能被拒，所以连点）。
- 之后：
  - **非残虹**触发 → 立即切残虹二连；
  - **残虹**触发 → 切达芙蒂尔，Q/E 能放就放，不能就连点左键普攻；**1 秒后**切回残虹继续循环。

### 1.7 环合值规则

- 环合值**每个角色各自独立**；角色不在场时环合值**不会变化**。
- 只在**残虹在场时**检测残虹的环合值。
- 残虹环合满时，必须**直接由残虹切换到伊洛伊**（唯一例外：开局那次残虹切达芙蒂尔）。
- **入场技**：切人前读**上一任**的环合值；若满，则新上场角色会触发入场技，入场技动画期间按 Q 无效 → 放 Q 要**连按**；若新上场是**残虹**，则先等 **1.6s** 再开始长按。

### 1.8 其他约定

- **可控判定** = 冷却数字开始跳 **或** Q 图标由亮变灭（若一开始图标就是灭的，不算）。
- 所有 Q 之后的操作，一律**动画结束/可控后**再进行。
- **E 切人规则**：所有角色（残虹金 E / 伊洛伊 / 早雾 / 达芙蒂尔）点 E 后，只要**观察到 E 进入 CD**（说明已释放）即可**立即切人**，不必等动画收尾，切人也不会打断 E。实现见 `_skill_until_registered`。
- 固定 1~4 号位；**取消**“检测四人是否都在队里”的保险。
- “立即切换”先接受框架 ~0.3s 的切人确认延迟，稳定后再考虑硬切。
- 声音闪避反击**开启**，会抢占输入（在预期之内）。
- 达芙蒂尔 E 冷却 16s，1.5 秒窗口内不会重复放。

---

## 2. 角色 / 位置 / 资源对照

| 位置 | 类 | 中文名 | 文件 | 元素 |
|---|---|---|---|---|
| 1 | `Zankou` | 残虹 | `src/char/Zankou.py` | RED |
| 2 | `Daffodill` | 达芙蒂尔 | `src/char/Daffodill.py` | PURPLE |
| 3 | `Iroi` | 伊洛伊 | `src/char/Iroi.py`（继承 `Support`） | GREEN |
| 4 | `Sakiri` | 早雾 | `src/char/Sakiri.py`（继承 `Support`） | RED |

相关模板/标签（`src/Labels.py`）：`zankou_skill_gold`、`zankou_skill_purple`、`zankou_ult_purple`、`ult_ready`、`box_ultimate`、`box_skill`、`health_bar_slash`、`is_current_char`。
- 模板资源在 `assets/`（`coco_annotations.json` + `images/`）；`ok_templates/` 是子模块。
- `process_feature.py` 里 `zankou_skill_gold` **没有**专用滤镜；`ult_ready` 有 `ultimate_ready_filter`。

---

## 3. 文件与注册

- 任务实现：`src/tasks/trigger/FourCharComboTask.py`（继承 `BaseCombatTask, TriggerTask`，复用截图/OCR/输入/切人确认，**不用 planner**）。
- 注册：`src/config.py` 的 `trigger_tasks`：
  ```python
  ["src.tasks.trigger.FourCharComboTask", "FourCharComboTask"],
  ```
- 任务名：`四人连招`；默认 `_enabled=False`；`trigger_interval=0.1`。

---

## 4. 代码结构与状态机

### 4.1 主要方法

| 方法 | 职责 |
|---|---|
| `load_chars` | 按固定位置构造 4 个角色，绑定声音动作 |
| `run` | 触发任务入口：未入战时做 `_precombat_gold_e`，入战后跑 `_run_rotation` |
| `_precombat_gold_e` | 入战前**只检测**金 E（不做键鼠操作）；检测到 → 按 E、切达芙蒂尔、入战前放 Q |
| `_precombat_daffodill_q` | 入战前在达芙蒂尔身上**只检测** Q，可用即放（不要求入战，非阻塞） |
| `_reset_precombat` | 重置 `_opener_gold_e_done` / `_precombat_daffodill_q_done` / 声音待处理标记 / 日志 phase / 战斗检测抑制；`enable` 与 `combat_end` 调用 |
| `check_combat` / `_suspend_combat_check` | 覆写 `check_combat`；紧输入序列(开局)期间抑制战斗检测，避免大招特写被误判脱战打断 |
| `_run_rotation` | `_opener` → `while in_combat(): _loop_once`（不再强制先切残虹，避免打断入战前已切到位的达芙蒂尔） |
| `_opener` | 1.1 的开局序列；入战前已放达芙蒂尔 Q 时跳过该步 |
| `_action_log` / `_set_action_phase` / `_close_action_log` | 独立轻量键鼠操作日志（见第 8 节） |
| `click` / `send_key` / `send_key_down` / `send_key_up` / `mouse_down` / `mouse_up` | 覆写以记录真实键鼠操作后转发 `super()` |
| `_loop_once` | 1.2 的主循环一轮 |
| `_zankou_fixed_step` | 早雾之后的残虹固定步骤（双 Q 只在这里） |
| `_daffodill_until_cycle_full` / `_daffodill_window` | 达芙蒂尔循环；窗口内 E 用 `_skill_until_registered`，保证 `DAFFODILL_FIELD_TIME` 计时有效 |
| `_skill_until_registered` | 连按 E 直到 E 进 CD（已释放）即返回；替代阻塞的 `click_skill()`，实现“观察到 CD 就切人” |
| `_pad_until_q` | 伊洛伊/早雾 Q 不可放时的垫刀循环 |
| `_zankou_gold_e` / `_zankou_combo` | 开局金 E / 残虹二连 |
| `_hold_until_gold` | 长按轮询金 E（最低 0.7s，最长 2s，记录 conf） |
| `_zankou_double_q` | 双 Q（等 Q 重新亮起） |
| `_cast_q` / `_press_q_ready` / `_press_q_until_registered` / `_q_registered` | 单 Q：连按到注册 + 等可控 |
| `_iroi_q_funnel` | 浮游炮 |
| `_wait_controllable` / `_wait_cd_ticking` | 可控判定 |
| `_sound_dodge_action` / `_sound_counter_action` / `_sound_immediate_reaction` / `_maybe_handle_sound_counter` / `_sound_reaction_zankou` | 闪避反击与反应 |
| `_switch_to` / `_ensure_current` | 切人（切前读上一任环合设入场窗口） |

### 4.2 关键常量（`FourCharComboTask`）

```
COMBO_HOLD_MIN=0.7  COMBO_HOLD_MAX=2.0  COMBO_POLL_INTERVAL=0.05
COMBO_RELEASE_GAP=0.1  COMBO_CLICK_GAP=0.05
SKILL_REGISTER_TIMEOUT=1.0  DAFFODILL_SKILL_REGISTER_TIMEOUT=0.5
GOLD_THRESHOLD=0.7  DAFFODILL_FIELD_TIME=1.5  IROI_FUNNEL_POST_SLEEP=0.3
Q_READY_TIMEOUT=5.0  Q_REGISTER_TIMEOUT=3.0  Q_DOUBLE_TIMEOUT=8.0  Q_PRESS_INTERVAL=0.12
ENTRY_SKILL_WAIT=1.6  CONTROLLABLE_TIMEOUT=10.0  ZANKOU_Q_READY_WINDOW=2.0
SOUND_REACTION_DAFFODILL_TIME=1.0  SOUND_IMMEDIATE_SPAM_TIME=1.2  SCRIPT_TICK=0.05
ACTION_LOG_PATH=logs/four_combo_actions.log
```

---

## 5. 关键检测机制与结论

| 需求 | 用法 | 结论 |
|---|---|---|
| 当前角色 Q 按钮亮/灭 | `self.box_highlighted("ultimate")` | 可用；双 Q 靠“第二段后按钮变灭” |
| 当前角色 Q 剩余冷却 | `self.get_cd("ultimate")` / `has_cd`（OCR 右下角，扣冻结时间） | 可用，**仅当前角色** |
| 队友 Q 是否就绪 | `char.ultimate_available()`（非当前走 `task.ultimate_available(index)` 模板） | 模板匹配经常 `conf=0.0`，不稳 |
| 残虹金 E | `find_one(Labels.zankou_skill_gold)` | 无专用滤镜，原图颜色模板匹配，阈值 0.7 常漏检；已加 `conf=` 日志 |
| 残虹环合满 | `is_cycle_full()`（2560x1440 下 `944,1316` 环形白像素密度） | 读的是**当前角色**；必须在切人**之前**读上一任 |
| 入场技 | 切人前读上一任 `is_cycle_full()`，满则设 1.6s 入场窗口 | 入场技期间按 Q 无效 → 连按 Q |
| 关卡剩余时间 | 无通用检测 | 读不到（`heist_timer` 只判断是否在劫案中） |
| 战斗 UI 是否可见 | `is_in_team()`（`health_bar_slash`） | **NTE 大招特写期间一直 True**，不能判断动画 |

---

## 6. 踩坑点（重要）

1. **`is_in_team()` 不能判断大招动画**：NTE 的 Q 特写期间血条斜杠不消失，`is_in_team()` 一直 True → `wait_until(not is_in_team)` 每次超时 2s。已彻底移除基于它的动画等待。
2. **`check_combat()` 会触发重新索敌**：`_recover_or_end_combat` → `target_enemy` → `middle_click`，打断长按/输入（日志里频繁 `targeting enemy for 3s`）。紧输入序列用 `skip_sleep_checks(check_combat=True)` 跳过战斗检查，但**保留声音抢占**（不动 `sound_combat_context`）。
   - **特写误判打断开局**：`BaseChar.click_skill()` 内部 `sleep` 会走 `sleep_check → check_combat`，而 `skip_sleep_checks` 覆盖不到它。大招特写期间战斗检测会短暂判脱战，`target_enemy` 空转 ~3s 后失败抛 `NotInCombatException`，导致 `_opener` 从早雾 Q 处中断并整段重跑（表现为“早雾 Q 接不上残虹双 Q，要等一会”）。修法：`_opener` / `_loop_once` 的紧输入序列用 `_suspend_combat_check()` 抑制 `check_combat`（覆写 `check_combat`，仅本任务生效），长循环 `_daffodill_until_cycle_full` 不抑制，仍靠 `while in_combat()` 检测脱战。
3. **`click_skill()` 会阻塞在场窗口**：`BaseChar.click_skill()` 连按 E 直到技能不可用（最长 `SKILL_TIME_OUT=15s`）。在 `_daffodill_window`（2s）/ `_sound_reaction_zankou`（1s）里直接调用会把窗口计时卡死，导致达芙蒂尔一直呆在场上、无法按时切回残虹（实测声音反击后达芙蒂尔 E 连点 ~1.7s 直到战斗结束）。修法：窗口内改用 `_skill_until_registered()`（连按 E 到 E 进 CD 即返回）。
3. **Q 会被入场技/切人动画吃掉**：Q 一律“连按到注册”（`_press_q_until_registered`），不要只按一次。
4. **环合满切人触发入场技**：`_switch_to` 在切人**之前**读上一任 `is_cycle_full()`；不是切完再读残虹自己的。
5. **切人确认延迟 ~0.3s**；`_switch_to_char` 会反复发切人键直到成功，天然满足“短时间无法切人就连点切人键”。
6. **外置代码限制**：`custom_chars/external_chars` 只能覆盖单个角色，做不了跨角色固定脚本 → 必须独立任务 + 跑源码。
7. **打包版会被更新覆盖**：改 `data/apps/.../repo` 无效。
8. **Python 版本**：项目要求 3.12，本机默认 3.14，必须用 `.venv` 的 3.12。
9. **中文标点**：新增/修改 Python 源码注释和字符串时用 ASCII `,` `;`（仓库 AGENTS.md 要求）。
10. **日志脱敏**：不要提交用户日志、截图、账号、本机隐私路径。
11. **Z 盘是 ramdisk**：重启清空，仓库必须放 C 盘。

---

## 7. 当前状态 / 未解决项 / 下一步

**已实现**：1.1~1.7 的状态机全部落地；编译/lint/`tests.TestCombatPlanner`(94) 通过。

**最近改动**：
- 入战前预判：未入战且残虹在场时**只轮询检测金 E，不做任何键鼠操作**（长按由玩家自己预判操作），检测到即按 E、切达芙蒂尔、入战前自动放 Q；入战后再从切伊洛伊放 E 继续。`_opener` 会跳过入战前已放的达芙蒂尔 Q。
- 新增独立轻量键鼠日志 `logs/four_combo_actions.log`（覆写输入方法记录真实操作 + phase 标记），用于复盘每次残虹二连的按键时序。
- 修复开局/主循环被打断：`_opener` 与 `_loop_once` 的紧输入序列用 `_suspend_combat_check()` 抑制 `check_combat`，避免早雾 Q 特写期间战斗检测误判脱战、`target_enemy` 空转 3s 后打断流程重跑（表现为早雾 Q 接不上残虹双 Q，要等一会）。
- 残虹二连长按→左键间隔改为 `COMBO_RELEASE_GAP=0.1s`（原几乎 0）。
- 达芙蒂尔在场窗口的 E 改为 `_skill_until_registered`，修复声音反击/达芙蒂尔循环里 `click_skill()` 阻塞导致达芙蒂尔卡场、不按时切回残虹。
- 主循环伊洛伊补齐 **E**（原只在开局放，缺增伤）：切伊洛伊 → E → Q → 浮游炮。
- 所有角色 E 统一为 `_skill_until_registered`（残虹金 E / 伊洛伊 / 早雾 / 达芙蒂尔），做到“观察到 E 进 CD 立即切人”，不再等 `click_skill()` 动画收尾。
- 达芙蒂尔在场窗口 `DAFFODILL_FIELD_TIME` 由 2.0s 调整为 **1.5s**。

**实测已知问题（最近一轮日志结论）**：
- `is_in_team` 动画判断失效 → 已修（移除）。
- Q 被吃 → 已改为连按到注册。
- `check_combat` 重新索敌打断 → 已改为紧输入时跳过。
- 金 E 漏检（`zankou gold E not detected`）→ 已加 `conf=` 日志，**待用户提供实测 conf 值**决定是否降 `GOLD_THRESHOLD` 或加专用滤镜。
- 队友 Q 模板 `conf=0.0` → 待处理。
- 声音反应偏激进 → 视实测调 `SOUND_IMMEDIATE_SPAM_TIME` / 阈值。

**下一步（待用户实测反馈）**：
1. 收集 `zankou gold E detected/not detected, conf=...`。
2. 收集 `Q registered` / `Q not registered`、`zankou second Q not ready`。
3. 确认双 Q 第二段判定（“可控后 Q 重新亮起”）是否稳。
4. 确认达芙蒂尔 Q 收尾、垫刀循环、环合满切伊洛伊是否符合预期。
5. 必要时给金色 E 增加预处理滤镜（参考 `process_feature.py` 的 `ult_ready`）。

---

## 8. 环境、运行、日志、版本管理

- 源码仓库：`C:\tool\ok-nte-src`（git，分支 `main`，子模块 `ok_templates`）。
- 虚拟环境：`C:\tool\ok-nte-src\.venv`，**Python 3.12.13**（用 `uv sync` 拉取）。
- 代理（装依赖用）：`http://127.0.0.1:7897`。
- 启动：`start.bat`（自动请求管理员权限，跑 `.venv\Scripts\python.exe main.py`）。
- 日志：`logs\ok-script.log`，每天午夜轮转 `ok-script.YYYY-MM-DD.log`，保留 7 天；`main_debug.py` 同文件、级别 DEBUG。
- 连招键鼠日志：`logs\four_combo_actions.log`（仅本任务写入，追加不轮转）。每行格式
  `HH:MM:SS.mmm +间隔s [phase] 操作`，例如 `[zankou_combo] mouse_down left`；
  每次启动写一行 `==== session YYYY-MM-DD HH:MM:SS ====`。phase 取值：
  `precombat_gold_e` / `precombat_daffodill_q` / `opener` / `loop` / `pad_until_q` /
  `zankou_gold_e` / `zankou_enter` / `zankou_combo` / `zankou_double_q` /
  `iroi_funnel` / `daffodill_window` / `sound_zankou`。该文件属本地生成物，不要提交。
- 常用命令（仓库根目录）：
  ```powershell
  .\.venv\Scripts\python.exe -m py_compile src\tasks\trigger\FourCharComboTask.py
  .\.venv\Scripts\python.exe -m unittest tests.TestCombatPlanner
  uv run --with ruff ruff check src\tasks\trigger\FourCharComboTask.py
  ```
- **版本管理约定：每次改动后**
  ```powershell
  git add -A
  git commit -m "<说明>"
  ```
  不提交 `logs/`、`screenshots/`、`configs/`、`cache/` 等本地生成物。
- 已知 git 邮箱是占位符 `你的GitHub注册邮箱@example.com`（本地提交可用，推 GitHub 前需改）。

---

## 9. 相关文档 / 参考

- 仓库出招表开发指南：`docs/zh-CN/development/combat-planner.md`（planner 版；本脚本绕开它）。
- 框架源码：`.venv/Lib/site-packages/ok/`（ok-script 2.0.4）。
- 角色基类：`src/char/BaseChar.py`；战斗基类：`src/combat/BaseCombatTask.py`。
- 现有 planner 角色实现可参考：`src/char/Hotori.py`（复杂协作）、`src/char/Daffodill.py`、`src/char/Iroi.py`、`src/char/Zankou.py`。
- 手动键鼠录制工具：`tools/record_input.py` + `tools/record_input.cmd`（管理员启动，GUI 窗口）。点窗口按钮开始/停止（热键 `F9` 开始 / `F10` 停止；`Ctrl+Shift+H/J` 可能被中文输入法占用），记录期间实时显示事件，停止时写入 `logs/input_record_*.log`，格式与 `four_combo_actions.log` 一致，便于对手动操作与脚本时序。
