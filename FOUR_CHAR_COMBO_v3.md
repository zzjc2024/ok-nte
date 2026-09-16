# FOUR_CHAR_COMBO v3 — 四人连招固定脚本交接文档

> 只读本文即可接手维护。**结论优先**：每条只写「结论 + 必要证据 + 文件/函数位置」。
> 无法确认的一律标 **「未验证」**，不要当成事实。
> 基线：仓库 `C:\tool\ok-nte-src`，commit `4112808`（2026-09-16），Python 3.12 / `.venv`。
> 本文取代 `FOUR_CHAR_COMBO_v2.md`（已删除）。同一份文档另存于 `C:\gv1.02\tools\FOUR_CHAR_COMBO_v3.md`。

---

## 1. 定位与入口

- 固定编队（1 残虹 / 2 达芙蒂尔 / 3 伊洛伊 / 4 早雾）的严格时序固定连招，**完全绕开 planner**。`AutoCombatTask` 保持原样给其他编队用。
- 独立**触发任务**，手动启用；**不要与 `AutoCombatTask` 同开**。
- 实现：`src/tasks/trigger/FourCharComboTask.py`（继承 `BaseCombatTask, TriggerTask`）。
- 注册：`src/config.py` 的 `trigger_tasks` → `["src.tasks.trigger.FourCharComboTask", "FourCharComboTask"]`。
- 任务名 `四人连招`；`_enabled=False` 默认关；`trigger_interval=0.1`。
- 声音模板：`assets/sounds/`（`dodge.wav`、`counter.wav`、`dodge_success.wav`、`dodge_motion_1/2/3.wav`，每个 wav 旁有同名 `.npy` 缓存）。

---

## 2. 队伍 / 属性 / 环

| 位置 | 类 | 中文名 | 元素（游戏名） | 元素（代码） | 文件 |
|---|---|---|---|---|---|
| 1 | `Zankou` | 残虹 | 咒 | `Element.RED` | `src/char/Zankou.py` |
| 2 | `Daffodill` | 达芙蒂尔 | 暗 | `Element.PURPLE` | `src/char/Daffodill.py` |
| 3 | `Iroi` | 伊洛伊 | 灵 | `Element.GREEN` | `src/char/Iroi.py`（继承 `Support`） |
| 4 | `Sakiri` | 早雾 | 咒 | `Element.RED` | `src/char/Sakiri.py`（继承 `Support`） |

**属性环**（仓库已有，`BaseCombatTask.element_ring` / `element_ring_index`）：

```
White → Green → Red → Purple → Blue → Yellow → White   （首尾相邻）
对应：光=White  灵=Green  咒=Red  暗=Purple  魂=Blue  相=Yellow
```

- **咒(Red) 的邻居是灵(Green) 和暗(Purple)** → 达芙蒂尔/伊洛伊满环合切残虹会触发残虹入场技（见 3.7）。

---

## 3. 连招流程（最终）

### 3.1 开局 opener（每场战斗一次）

入战前（未入战且残虹在场）：`_precombat_gold_e()` **只轮询检测金 E，不做任何键鼠操作**（长按由玩家自己预判敌人出现）；检测到金 E 才点 E、切达芙蒂尔，并在入战前等达芙蒂尔 Q 可用即放（`_precombat_daffodill_q`）。

入战后 `_opener()` 固定序列（`_suspend_combat_check()` 内，避免大招特写被误判脱战）：

1. 残虹**金 E**（`_zankou_gold_e`：长按到变金 → 点 E）
2. 切**达芙蒂尔** → 点 Q（入战前已放则跳过）
3. 切**伊洛伊** → 点 E
4. 切**早雾** → 点 Q → 点 E
5. 切**残虹** → 双 Q → 二连
6. 切**伊洛伊** → Q → 浮游炮
7. 切**残虹**二连（`_zankou_combo_switch`）→ 之后进达芙蒂尔循环

**中途脱战要中止**：每步之间调 `_opener_combat_lost(tag)`，**连续 `OPENER_COMBAT_LOST_GRACE=1.5s` 没有任何敌人信号**就中止并清 `_opener_gold_e_done` / `_precombat_daffodill_q_done`（下一场重新从金 E 开始）。

- 判据 `_enemy_present()` = `is_boss() or find_lv() or find_target() or has_health_bar()`。**不要用 `in_combat()`**（见 §6.2）。
- 双 Q 之前单独再查一次（`zankou_before_double_q`），避免空放大招；`_zankou_double_q()` 自身在 `_enemy_present()` 为 False 时也直接跳过。

### 3.2 主循环（起点 = 伊洛伊）

`_run_rotation()` = `_opener()` → `while in_combat(): _loop_once()`。

- **伊洛伊**：切到后先点 **E**；Q 可放 → Q + 浮游炮；不可放 → 垫刀（`_pad_until_q`：在伊洛伊身上打 `PAD_FIELD_TIME=1.5s` → 残虹二连 → 切回伊洛伊，重复到 Q 可放）。**浮游炮之后先切残虹二连，再切早雾**（`_zankou_combo_switch(self.sakiri)`）。
- **早雾**：Q 可放 → Q；不可放 → 同样垫刀；然后点 **E**。
- **残虹固定步骤**（`_zankou_fixed_step`，**双 Q 只在这里**，读 `get_cd("ultimate")`）：Q 亮 → 双 Q → 二连；Q 没亮但 CD < `ZANKOU_Q_READY_WINDOW=2.0s` → 留场连点左键等 Q 亮 → 双 Q → 二连；CD ≥ 2s → 只做二连。
- **达芙蒂尔循环**（`_daffodill_until_cycle_full` / `_daffodill_window`，重复到残虹环合满）：达芙蒂尔 Q/E 能放就放，否则连点普攻；在场 `DAFFODILL_FIELD_TIME=1.5s` → 切残虹二连；若 Q 可放，放完 Q（可控后）立即切残虹二连，不等 1.5s。
- **环合 ≥ `CYCLE_STAY_RATIO=0.9` 不再互切**：`_zankou_combo_switch` 读 `cycle_ratio()`，≥0.9 时留在残虹身上连点左键（`_stay_until_cycle_full`）直到环合满，然后切伊洛伊。
- 残虹环合满 → 切**伊洛伊** → 回主循环起点。

### 3.3 残虹二连（金 E 长按）

`_zankou_combo()` / `_zankou_gold_e()` / `_zankou_combo_interruptible()` 都走 `_zankou_hold_with_recovery()`：

**长按左键**（`COMBO_HOLD_MIN=0.67s` ~ `COMBO_HOLD_MAX=0.9s`，轮询 `Labels.zankou_skill_gold`，`GOLD_THRESHOLD=0.7`）→ 松开 → 等 `COMBO_RELEASE_GAP=0.06s` → 单击左键 → 等 `COMBO_CLICK_GAP=0.05s` → 立即切人（开局金 E 是松开后点 E）。

- **长按上限绝对不许回到 2s**：金 E 在 ~0.67~0.8s 出现（手动实测长按 0.87~0.99s 是"看到变金后再松手"），0.9s 还没金就是异常。
- **不许重按**：重按会白等 1~2s，限时关卡等于失败。起点由入场技预测（3.7）保证。

没出金 E 时分流（`_hold_until_gold` 返回 `(HoldResult, damaged)`）：

| 情况 | 判据 | 处理 |
|---|---|---|
| 掉血 | 长按期间**多次采样**血条像素数，出现相对历史峰值的下降 | 大概率被打断 → `_dodge_until_triggered()`：连按 shift 直到听到**闪避动作音**确认闪避生效 → 重打长按（最多 `COMBO_DODGE_RETRY_MAX=3` 轮） |
| 没掉血 | 全程无下降 | 可控状态下长按不出金 E 不可能 → `_raise_combo_anomaly()`：落盘现场 → `disable()` 停任务 → 抛 `ZankouComboAnomaly` |
| 一帧血条都没取到 | `health_samples == 0` | 无法证明"没掉血" → 按"被打断"走闪避重试 |

- 血条必须**连续采样**：`_health_pixels()` = 当前角色血条红条掩码非零像素数，判据 `下降 > max(HEALTH_DROP_MIN_PIXELS=4, peak*HEALTH_DROP_RATIO=0.02)`。只取首尾两次会漏掉"掉血→伊洛伊回血→再采样"。
- 闪避重试期间若听到**成功闪避音** → 丢弃队列里的待处理动作（`SoundCombatContext().discard_pending_action()`）后走现成的 `_sound_dodge_success_action()`（点按左键 → 二连），不再补一次。

### 3.4 残虹双 Q

`_zankou_double_q()`：

1. 先 `_press_q_ready()`（Q 就绪；等待期间按 `Q_WAIT_ATTACK_INTERVAL=0.2s` 节流普攻，避免发呆），再 `_wait_in_team(timeout=ENTRY_SKILL_WAIT)`；
2. **连按 Q**（`send_ultimate_key`，间隔 `Q_PRESS_INTERVAL=0.12s`，冷却中按键被忽略）；
3. `_press_q_through_animations()` 依次确认 4 个阶段 `enter1 → exit1 → enter2 → exit2`，每阶段要求 `is_in_team()` 稳定 `ANIMATION_STABLE_TIME=0.3s`（防特写抖动数错动画）；
4. `_wait_double_q_recovery(stage)` —— **两个条件必须同时成立**才交回上层长按：
   - **第 2 段** Q 的动画结束：`stage >= 3`（`enter2` 已确认，绝不能拿第 1 段当数）+ `is_in_team()` 稳定 0.3s 回来；
   - Q 冷却的**原始 OCR 数字**（`_raw_ultimate_cd()`）真正变小。

> 已验证（18:36 实机）：`zankou combo ready in 3.08s (stage=3, cd_raw 19.9 -> 19.8)`，第 2 段 Q → 长按 = **5.3s**，金 E 在长按 0.80s 出现。
> `Q_DOUBLE_TIMEOUT=8.0s` 通常不够确认 `exit2`（双 Q 全程约 11s），日志常见 `stage=3/4`；二连时机**不依赖 `exit2`**。

### 3.5 伊洛伊浮游炮

`_iroi_q_funnel()`：点 Q → `_wait_iroi_cutscene()` 等脱离大招动画（`is_in_team` 恢复）→ 复用**原版** `iroi._wait_ultimate_unfreeze`（内部 `mouse_down` 长按；等待信号 = `box_ultimate` 图标变化 / Q 不可用）→ `mouse_up` → `sleep IROI_FUNNEL_POST_SLEEP=0.3s` → **单击左键**。

- 结尾 `sleep 0.3s + 单击左键` 是本脚本额外要求，原版没有。

### 3.6 环合

- 环合值每个角色独立；角色不在场时不变；只在残虹在场时读残虹的。
- 残虹环合满必须**直接由残虹切伊洛伊**（唯一例外：开局那次残虹切达芙蒂尔）。

### 3.7 入场技（必须提前预测）

游戏机制：**满环合**的角色切到**属性环上相邻**的角色时，新上场角色触发入场技，期间无法控制、按住不生效。

- 判据：`_switch_triggers_entry_skill(current, char)` = 上一任 `current.is_cycle_full()` **且** `_is_adjacent_element(current, char)`（`element_ring` 相邻，含首尾环绕）。
- 计时：`_switch_to()` 里 `_entry_skill_until = 按下切人键的时刻 + ENTRY_SKILL_WAIT=1.1s`；`_hold_until_gold()` 开头 `sleep` 到该时刻再长按。
- 手动实测（2 次：达芙蒂尔→残虹、伊洛伊→残虹）：**切人键 → 长按 = 1.09 / 1.12s**（长按 0.93 / 0.91s，然后单击 0.10s）。
- 入场技期间按 Q 无效 → 放 Q 一律"连按到注册"。
- 日志：`four combo entry skill expected: <上一任>(元素) -> <新角色>(元素), cycle_ratio=..., wait 1.1s`。
- 代码实现与 `cycle_ratio()` 的可靠性 **未验证**（见 §7）。

### 3.8 声音（三条音效各司其职）

| 音效 | 模板 / 阈值 | 行为 |
|---|---|---|
| **攻击警报音** | `dodge.wav` / `counter.wav`，`Dodge Threshold=0.13` / `Counter Attack Threshold=0.12` | 只按闪避（`d`+`lshift`），不做反击 |
| **闪避成功音** | `dodge_success.wav`，`Dodge Success Threshold=0.3` | 触发反击连招 |
| **闪避动作音** | `dodge_motion_1/2/3.wav`（取 max），`Dodge Motion Threshold=0.3` | **只做通知**：确认"闪避真的触发了" |

- **闪避成功反击**（`_sound_dodge_success_action`）：**当前是残虹** → 点按左键 0.08s → 等 0.18s → 残虹二连（长按仍按到金 E）；**非残虹** → `_sound_immediate_reaction`（连点左键 + 连点切人键 `SOUND_IMMEDIATE_SPAM_TIME=1.2s`），随后主循环切残虹打二连。
- **反击可被新警报打断**：长按期间再听到攻击警报 → 打断 → 闪避 → 点按左键 → 再等 0.18s → 再打二连。机制：`SoundCombatContext._notify_task_alert()` 在动作入队**之前**回调任务 `on_sound_alert()`（跑在声音线程，只置 `_alert_interrupt`），`_zankou_combo_interruptible()` 在长按轮询里检查；不依赖 `sleep_check`（连招期间 `skip.all = True`）。
- **闪避动作音**只通知不动作：在 `SoundListener._check_triggers` 里放在节流**之前**、不占用 `_last_trigger_time`，只用自己的 `_dodge_motion_interval=0.3s` 去重；链路 `SoundCombatContext._notify_task_dodge_motion()` → 任务 `on_dodge_motion_sound()`（置 `_dodge_motion_heard`）。用途见 3.3 的闪避重试。
- 为什么不用警报音当"闪避成功"判据：实测同一录音里警报音检出 17 次、闪避成功音 15 次，时间对不上（警报普遍早 0.45~0.6s），约 3 次闪避完全没有警报音。

### 3.9 其他约定

- **可控** = 冷却数字开始跳 **或** Q 图标由亮变灭（原本就灭的不算）。
- **E 切人规则**：所有角色点 E 后，观察到 E 进入 CD（已释放）即可立即切人，不必等动画收尾（`_skill_until_registered`）。
- 固定 1~4 号位；取消"检测四人是否都在队里"的保险。
- 声音闪避反击**开启**，会抢占输入（预期内）。
- 切人后先 `sleep SWITCH_SETTLE_TIME=0.1s` 再长按（`_zankou_combo_switch`）。

---

## 4. 关键时序与常量

### 4.1 手动实测（用于对齐脚本）

| 场景 | 实测 | 说明 |
|---|---|---|
| 双 Q → 长按 | **5.04 / 5.09 / 5.25s**（3 次） | 从第 2 次 Q 按下算；脚本按 `_wait_double_q_recovery` 实测 5.3s |
| 入场技：切人键 → 长按 | **1.09 / 1.12s**（2 次） | 达芙蒂尔→残虹、伊洛伊→残虹 |
| 长按（二连） | 0.87~0.99s | 含"看到变金后松手"的反应时间 |
| 长按 → 松开 → 单击 | 0.14~0.31s | 脚本用 `COMBO_RELEASE_GAP=0.06`（未验证是否偏短） |
| 单击时长 | 0.10~0.12s | 脚本用 `click()` 默认 down_time |
| 闪避 → 长按 | 0.07~0.11s | 右键/shift 都是闪避 |
| 闪避成功音 → 长按 | 中位 0.46s | 脚本用 点按 0.08s + 等 0.18s |

### 4.2 常量（`FourCharComboTask`）

```
ZANKOU_INDEX=0  DAFFODILL_INDEX=1  IROI_INDEX=2  SAKIRI_INDEX=3
COMBO_HOLD_MIN=0.67  COMBO_HOLD_MAX=0.9  COMBO_POLL_INTERVAL=0.05
COMBO_RELEASE_GAP=0.06  COMBO_CLICK_GAP=0.05  GOLD_THRESHOLD=0.7
COMBO_DODGE_RETRY_MAX=3  HEALTH_DROP_RATIO=0.02  HEALTH_DROP_MIN_PIXELS=4
DODGE_RETRY_TIMEOUT=3.0  DODGE_RETRY_INTERVAL=0.15
DAFFODILL_FIELD_TIME=1.5  PAD_FIELD_TIME=1.5  IROI_FUNNEL_POST_SLEEP=0.3
Q_READY_TIMEOUT=5.0  Q_WAIT_ATTACK_INTERVAL=0.2  Q_REGISTER_TIMEOUT=3.0
Q_DOUBLE_TIMEOUT=8.0  Q_PRESS_INTERVAL=0.12
ENTRY_SKILL_WAIT=1.1  SWITCH_SETTLE_TIME=0.1  SUPPRESS_SWITCH_CLICK=True
SWITCH_CONFIRM_TIMEOUT=3.0  SKILL_REGISTER_TIMEOUT=2.0
DAFFODILL_SKILL_REGISTER_TIMEOUT=0.5
CYCLE_BAR_VISIBLE_MIN_PIXELS=20  IROI_FUNNEL_ANIMATION_TIMEOUT=5.0
CONTROLLABLE_TIMEOUT=10.0  ZANKOU_Q_READY_WINDOW=2.0
ANIMATION_STABLE_TIME=0.3  CYCLE_STAY_RATIO=0.9  COMBAT_STATE_LOG_INTERVAL=2.0
OPENER_COMBAT_LOST_GRACE=1.5
SOUND_IMMEDIATE_SPAM_TIME=1.2  SOUND_SUCCESS_CLICK_DOWN=0.08  SOUND_SUCCESS_WAIT=0.18
SCRIPT_TICK=0.05  CD_OCR_BOX=(0.8594, 0.8847, 0.9578, 0.9139)
ACTION_LOG_PATH=logs/four_combo_actions.log
```

### 4.3 关键方法索引

| 方法 | 职责 |
|---|---|
| `run` / `_run_rotation` | 入口（未入战 → `_precombat_gold_e`；入战 → `_opener` + `while in_combat(): _loop_once`） |
| `_opener` / `_loop_once` / `_opener_combat_lost` / `_enemy_present` | 3.1 / 3.2 |
| `_zankou_fixed_step` | 早雾之后的残虹固定步骤（双 Q 只在这里） |
| `_daffodill_until_cycle_full` / `_daffodill_window` | 达芙蒂尔循环 |
| `_pad_until_q` | 伊洛伊/早雾 Q 不可放时垫刀；记 `_pad_target` 供声音反击用 |
| `_skill_until_registered` | E 进 CD 即返回（替代阻塞的 `click_skill`） |
| `_zankou_gold_e` / `_zankou_combo` / `_zankou_combo_interruptible` | 金 E / 二连 / 可打断二连（都走 `_zankou_hold_with_recovery`） |
| `_hold_until_gold` | 长按轮询金 E，返回 `(HoldResult, damaged)`，期间多次采样血条 |
| `_zankou_hold_with_recovery` | 长按 + 失败分流（3.3） |
| `_dodge_until_triggered` / `_press_dodge` | 连按 shift 到听到闪避动作音（`DODGE_RETRY_TIMEOUT=3s`，超时抛异常） |
| `_health_pixels` / `_health_drop_margin` / `_raise_combo_anomaly` | 血条像素数 / 掉血判据 / 落盘 + `disable()` + 抛 `ZankouComboAnomaly` |
| `_zankou_double_q` / `_press_q_through_animations` / `_wait_double_q_recovery` / `_raw_ultimate_cd` | 3.4 |
| `_zankou_combo_switch` / `_stay_until_cycle_full` | 切残虹二连后按 `cycle_ratio()` 决定去向 |
| `_cast_q` / `_press_q_ready` / `_press_q_until_registered` / `_q_registered` / `_q_wait_attack` | 单 Q：连按到注册 + 等可控 |
| `_iroi_q_funnel` / `_wait_iroi_cutscene` | 浮游炮 / 等脱离大招动画 |
| `_switch_to` / `_switch_triggers_entry_skill` / `_is_adjacent_element` / `_confirm_switch` | 3.7 + 自研切人确认 |
| `_sound_*` / `on_sound_alert` / `on_dodge_motion_sound` | 3.8 |
| `_action_log` / `_set_action_phase` / `_close_action_log` / `_dump_q_cd_state` | 日志（§9） |
| `click` / `send_key*` / `mouse_down` / `mouse_up` | 覆写记录真实操作后转发 `super()` |

---

## 5. 检测手段（结论 + 位置）

排查/扩展时优先用这些，不要另造机制。

### 5.1 当前角色 / 切人
- `_get_current_char_detection(frame=None, char_count=None)`：图像匹配当前角色头像，返回 `index/score/scores/accepted/strong/reason`；**传 `frame` 可绕过 0.8s sticky tracker**。**当前角色检测的真值来源。**
- `get_current_char(raise_exception=False)`：只读 `is_current_char` 标志（缓存）。`get_current_char_index()` / `is_char_at_index()` / `_get_char_match_scores()`。
- `is_health_changed(frame)`：血条快照对比，框架 `active health change` 用它；**会误报，不采信**。
- 真值判别：`info_set current char idx X conf Y` 中 `conf=0.750`（= `reject_score`）表示"检测到的当前角色不是目标"。

### 5.2 动画 / 特写
- `is_in_team(frame)`：血条斜杠模板。**大招特写期间 False**（多场日志证实）；`_wait_in_team`、双 Q 动画计数、`BaseChar._wait_action_animation` 都用它。特写期间状态会抖动，所以判定要"稳定 0.3s"。
- `task.in_animation`：由 `BaseChar._wait_action_animation` 驱动。
- `_cycle_bar_white_pixels()` / `_is_cycle_bar_visible()`：环合条环形白像素（仅对照采样）。

### 5.3 技能 / 冷却
- `get_cd(name, char_index)` / `has_cd(...)`：OCR 右下角 CD 数字（`skill`/`ultimate`，扣冻结时间）；**仅当前角色可靠**；**会随时间漂移，不能判断"冷却是否真的在计时"**（见 §6.3）。
- `refresh_cd()` + `task.cds[idx]["ultimate"]`：CD 的**原始 OCR 数字**（不扣时间）→ `_raw_ultimate_cd()`。
- `box_highlighted(name)`（图标亮/灭）、`available(name, check_color, check_cd)`（亮 且 无 CD）。
- `BaseChar.skill_available()` / `ultimate_available()`：当前角色走 `available`；非当前走 `task.ultimate_available(index)`（模板匹配，常 `conf=0.0`，不稳）。

### 5.4 环合
- `cycle_ratio()`：环合条填充比例（12 点 / 6 点方向白像素密度比，2560x1440 下 `944,1316`）；读**当前角色**。
- `is_cycle_full()`：`cycle_ratio() > 0.9`（原实现，行为不变）。

### 5.5 玩家血条（掉血判据）
- `_get_health_box(frame)` / `_get_health_snapshot(frame)`：当前角色血条红条颜色掩码（`char_health_color`）；位置来自 `is_in_team()` 斜杠框右移。
- `_health_pixels()`（本任务加）：血条掩码非零像素数。
- `is_health_changed(frame)`：框架版模板匹配，**和框架切人判断共用 `scene` 快照** → 本任务不用。

### 5.6 战斗 / 脱战
`in_combat()` / `do_check_in_combat()`、`combat_detect(frame, target, lv, force)`、`is_boss()`、`has_health_bar()` / `_find_red_health_bar()`、`find_target(sync, frame, force)`（OpenVINO）、`find_lv(frame, threshold)`、`combat_detect_uncertain`、`combat_detect_state.miss_count`、`_boss_fight`。

脱战判定链（`src/combat/CombatCheck.py`，本任务无额外判定）：
`do_check_in_combat()` → `_check_active_combat()` → `async_combat_detect()`（Lv + target）→ `_update_combat_detect_state()`（`miss_required=1`，首次未命中给 `uncertain_seconds=0.5` 宽限并 `middle_click()` 重锁）→ `_recover_or_end_combat()`（`target_enemy` 最多 `target_enemy_time_out=3s` 反复 `middle_click`）→ 全失败才 `reset_to_false()`。
即**至少 ~3.5s 连续丢失 + 反复重锁**才算彻底脱战。

### 5.7 通用视觉 / OCR
`find_one` / `find_feature`、`find_best_match_in_box` / `find_first_match_in_box`、`ocr(x,y,to_x,to_y,match,...)`、`openvino_detect(...)`、`calculate_color_percentage`、`find_color_rectangles`。

### 5.8 场景缓存（`src/scene/NTEScene.py`）
`scene.in_combat()` / `set_in_combat()` / `set_not_in_combat()`、`scene.is_in_team(fun)`、`scene.health_snapshot()` / `clear_health_snapshot()`、`scene.cd_refreshed`（每帧 `reset_scene()` 置 False，所以 `refresh_cd` 每帧重新 OCR）。

### 5.9 声音
- `SoundListener`（`dodge_score` / `counter_score` / `dodge_success_score` / `dodge_motion_score`；多模板取 max；`dodge_motion` 走 `dodge_motion_sample_paths`）。
- `SoundCombatContext`（动作回调 + 抢占；`on_dodge_motion_triggered` 只通知任务；`discard_pending_action()` 丢弃待处理动作）。
- `DodgeCounterTrigger`（`execute_dodge` / `execute_counter_attack` / `execute_dodge_success`）。
- 阈值配置项 `Sound Trigger Config`：`Enable Sound Trigger` / `Dodge All Attacks` / `Dodge Threshold` / `Counter Attack Threshold` / `Dodge Success Threshold` / `Dodge Motion Threshold`。

---

## 6. 可靠性规则 / 坑（改动前必读）

1. **切人检测不可靠 → 自研确认**：框架 `_switch_to_char` 的 `active health change` 会误报（残虹没上场却报成功）。`_switch_to()` 先 `_wait_in_team()` 等脱离动画，再 `_confirm_switch()` 重按切人键直到图像确认目标上场（`SWITCH_CONFIRM_TIMEOUT=3.0s`），然后 `_set_current_char`。`SUPPRESS_SWITCH_CLICK=True` 跳过框架切人自带的左键点击（避免"二连前先点按几下"）。
2. **不要用 `in_combat()` 判断"敌人还在"**：关卡切换/敌人刚死时它会因为异步检测 pending（`combat_detect.value is None` → 算在战斗）或"重新索敌成功"而继续返回 True（日志 `miss=0` 且 `is_boss/lv/target/health_bar` 全 False 却仍 in_combat）。用 `_enemy_present()` 的原始信号。
3. **`get_cd()` 不能判断"冷却是否开始计时"**：`get_cd = cds["ultimate"] - (now - OCR快照时间)`，冷却数字只要挂在屏幕上就会一路变小（即使游戏冻结冷却）。游戏在 Q 动画期间把冷却数字**冻结在满值**（实测：Q#2 后 3.1s 读到的原始值仍是 `20.0`），所以要看**原始数字**是否变小。
4. **不要"动画一结束就长按"**：双 Q 后必须等"第 2 段动画结束 + Q 冷却原始数字变小"（3.4）；切人/入场技期间按住不生效，必须等 `_entry_skill_until`（3.7）。
5. **长按上限 0.9s、不许重按**（3.3）。
6. **紧输入序列抑制战斗检测**：`check_combat()` 会触发 `_recover_or_end_combat → target_enemy → middle_click` 打断输入。`_opener` / `_loop_once` 用 `_suspend_combat_check()`（覆写 `check_combat`，仅本任务生效）；更细的循环用 `skip_sleep_checks(check_combat=True)`。**注意 `BaseChar.click_skill()` 内部的 `sleep` 会走 `sleep_check → check_combat`，`skip_sleep_checks` 覆盖不到**，所以本任务不用 `click_skill()`。
7. **`click_skill()` 会阻塞在场窗口**（连按 E 直到技能不可用，最长 15s，会把窗口计时卡死）→ 一律用 `_skill_until_registered()`。
8. **Q 会被入场技/切人动画吃掉** → Q 一律"连按到注册"。
9. **金 E 检测不稳**：`find_one(Labels.zankou_skill_gold)` 是原图颜色模板匹配、无专用滤镜，`conf` 实测 0.7~0.9 可检出、有时 0.000。
10. **队友 Q 模板不稳**：`task.ultimate_available(index)` 常 `conf=0.0`；能读到当前角色时优先用 `available`。
11. **声音模板必须从"干净录音"里切**：闪避动作音混在音乐里时相关值主要来自音乐（从带音乐的录音切模板 → 闪避点最低分低于背景最高分，完全不可用）。必须让素材"只按闪避、不移动"（边移动会把脚步声叠进来）。
12. **外置代码限制**：`custom_chars/external_chars` 只能覆盖单个角色，做不了跨角色固定脚本 → 必须独立任务 + 跑源码。
13. **打包版会被更新覆盖**：改 `data/apps/.../repo` 无效。
14. **环境**：Python 3.12（必须 `.venv`）；仓库放 C 盘（Z 盘是 ramdisk，重启清空）。
15. **日志/隐私**：不提交 `logs/`、截图、账号、本机路径等本地生成物。
16. **Python 注释与字符串用 ASCII `,` `;`**（仓库 AGENTS.md 要求）。
17. **不要在轮询热路径里直接 `log_info`**：`Iroi._wait_ultimate_unfreeze` 会以 ~250 次/秒轮询 `ultimate_available()`，已用 `run_with_interval(..., 1, action_name=f"ultimate_available_log_{index}")` 节流到每角色 1 条/秒。

---

## 7. 已知限制 / 未验证项

**未验证（代码已写，但没有实机跑过 / 没有足够样本）**
- 入场技预测（`_switch_triggers_entry_skill` + `ENTRY_SKILL_WAIT=1.1`）：手动时序已验证（1.09/1.12s），但代码路径没跑过实机。
- `cycle_ratio()` 的可靠性：18:36 那次"伊洛伊(灵)→残虹(咒)"相邻却没预测出入场技，疑似 `is_cycle_full()` 误读，**未确认**。下次看 `four combo entry skill expected` 是否出现。
- 掉血阈值 `HEALTH_DROP_RATIO=0.02` / `HEALTH_DROP_MIN_PIXELS=4`：只有 1 个样本（满血 `health_peak=306` 像素、0.9s 采到 13 次）。
- 闪避动作音的**游戏内**分数（离线验证 20/20、30/30、10/10，命中分 0.94~1.00，干净背景 ≤0.05）；阈值 0.3 是否合适待实测。
- 闪避重试（`_dodge_until_triggered` 连按 shift + 闪避动作音确认）与异常停止路径的实机表现。
- `SWITCH_SETTLE_TIME=0.1` 是否必要（有了入场技预测后可能多余）。
- `COMBO_RELEASE_GAP=0.06`（手动实测 0.14~0.31s，未验证是否偏短）。

**已知限制（暂不改）**
- `exit2` 基本确认不了（`Q_DOUBLE_TIMEOUT=8s` < 双 Q 全程 ~11s），二连时机不依赖它。
- 早雾 E 经常放不出来（`Sakiri skill not registered within 2.0s`，`lit=0` / `in_team=False`）。
- 切人偶发确认失败（`four combo switch not confirmed, want X, got -1 (reason=rejected)`）。
- `_wait_controllable()`（单 Q 用）仍用 `get_cd` 漂移判定，未改成原始值。

---

## 8. 修改时必须遵守的规则

1. 先读本文再动代码；只改本任务相关文件，不顺手重构。
2. 保持"只通过 UI / 系统输出交互"的边界（截图、OCR、模板、声音、窗口 API、普通键鼠）。
3. 长按 ≤ `COMBO_HOLD_MAX=0.9s`、**不许重按**；长按起点必须由 `_wait_double_q_recovery()` / `_entry_skill_until` 保证。
4. 冷却判断用**原始 OCR 数字**，不用 `get_cd()`。
5. 敌人是否还在用 `_enemy_present()`，不用 `in_combat()`。
6. 不采信框架的 `active health change` 切人判定；不改自研 `_confirm_switch` 的语义。
7. 声音：新增音效按 `dodge_motion` 的模式加（独立阈值 + 独立去重 + 不占用 `_last_trigger_time`）；模板必须从干净录音切。
8. 改完必须：`py_compile` + `ruff` + `unittest discover -s tests -p "*.py"`，然后 `git add` + `git commit`。
9. 更新本文对应章节（时序/常量/方法索引/未验证项）。

---

## 9. 日志 / 环境 / 命令 / 工具

### 日志（排查优先读前两个）
- **流程日志**：`logs/four_combo.log`。`_ensure_combo_log_handler()` 给 `ok` logger 挂带 `_ComboLogFilter` 的 FileHandler；放行条件 = **日志正文或 logger 名**含 `FourCharComboTask` / `four_combo` / `four char combo` / `CombatCheck` / `Dodge` / `SoundCombatContext` / `SoundListener` 任一（`__init__` 与每次 `run()` 确保 handler 存在）。
  - 脱战排查：`four char combo combat state [tag]`（`_maybe_log_combat_state()` 每 `COMBAT_STATE_LOG_INTERVAL=2s`），含 `in_combat / scene_cache / uncertain / miss / boss_flag / is_boss / lv / target / health_bar`。
  - 现场落盘：`_dump_q_cd_state(tag)` 写 `logs/four_combo_<tag>_<时间>.png`（整帧）+ `_cdbar.png`（右下技能条裁剪），并打一行 `four combo q cd dump [tag] saved=... ocr=[名字@(x,y)...] cds={...} current=...`。tag：`q_not_ready` / `combo_ready_timeout` / `combo_anomaly`。
  - 二连关键日志：`zankou double q recovery t=... in_team=... cd_raw=... baseline=...`、`zankou combo ready in X.XXs (stage=S, cd_raw A -> B)`、`zankou gold E detected, conf=...`、`zankou gold E not detected, best conf=... (hold=0.9s, health_samples=N, health_peak=P, damaged=...)`、`zankou gold E missing but damaged, dodge then retry (i/3)`、`four char combo anomaly: ...`。
  - 声音：`Dodge MOTION TRIGGERED! score: ...`、`Dodge SUCCESS TRIGGERED! ...`、`Audio monitoring - ... dodge_motion_score: ...`（每 20s）。
- **键鼠日志**：`logs/four_combo_actions.log`，格式 `HH:MM:SS.mmm +间隔s [phase] 操作`，每次启动写 `==== session ... ====`。phase：`precombat_gold_e` / `precombat_daffodill_q` / `opener` / `loop` / `pad_until_q` / `zankou_gold_e` / `zankou_enter` / `zankou_combo` / `zankou_double_q` / `zankou_cycle_full` / `iroi_funnel` / `daffodill_window` / `sound_success` / `sound_success_interrupt` / `dodge_retry`。
- 全量：`logs/ok-script.log`（午夜轮转、保留 7 天）。

### 环境与命令
- 仓库 `C:\tool\ok-nte-src`（git，分支 `main`，子模块 `ok_templates`）；`.venv` Python 3.12.13（`uv sync`）；装依赖代理 `http://127.0.0.1:7897`；启动 `start.bat`（管理员，跑 `.venv\Scripts\python.exe main.py`）。

```powershell
.\.venv\Scripts\python.exe -m py_compile src\tasks\trigger\FourCharComboTask.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "*.py"
uv run --with ruff ruff check src\tasks\trigger\FourCharComboTask.py
```

- 不提交 `logs/`、`screenshots/`、`configs/`、`cache/`。已知 git 邮箱是占位符（推 GitHub 前需改）。

### 录制 / 素材工具
- 手动键鼠录制：`tools/record_input.py` + `tools/record_input.cmd`（管理员，GUI；热键 `F10` 开始 / `F12` 停止），日志 `logs/input_record_*.log`（格式同 `four_combo_actions.log`），用于对齐手动与脚本时序。同时监听"闪避成功音"（模板 `assets/sounds/dodge_success.wav`，阈值 0.3），命中插入 `sound dodge_success score=...`。
- 游戏音频内录：`tools/game_recorder_gui.py` + `tools/record_game_audio.cmd`（WASAPI loopback；热键 `F11` 开始 / `F12` 停止），输出 `logs/game_audio_output.wav`，用于制作声音模板。依赖 `pyaudiowpatch` + `keyboard`，装在**仓库外独立环境** `C:\tool\.venv-tools`（不碰项目 `.venv`）。

### 参考
- 出招表开发指南：`docs/zh-CN/development/combat-planner.md`（planner 版；本脚本绕开它）。
- 框架源码：`.venv/Lib/site-packages/ok/`（ok-script 2.0.4）。
- 角色基类 `src/char/BaseChar.py`；战斗基类 `src/combat/BaseCombatTask.py`（`element_ring` / `cycle_ratio` / `_apply_sound_config`）；当前角色检测 `src/utils/current_char_detector.py` + `src/tasks/mixin/CharUIMixin.py`。
- 角色实现：`src/char/Zankou.py`、`Daffodill.py`、`Iroi.py`（浮游炮 `_wait_ultimate_unfreeze`）、`Sakiri.py`；复杂协作参考 `src/char/Hotori.py`。
