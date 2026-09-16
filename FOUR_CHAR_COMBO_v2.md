# 四人连招固定脚本（FourCharComboTask）权威事实库 v2

> 交接用权威文档。**新对话请先完整读完本文再动代码。**
> 本文只保留**最终结论**；已删除过程记录、重复说明与被推翻的旧结论。
> 最后更新：见 git 提交历史（`git log --oneline -5`）。

---

## 0. 定位

固定编队（1 残虹 / 2 达芙蒂尔 / 3 伊洛伊 / 4 早雾）的严格时序固定连招，**完全绕开 planner**。`AutoCombatTask` 保持原样给其他编队用；本脚本是**独立触发任务**，手动启用，不要与 `AutoCombatTask` 同开。

---

## 1. 连招规范（最终，改动前先对齐）

### 1.1 开局（opener，每场战斗一次）

**入战前预判**：未入战且残虹在场时，脚本**只轮询检测金 E，不做任何键鼠操作**（长按由玩家自己预判敌人出现）；检测到金 E 后点 E、切达芙蒂尔，并在**入战前**等达芙蒂尔 Q 可用即放；随后入战再继续。

1. **残虹金 E**：长按左键约 0.7s 变金（99% 情况，不超过 0.8s）→ 变金瞬间松开、点 E。
2. 切**达芙蒂尔**，点 Q（入战前已放则跳过）。
3. 切**伊洛伊**，点 E。
4. 切**早雾**，点 Q，再点 E（观察到 E 进 CD 即切）。
5. 切**残虹**：双 Q → 二连。
6. 切**伊洛伊**：Q → 浮游炮。
7. 切**残虹**二连 → 进入与达芙蒂尔互切（见 1.2 达芙蒂尔循环）。

**中途脱战要中止**：开场序列期间 `check_combat` 被抑制（避免大招特写被误判脱战），所以每一步之间用 `_opener_combat_lost(tag)` 自己判断一次：`in_combat()` 连续为 False 达到 `OPENER_COMBAT_LOST_GRACE=2s` 就中止开场，并清掉 `_opener_gold_e_done` / `_precombat_daffodill_q_done`，让下一场战斗重新从金 E 开始。
- 为什么需要：开场序列本身要 20~30s 且**没有** `in_combat()` 检查；如果第一个敌人金 E 后就死了，脚本会继续把达芙蒂尔 Q / 伊洛伊 E / 早雾 Q+E / 残虹双 Q / 浮游炮全部打完，而且因为开场记忆没清，第二个敌人出现时**不会再放金 E**。
- 为什么不直接开 `check_combat`：`in_combat()` 在残虹双 Q 的连续特写里会短暂变 False，直接判定会把开场打断（旧 bug）。`in_combat()` 本身要连续丢失 Lv/target ~3.5s 才会返回 False，再加 2s 宽限，避免特写误判。

### 1.2 主循环（起点 = 伊洛伊）

- **伊洛伊**：切到后先点 **E**；Q 可放 → Q + 浮游炮；不可放 → 垫刀（在伊洛伊身上打 `PAD_FIELD_TIME=1.5s` → 残虹二连 → 切回伊洛伊）重复直到 Q 可放。**浮游炮之后先切残虹二连，再切早雾**（`_zankou_combo_switch(self.sakiri)`）。
- **早雾**：Q 可放 → Q；不可放 → 同样垫刀；然后点 **E**（观察到进 CD 即切残虹）。
- **残虹固定步骤**（双 Q **只在这里**，读 `get_cd("ultimate")`）：
  - Q 亮 → 双 Q → 二连；
  - Q 没亮但 CD < `ZANKOU_Q_READY_WINDOW=2s` → 留场连点左键等 Q 亮 → 双 Q → 二连；
  - CD ≥ 2s → 只做二连。
- **达芙蒂尔循环**（重复直到残虹环合满）：达芙蒂尔 Q/E 能放就放，否则连点普攻；在场 `DAFFODILL_FIELD_TIME=1.5s` → 切残虹二连；**若 Q 可放**，放完 Q（可控后）立即切残虹二连，不等 1.5s。
- **环合 ≥90% 不再互切**：`_zankou_combo_switch` 里读 `cycle_ratio()`，`>= CYCLE_STAY_RATIO(0.9)` 时**不再切达芙蒂尔**，而是留在残虹身上连点左键（`_stay_until_cycle_full`）直到环合满，然后切伊洛伊。
- 残虹环合满 → 切**伊洛伊** → 回主循环起点。

### 1.3 残虹二连

长按左键（最低 `COMBO_HOLD_MIN=0.7s`、最长 `COMBO_HOLD_MAX=2.0s`，轮询 `Labels.zankou_skill_gold`，阈值 `GOLD_THRESHOLD=0.7`）→ 松开 → 等 `COMBO_RELEASE_GAP=0.06s` → 单击左键 → 等 `COMBO_CLICK_GAP=0.05s` → 立即切人。闪避反击导致 E 变金也走这套。

### 1.4 残虹双 Q（最终实现）

1. 切残虹后先 `_wait_in_team(ENTRY_SKILL_WAIT=1.6s)` 等脱离切人/入场动画；
2. **连按 Q**（`send_ultimate_key` 每 ~`Q_PRESS_INTERVAL=0.12s`，冷却中按键被忽略）；
3. `_press_q_through_animations()` 依次确认 **4 个阶段**：`enter1`（第 1 段进特写）→ `exit1` → `enter2`（第 2 段进特写）→ `exit2`。每阶段要求 `is_in_team()` 稳定保持 `ANIMATION_STABLE_TIME=0.3s` 才算确认，防止特写期间状态抖动把动画数错；
4. 4 阶段全部确认后 `_wait_cd_ticking()`：等右下角 Q 冷却数字**首次变小**，然后上层才做二连。

> 为什么是"4 阶段确认后再等 CD 变化"，而不是固定 `CD ≤ 19.7` 或"CD 首次变小"：
> 一段 Q 结束、二段 Q 之前 CD 会先小幅跳一次；如果从双 Q 一开始就盯 CD，会在**第 1 段动画刚结束**时就触发，二连提前好几秒（表现为"长按太早、没生效"）。现在 CD 只在 `exit2` 之后才开始采样，所以看到的一定是二段特写结束、CD 恢复计时后的第一次变化。

> 依据：`BaseChar._wait_action_animation` 就是用 `is_in_team` 判断大招动画进入/脱离；多场日志证实特写期间 `is_in_team` 连续 False ~2s。

### 1.5 伊洛伊浮游炮（最终实现）

点 Q → `_wait_iroi_cutscene()` 等脱离大招动画（`is_in_team` 恢复）→ 复用**原版** `iroi._wait_ultimate_unfreeze`（内部 `mouse_down` 长按，等待信号 = `box_ultimate` 图标变化 / Q 不可用）→ `mouse_up` → `sleep IROI_FUNNEL_POST_SLEEP=0.3s` → **单击左键**。
- 结尾 `sleep 0.3s + 单击左键` 是本脚本额外要求，原版没有。

### 1.6 声音触发：闪避 与 闪避成功反击

两条音效各司其职，**不再是"听到警报就反击"**：

- **攻击警报音**（`dodge.wav` / `counter.wav`）→ 只按闪避（`d`+`lshift`），不做反击。
- **闪避成功音**（`dodge_success.wav`，阈值 = 配置项 `Dodge Success Threshold`，默认 **0.3**）→ 触发反击连招：
  - **当前是残虹**：**点按左键 0.08s → 等 0.18s → 残虹二连**（长按仍按到金 E，`_hold_until_gold`）。实测手动中位：点按后 0.27s 起长按、长按 1.09s、短按 0.10s（取偏低值是为了抢时间）。
  - **非残虹**：保持原逻辑 —— 连点左键 + 连点切人键（切残虹）`SOUND_IMMEDIATE_SPAM_TIME=1.2s`，随后主循环 `_switch_to(zankou)` + 残虹二连。

- **反击连招可被新警报打断**：残虹二连**长按期间**若又听到攻击警报，立即打断 → **闪避 → 点按左键 → 再等 0.18s → 再打二连**。
  - 机制：`SoundCombatContext._notify_task_alert()` 在动作入队**之前**回调任务的 `on_sound_alert()`（跑在声音监听线程），任务只置 `_alert_interrupt` 事件；`_zankou_combo_interruptible()` 在长按轮询里检查该事件。不依赖 `sleep_check`，因为连招期间已经 `skip.all = True`。
- **为什么不用警报音当"闪避成功"判据**：实测同一份录音里警报音检出 17 次、闪避成功音 15 次，时间对不上（警报普遍早 0.45~0.6s），且约 3 次闪避完全没有警报音。
- 闪避成功音模板取自 `logs/15次闪避.wav` 的 `64.0~64.2s`（0.2s，正好等于 `sample_len`），离线验证 15/15、事件分 0.76~1.00、事件外背景 ≤0.21。
- 注意：日志里的分数是**首次越过阈值的那个窗口**的分数（不是峰值），所以阈值 0.45 实测余量只有 0.006、会漏检，才降到 0.3。

### 1.7 环合 / 入场技

- 环合值每个角色独立；角色不在场时不变；只在残虹在场时读残虹的。
- 残虹环合满必须**直接由残虹切伊洛伊**（唯一例外：开局那次残虹切达芙蒂尔）。
- **入场技**：切人前读**上一任** `is_cycle_full()`；满则设 `ENTRY_SKILL_WAIT=1.6s` 入场窗口。入场技期间按 Q 无效 → 放 Q 要连按；若新上场是**残虹**，先等 1.6s 再长按。

### 1.8 其他约定

- **可控** = 冷却数字开始跳 **或** Q 图标由亮变灭（原本就灭的不算）。
- **E 切人规则**：所有角色点 E 后，观察到 E 进入 CD（已释放）即可立即切人，不必等动画收尾（`_skill_until_registered`）。
- 固定 1~4 号位；取消"检测四人是否都在队里"的保险。
- 声音闪避反击**开启**，会抢占输入（预期内）。

---

## 2. 角色 / 位置 / 资源

| 位置 | 类 | 中文名 | 文件 | 元素 |
|---|---|---|---|---|
| 1 | `Zankou` | 残虹 | `src/char/Zankou.py` | RED |
| 2 | `Daffodill` | 达芙蒂尔 | `src/char/Daffodill.py` | PURPLE |
| 3 | `Iroi` | 伊洛伊 | `src/char/Iroi.py`（继承 `Support`） | GREEN |
| 4 | `Sakiri` | 早雾 | `src/char/Sakiri.py`（继承 `Support`） | RED |

- 相关标签（`src/Labels.py`）：`zankou_skill_gold`、`zankou_skill_purple`、`zankou_ult_purple`、`ult_ready`、`box_ultimate`、`box_skill`、`health_bar_slash`、`is_current_char`。
- 模板资源在 `assets/`（`coco_annotations.json` + `images/`）；`ok_templates/` 是子模块。
- `process_feature.py` 里 `zankou_skill_gold` **没有**专用滤镜（原图颜色模板匹配）；`ult_ready` 有 `ultimate_ready_filter`。

---

## 3. 文件与注册

- 任务实现：`src/tasks/trigger/FourCharComboTask.py`（继承 `BaseCombatTask, TriggerTask`；复用截图/OCR/输入/切人，**不用 planner**）。
- 注册：`src/config.py` 的 `trigger_tasks`：`["src.tasks.trigger.FourCharComboTask", "FourCharComboTask"]`。
- 任务名 `四人连招`；默认 `_enabled=False`；`trigger_interval=0.1`。

---

## 4. 代码结构

### 4.1 主要方法

| 方法 | 职责 |
|---|---|
| `load_chars` | 按固定位置构造 4 个角色，绑定声音动作 |
| `run` | 入口：未入战 → `_precombat_gold_e`；入战 → `_run_rotation` |
| `_precombat_gold_e` / `_precombat_daffodill_q` | 入战前**只检测**金 E / 达芙蒂尔 Q，检测到才操作 |
| `check_combat` / `_suspend_combat_check` | 紧输入序列期间抑制战斗检测，避免特写误判脱战打断 |
| `_run_rotation` | `_opener` → `while in_combat(): _loop_once` |
| `_opener` / `_loop_once` | 1.1 开局 / 1.2 主循环一轮 |
| `_opener_combat_lost` | 开场序列中途的脱战判断（带 `OPENER_COMBAT_LOST_GRACE` 宽限），脱战即中止并清开场记忆 |
| `_zankou_fixed_step` | 早雾之后的残虹固定步骤（双 Q 只在这里），带 Q 状态诊断日志 |
| `_daffodill_until_cycle_full` / `_daffodill_window` | 达芙蒂尔循环（1.5s 窗口 / Q 可用即走） |
| `_pad_until_q` | 伊洛伊/早雾 Q 不可放时的垫刀；记录 `_pad_target` 供声音反击用 |
| `_skill_until_registered` | 只在 E 图标亮时连按 E 到 E 进 CD 即返回（替代阻塞的 `click_skill`） |
| `_zankou_gold_e` / `_zankou_combo` / `_hold_until_gold` | 开局金 E / 二连 / 长按轮询金 E |
| `_zankou_double_q` / `_press_q_through_animations` | 双 Q（连按 Q + 确认 enter1/exit1/enter2/exit2 + 等 CD 变化） |
| `_zankou_combo_switch` / `_stay_until_cycle_full` | 切残虹二连后按 `cycle_ratio()` 决定切谁；环合 ≥0.9 时留场打到满 |
| `_cast_q` / `_press_q_ready` / `_press_q_until_registered` / `_q_registered` | 单 Q：连按到注册 + 等可控 |
| `_iroi_q_funnel` / `_wait_iroi_cutscene` | 浮游炮 / 等脱离大招动画 |
| `_wait_controllable` / `_wait_cd_ticking` / `_wait_in_team` | 可控 / 等 Q 冷却首次变小 / 脱离动画 |
| `_maybe_log_combat_state` | 低频打印各脱战信号，定位"敌人已死但 in_combat 仍为 True" |
| `_sound_dodge_action` / `_sound_counter_action` | 听到攻击警报：只按闪避（反击已改由闪避成功音触发） |
| `_sound_dodge_success_action` | 听到闪避成功音：残虹 → 点左键 0.08s + 等 0.18s + 二连；非残虹 → `_sound_immediate_reaction` |
| `_sound_immediate_reaction` / `_maybe_handle_sound_counter` | 非残虹反击：连点左键 + 连点切人键，随后主循环切残虹打二连 |
| `_switch_to` / `_ensure_current` | 切人：先 `_wait_in_team` 等脱离动画，再 `_confirm_switch` |
| `_confirm_switch` | **自研切人确认**：重按切人键直到图像确认目标上场；不采信 `active health change`，不额外点击 |
| `_action_log` / `_set_action_phase` / `_close_action_log` | 独立键鼠日志（见 7） |
| `click` / `send_key` / `send_key_down` / `send_key_up` / `mouse_down` / `mouse_up` | 覆写记录真实操作后转发 `super()` |

### 4.2 关键常量（`FourCharComboTask`）

```
COMBO_HOLD_MIN=0.7  COMBO_HOLD_MAX=2.0  COMBO_POLL_INTERVAL=0.05
COMBO_RELEASE_GAP=0.06  COMBO_CLICK_GAP=0.05  GOLD_THRESHOLD=0.7
DAFFODILL_FIELD_TIME=1.5  PAD_FIELD_TIME=1.5  IROI_FUNNEL_POST_SLEEP=0.3
Q_READY_TIMEOUT=5.0  Q_REGISTER_TIMEOUT=3.0  Q_DOUBLE_TIMEOUT=8.0  Q_PRESS_INTERVAL=0.12
ENTRY_SKILL_WAIT=1.6  SUPPRESS_SWITCH_CLICK=True  SWITCH_CONFIRM_TIMEOUT=3.0
SKILL_REGISTER_TIMEOUT=2.0  DAFFODILL_SKILL_REGISTER_TIMEOUT=0.5
CYCLE_BAR_VISIBLE_MIN_PIXELS=20  IROI_FUNNEL_ANIMATION_TIMEOUT=5.0
CONTROLLABLE_TIMEOUT=10.0  ZANKOU_Q_READY_WINDOW=2.0
ANIMATION_STABLE_TIME=0.3  CYCLE_STAY_RATIO=0.9  COMBAT_STATE_LOG_INTERVAL=2.0
OPENER_COMBAT_LOST_GRACE=2.0
SOUND_IMMEDIATE_SPAM_TIME=1.2  SOUND_SUCCESS_CLICK_DOWN=0.08  SOUND_SUCCESS_WAIT=0.18  SCRIPT_TICK=0.05
ACTION_LOG_PATH=logs/four_combo_actions.log
```

---

## 5. 检测手段全集（代码库现有）

> 排查/扩展时优先从这里选，不要另造机制。

### 5.1 当前角色 / 切人
| 方法 | 说明 |
|---|---|
| `get_current_char(raise_exception=False)` | 读 `is_current_char` 标志（缓存，不独立检测） |
| `_get_current_char_detection(frame=None, char_count=None)` | 图像匹配当前角色头像，返回 `CurrentCharDetection(index/score/scores/accepted/strong/reason/active_scores)`；**传 `frame` 可绕过 0.8s sticky tracker**。当前角色检测的真值来源 |
| `get_current_char_index(char_count)` | 返回当前 index 或 -1 |
| `is_char_at_index(index, threshold, frame, char_count)` | 框架切人 fallback 用 |
| `_get_char_match_scores(frame, char_count)` | 4 槽位分数（越低越像当前） |
| `is_health_changed(frame)` | 血条颜色快照对比；框架 `active health change` 用它（**会误报，不采信**） |
| `is_in_team(frame)` / `in_team()` | 血条斜杠模板（在队伍/战斗 UI） |
| `_multi_stage_char_match()` / `_get_char_text_box()` / `_update_char_ui_offset()` | 角色名多对比度匹配 + UI 偏移 |
| `_set_current_char(current, switch_to, has_intro)` | 更新 `is_current_char` 标志 |

### 5.2 动画 / 特写
| 方法 | 说明 |
|---|---|
| `is_in_team()` | **大招特写期间 False**（多场日志证实）；`_wait_in_team`、双 Q 动画计数都用它 |
| `task.in_animation`（属性） | 由 `BaseChar._wait_action_animation` 用 `is_in_team` 驱动 |
| `_cycle_bar_white_pixels()` / `_is_cycle_bar_visible()` | 环合条环形白像素（本任务加，**仅对照采样**） |

### 5.3 技能 / 冷却
| 方法 | 说明 |
|---|---|
| `get_cd(name, char_index)` / `has_cd(name, char_index)` | OCR 右下角 CD 数字（`skill`/`ultimate`，扣冻结时间）；**仅当前角色可靠** |
| `box_highlighted(name)` | 技能图标白色像素占比（亮/灭） |
| `available(name, check_color, check_cd)` | 图标亮 **且** 无 CD |
| `BaseChar.skill_available()` / `ultimate_available()` | 当前角色走 `available`；非当前走 `task.ultimate_available(index)`（模板匹配，常 `conf=0.0`，不稳） |

### 5.4 环合
| 方法 | 说明 |
|---|---|
| `cycle_ratio()` | 环合条填充比例（12 点方向 / 6 点方向白像素密度比，2560x1440 下 `944,1316`）；读的是**当前角色** |
| `is_cycle_full()` | `cycle_ratio() > 0.9`（原实现，行为不变） |
| `_cycle_bar_white_pixels()` | 同款环形区域的白像素数（本任务加，仅对照采样） |

### 5.5 战斗
`in_combat()` / `do_check_in_combat()`（scene 缓存 + combat_detect）、`combat_detect(frame, target, lv, force)`、`is_boss()`（boss Lv 文字模板）、`has_health_bar()` / `_find_red_health_bar()`（敌人红血条颜色块）、`find_target(sync, frame, force)`（OpenVINO）、`find_lv(frame, threshold)`、`combat_detect_uncertain`、`combat_detect_state.miss_count`、`_boss_fight`。

**脱战判定链**（`src/combat/CombatCheck.py`，本任务没有额外判定）：
`do_check_in_combat()` → `_check_active_combat()` → `async_combat_detect()`（Lv + target）→ `_update_combat_detect_state()`（`miss_required=1`，首次未命中给 `uncertain_seconds=0.5` 宽限并 `middle_click()` 重锁）→ 宽限过后 `_recover_or_end_combat()`（`target_enemy` 最多 `target_enemy_time_out=3s` 反复 `middle_click` 重锁）→ 全失败才 `reset_to_false()`。
即**至少 ~3.5s 连续丢失 + 反复尝试重锁**才算彻底脱战。

### 5.6 通用视觉 / OCR
`find_one` / `find_feature`（模板匹配）、`find_best_match_in_box` / `find_first_match_in_box`、`ocr(x,y,to_x,to_y, match, ...)`、`openvino_detect(...)`、`calculate_color_percentage(color, box)`、`find_color_rectangles`（ok）。

### 5.7 场景缓存（`src/scene/NTEScene.py`）
`scene.in_combat()` / `set_in_combat()` / `set_not_in_combat()`、`scene.is_in_team(fun)` / `get_is_in_team_record()`、`scene.health_snapshot()` / `clear_health_snapshot()`、`scene.cd_refreshed`。

### 5.8 声音
`SoundListener`（`dodge_score` / `counter_score` / `dodge_success_score`；模板 `assets/sounds/dodge.wav`、`counter.wav`、`dodge_success.wav`）、`SoundCombatContext`（`dodge_action` / `counter_action` / `dodge_success_action` 回调、抢占）、`DodgeCounterTrigger`（`execute_dodge` / `execute_counter_attack` / `execute_dodge_success`）。阈值在配置项 `Sound Trigger Config`：`Dodge Threshold` / `Counter Attack Threshold` / `Dodge Success Threshold`。

---

## 6. 踩坑点（重要，均已是最终结论）

1. **切人检测不可靠 → 自研确认**：框架 `_switch_to_char` 的 `active health change` 会误报（残虹没上场却报成功）；`_get_current_char_detection` 才是真值。现 `_switch_to` 先 `_wait_in_team` 等脱离动画，再 `_confirm_switch` 重按切人键直到图像确认目标上场（`SWITCH_CONFIRM_TIMEOUT=3.0s`），确认后 `_set_current_char`。日志：`four combo switch confirmed -> X in Y.YYs` 或 `not confirmed`。
   - 真值判别：`info_set current char idx X conf Y` 中 `conf=0.750`（= `reject_score`）表示"检测到的当前角色不是目标"。
   - `SUPPRESS_SWITCH_CLICK=True`：跳过框架切人自带的 `switch_char_click` 左键点击（自研切人也不点击），避免"二连前先点按几下"。
2. **`check_combat()` 会触发重新索敌**：`_recover_or_end_combat` → `target_enemy` → `middle_click`，打断输入（日志频繁 `targeting enemy for 3s`）。紧输入序列用 `skip_sleep_checks(check_combat=True)` 跳过，**保留声音抢占**。
   - `BaseChar.click_skill()` 内部 `sleep` 会走 `sleep_check → check_combat`，`skip_sleep_checks` 覆盖不到；大招特写期间误判脱战 → `target_enemy` 空转 ~3s 抛 `NotInCombatException` 打断开局。修法：`_opener` / `_loop_once` 紧序列用 `_suspend_combat_check()`（覆写 `check_combat`，仅本任务生效）；长循环 `_daffodill_until_cycle_full` 不抑制。
3. **`click_skill()` 会阻塞在场窗口**：连按 E 直到技能不可用（最长 15s），会把窗口计时卡死（达芙蒂尔呆场不切）。修法：改用 `_skill_until_registered()`（E 进 CD 即返回）。
4. **Q 会被入场技/切人动画吃掉**：Q 一律"连按到注册"（`_press_q_until_registered`），不要只按一次。
5. **环合满切人触发入场技**：`_switch_to` 在切人**之前**读上一任 `is_cycle_full()`，不是切完再读。
6. **大招动画用 `is_in_team` 判断**：特写期间 `is_in_team` 为 False（多场日志证实），`BaseChar._wait_action_animation` 即用此。旧笔记"特写期间一直 True"已被推翻。
7. **金 E 检测不稳**：`find_one(Labels.zankou_skill_gold)` 原图颜色模板匹配、无专用滤镜，`conf` 实测 0.7~0.9 可检出、有时 0.000（此时二连仍会继续，只 warning）。需要时再考虑加滤镜（参考 `ult_ready`）。
8. **队友 Q 模板不稳**：`task.ultimate_available(index)` 常 `conf=0.0`；能读到当前角色时优先用 `available`。
9. **外置代码限制**：`custom_chars/external_chars` 只能覆盖单个角色，做不了跨角色固定脚本 → 必须独立任务 + 跑源码。
10. **打包版会被更新覆盖**：改 `data/apps/.../repo` 无效。
11. **Python 版本**：项目要求 3.12，必须用 `.venv` 的 3.12。
12. **中文标点**：新增/修改 Python 源码注释与字符串用 ASCII `,` `;`（仓库 AGENTS.md 要求）。
13. **日志脱敏**：不提交用户日志、截图、账号、本机隐私路径。
14. **Z 盘是 ramdisk**：重启清空，仓库必须放 C 盘。

---

## 7. 日志 / 环境 / 命令 / 版本管理

### 日志（排查优先读前两个）
- **流程日志（首选）**：`logs/four_combo.log`。`_ensure_combo_log_handler()` 给 `ok` logger 挂带 `_ComboLogFilter` 的 FileHandler；放行条件 = **日志正文或 logger 名**含 `FourCharComboTask` / `four_combo` / `four char combo` / `CombatCheck` / `Dodge` / `SoundCombatContext` / `SoundListener` 任一。`__init__` 与每次 `run()` 确保 handler 存在。
  - 看 logger 名是为了让这些模块自身的日志也能进来（否则形如 `Zankou skill registered` 这种不含关键词的正文会被漏掉）。
  - 排查"敌人死了但还卡在战斗状态"：搜 `four char combo combat state [tag]`，这行由 `_maybe_log_combat_state()` 每 `COMBAT_STATE_LOG_INTERVAL=2s` 打印一次，含 `in_combat / scene_cache / uncertain / miss / boss_flag / is_boss / lv / target / health_bar`，一眼看出是哪个信号把战斗状态按住了。
- **键鼠日志**：`logs/four_combo_actions.log`。格式 `HH:MM:SS.mmm +间隔s [phase] 操作`；每次启动写 `==== session YYYY-MM-DD HH:MM:SS ====`。phase 取值：`precombat_gold_e` / `precombat_daffodill_q` / `opener` / `loop` / `pad_until_q` / `zankou_gold_e` / `zankou_enter` / `zankou_combo` / `zankou_double_q` / `zankou_cycle_full` / `iroi_funnel` / `daffodill_window` / `sound_success` / `sound_success_interrupt`。本地生成物，不要提交。
- 全量日志：`logs/ok-script.log`（每天午夜轮转、保留 7 天）。

### 环境
- 仓库：`C:\tool\ok-nte-src`（git，分支 `main`，子模块 `ok_templates`）。
- 虚拟环境：`.venv`，**Python 3.12.13**（`uv sync`）。
- 代理（装依赖）：`http://127.0.0.1:7897`。
- 启动：`start.bat`（自动请求管理员权限，跑 `.venv\Scripts\python.exe main.py`）。

### 常用命令（仓库根目录）
```powershell
.\.venv\Scripts\python.exe -m py_compile src\tasks\trigger\FourCharComboTask.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "*.py"
uv run --with ruff ruff check src\tasks\trigger\FourCharComboTask.py
```

### 版本管理
- **每次改动后**：`git add <改动文件>` + `git commit -m "<说明>"`；不提交 `logs/`、`screenshots/`、`configs/`、`cache/` 等本地生成物。
- 已知 git 邮箱是占位符（推 GitHub 前需改）。

---

## 8. 相关文档 / 参考

- 仓库出招表开发指南：`docs/zh-CN/development/combat-planner.md`（planner 版；本脚本绕开它）。
- 框架源码：`.venv/Lib/site-packages/ok/`（ok-script 2.0.4）。
- 角色基类：`src/char/BaseChar.py`；战斗基类：`src/combat/BaseCombatTask.py`；当前角色检测：`src/utils/current_char_detector.py` + `src/tasks/mixin/CharUIMixin.py`。
- 参考实现：`src/char/Hotori.py`（复杂协作）、`src/char/Iroi.py`（浮游炮 `_wait_ultimate_unfreeze`）、`src/char/Daffodill.py`、`src/char/Zankou.py`。
- 手动键鼠录制工具：`tools/record_input.py` + `tools/record_input.cmd`（管理员启动，GUI 窗口；按钮开始/停止，热键 `F10` 开始 / `F12` 停止）。日志写 `logs/input_record_*.log`，格式与 `four_combo_actions.log` 一致，用于对齐手动与脚本时序。依赖 `pynput`（已在 `.venv`）。
  - 该工具**同时监听"闪避成功音"**（模板 `assets/sounds/dodge_success.wav`，阈值 `DODGE_SUCCESS_THRESHOLD=0.3`，与程序配置项 `Dodge Success Threshold` 默认值一致；走 `SoundListener` 同一套 WASAPI 进程回环 + 匹配），命中时在事件流插入 `sound dodge_success score=...`。GUI 有勾选框开关和实时分数显示，用来测量"闪避成功后隔多久才长按/二连"。
- 游戏音频录制工具：`tools/game_recorder_gui.py` + `tools/record_game_audio.cmd`（WASAPI 内录 loopback；按钮开始/停止，热键 `F11` 开始 / `F12` 停止），输出 `logs/game_audio_output.wav`，用于制作声音模板。依赖 `pyaudiowpatch` + `keyboard`，装在**仓库外的独立环境** `C:\tool\.venv-tools`，不碰项目 `.venv` 与全局 Python（建环境命令见启动脚本头部注释）。
