# 送礼 / 好感度功能改造方案（v4，待确认）

目标：在现有「羁遇赠礼」标签页上新增
1. **库存预警看板**：记录每日送礼消耗，自动算「当前库存还能送几天」。
2. **好感升级计算器**：设置主角特技等级（1-5，5% 加成阈值 = 特技等级 + 3），
   在角色单独页面选「后续买哪种礼物」，自动算「还要买多少个才能到目标等级」。

本文只是方案，未动代码。末尾「待确认问题」需要用户补充信息后再开工。

---

## 1. 现有实现侦察

| 位置 | 作用 | 关系 |
| --- | --- | --- |
| `src/gifts/GiftDb.py` | JSON 持久化（`schema_version=1`），`profiles` 字典 | **扩 schema 到 v2** |
| `src/gifts/GiftManager.py` | 单例；profile 增删改查、帧图片落盘 | 复用，新增字段读写走这里 |
| `src/gifts/layout.py` | 赠礼页坐标比例（已核对：5×2 主网格、列距 0.0651、行距 0.1351 **仍正确**） | 扩展新框 |
| `src/tasks/daily/GiftTask.py` | 自动赠礼；`MAX_TOTAL_GIFTS=10` + `MAX_GIFTS_PER_CHARACTER=3` + 每角色 `target_count`；已能 OCR 等级「10」判满级、OCR `(\d+)/3` | **接入：读快照 + 记消耗** |
| `src/ui/GiftManagerTab.py` | 单页编辑器（左角色列表 / 右设置卡 + 礼物优先级卡） | **加两块 UI** |
| `src/config.py` | `GiftTask` 已注册日常任务，`GiftManagerTab` 已注册标签页 | 不用改注册 |

---

## 2. 截图实测与已确认规则

### 2.1 经验表（已由用户确认，与实测一致）

| 升级 | 所需经验 |
| --- | --- |
| 1→2 | 500 |
| 2→3 | 1,000 |
| 3→4 | 2,000 |
| 4→5 | 3,500 |
| 5→6 | 5,000 |
| 6→7 | 7,000 |
| 7→8 | 9,000 |
| 8→9 | 12,000 |
| 9→10 | 16,000 |
| 10 | 满级（溢出经验浪费，实测显示 `220`） |

即 `EXP_TABLE = {1:500, 2:1000, 3:2000, 4:3500, 5:5000, 6:7000, 7:9000, 8:12000, 9:16000}`，
`MAX_LEVEL = 10`。**旧代码那份表（含 index0=100、L1=400、L8=12500）是错的，作废。**

### 2.2 赠礼页可 OCR 出的数据（1920x1080 客户区实测）

- **好感度等级**（1/2/3/4/8/9/10）+ **当前级经验/需求**（`1550/12000`、`500/2000`…）。
- **每个礼物格的经验值**（心形数字）+ **库存数量**（角标数字）。
- **每日剩余次数**：底部横幅「今日还能赠送10次礼物」；按钮「赠送3/3」。

### 2.3 已确认规则

- **每日次数**：**每角色 3 次 + 全局 10 次**（对应现有 `MAX_GIFTS_PER_CHARACTER=3` /
  `MAX_TOTAL_GIFTS=10`）。
- **全局 10 次的分配**：**完全照抄自动赠礼的真实行为** —— 按角色顺序，每人最多送
  `target_count`（用户配的「赠送次数」，1-3）次，累计到 10 次为止。
- **礼物档位**：普通礼物（占每日次数）经验值有 **100 / 200 / 400** 三种。
- **特殊礼物**：**不纳入计算**。现有程序已能识别（就是 `Labels.unlimit_gift` 那个角标模板，
  命名有误导），识别后自动标成「不可赠送」、无法选优先级。
- **不存在无限礼物**（用户确认：最便宜的礼物也要花钱买）。所以库存永远是有限的，没有 `∞`。
- **每日额外经验（约会）**：不吃 5% 加成；**每个角色页面各自一个「勾选框 + 数字输入框」**
  （默认 200），勾上就认为该角色每天固定获得这么多经验，绕开「每天只能约会一个角色」。
- **礼物列表比一屏长**，但自动赠礼本来只用首屏可见礼物，所以**不考虑滚动**。

---

## 3. 旧代码（`000送礼好感计算器.py`）审查

核心升级循环（`gain_exp`：按当前等级逐次加经验、边加边升级；5% 判定用「送礼前等级」）**逻辑正确，可直接移植**。要修 4 处：

1. **5% 阈值写死 8**（第 99 行）→ 参数化为 `特技等级 + 3`（1→≤4 … 5→≤8）。
2. **手写信(2000) 在 Day 0 一次性用完，绕过每日上限**（第 140-145 行）→ 按新规则重写。
3. **每日约会 +200 也乘了 1.05**（第 153 行）→ 改为不吃加成（已确认）。
4. **展示的「总经验缺口」没算加成**（第 188 行）→ 偏高。

另：经验表要换成上面那份；`int(base * coef)` 是截断；单角色、无持久化、无历史。

---

## 4. 方案

### 4.1 数据模型（`GiftDb` v2，带迁移）

```jsonc
{
  "schema_version": 2,
  "settings": {
    "protagonist_skill_level": 1,     // 1-5, 5% 阈值 = 该值 + 3
    "daily_gift_limit_per_char": 3,   // 每角色每日上限
    "daily_gift_limit_global": 10     // 全局每日上限
  },
  "profiles": {
    "gift_xxx": {
      // ... 现有字段不动（selected_slots 保留兼容）...
      "priority_gift_ids": ["gift_abc", "gift_def"],  // 新增: 优先级改按礼物身份存
      "bond_level": 8,           // 羁遇等级（OCR 预填, 可手改）
      "bond_exp": 1550,          // 当前级已存经验
      "target_level": 10,        // 目标等级
      "buy_tier": 100,           // 库存耗尽后买哪种档位（100/200/400）
      "daily_extra_exp": 200,    // 每日额外经验（约会）
      "daily_extra_enabled": false
    }
  },
  "gift_catalog": {              // 新增: 礼物目录（按图标模板识别）
    "gift_abc": { "exp": 400, "name": "" }
  },
  "stock": { "gift_abc": 2, "gift_def": 65 },   // 新增: 每件礼物当前库存（OCR 快照）
  "stock_snapshot_at": "2026-09-22T19:56",
  "ledger": {                                   // 新增: 每日消耗
    "2026-09-22": { "sent": { "gift_abc": 2 }, "characters": { "残虹": 3 } }
  }
}
```

v1 库读入时自动补默认值并升版本，`profiles` 原字段不动。

### 4.2 礼物目录：按**用户命名**识别（图标模板只做"继承"）

**实测坑**：同一件礼物在不同角色页的**格位不同、首屏可见集合也不同**。

| 角色 | 首屏主网格前 5 格 |
| --- | --- |
| 残虹 | 票券400×2、饮料100×28、菜篮100×6、药瓶100×3、棉花糖400×65 |
| 早雾 | 兔耳400×1、信400×2、礼盒100×510、奶茶100×7、棉花糖400×65 |
| 阿德勒 | 票券400×2、饮料100×33、零食100×3、牛奶100×3、棉花糖400×65 |

所以**不能按格位聚合库存**（会少算，且每个角色算出来不一样）。

**曾尝试过、已放弃的方案**：纯靠图标自动认身份（感知哈希 / 模板匹配）。
实测 7 张截图：同一礼物跨 slot/跨行/跨角色，`TM_CCOEFF_NORMED` 得分 0.96~1.00、
不同礼物 ≤0.69，看着可行；但**白色系礼物（白条/白旗）和很暗的图标（收音机）在图标
中段细带里区分度不足**，会把两种礼物并成一个（5 种不同特征表示都给出同样错误的聚类）。
自动化认身份不可靠，所以改成下面的方案。

**最终方案（用户命名做身份）**：
- 在"设置礼物优先级"的地方，用户给这 10 个礼物**分别命名**（也可顺手填经验值）。
- **名字相同 = 同一种礼物**（`gift_id` 由归一化后的名字派生；归一化只做 Unicode NFKC、
  去首尾空白、折叠内部空白，**不做**模糊匹配，避免把两种礼物并成一个）。
- 用户点"更新当前角色"时，程序用**图标模板匹配**给出"建议继承"：
  新截图某格图标与用户之前标注过的图标很像（≥0.8）→ 建议沿用之前的名称/经验，
  **最终仍由用户确认**。
- 因此图标模板只用于"建议"，不承担身份判定；白色系/暗图标即使认错也不影响正确性，
  用户改名即可。

存储：
- `gift_catalog: {gift_id: {exp, name}}`（全局，`name` 是用户命名）。
- `profiles[*].slot_gift_ids: {slot: gift_id}`（每角色每格的标注）。
- `gift_configs/gift_icons/<gift_id>.png`（已标注礼物的图标，用于下次建议继承）。

### 4.2.1 礼物的经验值怎么来（当前实现的空白）

**现状**：`GiftManager` 只存 `selected_slots`（首屏 5×2 网格的格位序号）+ 一张截图帧，
判断「送哪个」靠图标模板、判断「能不能送」靠特殊礼物角标模板，**从未读过礼物的经验值**。
所以现在 UI 只能显示「优先级 1/2/3」，不知道是 400 还是 100。

**来源**：经验值就在同一页 —— 每个礼物卡**下沿有个心形数字**（`♥400` / `♥100`）。
主网格 5×2 的 10 个格位每个都有；顶部「角色喜爱」那排没有（那排本来也不参与优先级）。

**做法**：在**采集/更新角色**时（任务线程里）顺手把每格的 `♥数字` OCR 出来，
和图标模板一起写进 `gift_catalog`（`gift_id → exp`）。于是：
- profile 的优先级（`priority_gift_ids`）直接关联到经验值 → UI 可显示「优先级 1 · 票券(400)」；
- 目录里每件礼物自带 `exp`，库存 / 天数 / 计算器都用它。

**两点补充**：
1. **已采集的老角色**：帧图片都还在 `gift_configs/frames/*.png`，做一次性**回填**：
   批量对保存帧 OCR 心形数字（不依赖游戏开着）把经验值补上。
2. 不在 UI 线程做重 OCR（项目规范）→ 经验值一律在采集时或显式「读取」动作里算好存库。

需要新增的坐标：每个礼物格的心形数字框（在 `layout` 里加一个 y 偏移比例，待校准）。

### 4.2.2 心形数字需要滚动才能看到（用户实测）

实测对比（`10.png` 默认 vs `10 new.png` 手动下滚）：

| | 第一排心形 | 第二排心形 |
| --- | --- | --- |
| 默认视角 | ✅ 400/100/100/400/400 | ❌ 被底部横幅挡住 |
| 下滚一小段后 | ✅ 400/100/100/400/400 | ✅ 200/200/200/200/200 |

- **数量角标两排默认都可见**，所以「刷新库存」**不需要滚动**。
- **只有心形数字（经验值）需要滚动**，且下滚一小段就能同时看到两排。
- 底部「特殊礼物」横幅是**固定在底部**的，礼物往下滚会离开它，不用特别处理。

采集/回填时的「读经验值」流程（只在采集时做一次，不是每次跑任务都做）：

```
鼠标移到礼物网格上（滚轮需悬停）
  -> 向下滚一小段 -> 截图
  -> 逐格 OCR「图标 + 心形数字」-> 按图标模板 upsert 到 gift_catalog(gift_id -> exp)
  -> 滚回顶部（关键：保存帧必须保持默认视角, 否则 selected_slots 的格位全错）
```

三个细节：
1. **滚多少**：滚一次后检查心形数字是否读全，没读全就再滚一点（复用侧栏的 `scroll_and_is_end` 模式）。
2. **视角偏移不用管**：经验值是按**图标**存进目录的，只要在滚动帧里读到「该图标 = 400」即可，
   不需要把滚动后的行对应回默认格位。
3. **必须复位**：读完滚回顶部再保存帧。
4. **兜底**：某个格位的心形数字读不到时，UI 允许手动指定该礼物的档位（100/200/400）。

### 4.3 纯计算核心（新 `src/gifts/AffinityCalculator.py`）

```python
EXP_TABLE = {1:500, 2:1000, 3:2000, 4:3500, 5:5000, 6:7000, 7:9000, 8:12000, 9:16000}
MAX_LEVEL = 10
def bonus_threshold(skill_level) -> int: return min(8, skill_level + 3)
def gift_gain(base_exp, level, threshold) -> int: ...   # 带/不带 ×1.05

def simulate_character(...) -> CharacterPlan   # 需几天 / 各档消耗 / 各档需买 / 逐日明细
def simulate_all(profiles, settings, stock) -> GlobalPlan
```

每日流程（照抄自动赠礼）：
1. 先给每个勾了「每日额外经验」的角色加经验（**不吃加成**）。
2. 按角色顺序，每人送 `target_count` 次，累计到全局 10 次为止。
3. 每次送礼：从共享库存按「高经验优先」取（100/200/400），库存耗尽则按该角色
   `buy_tier` 购买；经验按「送礼前等级」判定 5% 加成。
4. 升级、溢出处理同旧代码。

产出「各档每日消耗 / 库存耗尽在第几天 / 全部达标需几天 / 需买总数」。

### 4.4 消耗记账：送礼前后差分

```
刷新库存:  截当前赠礼页 -> 逐格模板匹配 + OCR -> upsert stock / snapshot_at
跑完赠礼:  开始前 stock 快照 -> 跑完再读 -> 差 = ledger[今天].sent -> 刷新 stock
```

**注意点：**

1. **差分要按 `gift_id`**，不能按格位（见 4.2）。
2. **覆盖范围**：差分只覆盖本次实际送的角色。建议**整个 run 前后各读一次**（最省），
   或每个角色送完各读一次（更细）。
3. **「数量变化」≠「每日速率」**：某天只送了一部分角色、或隔几天没跑，速率就不准。
   → 天数以**计算器的计划速率**为准（已定），差分实测作对照。
4. **不要复用「更新当前角色」按钮**（它会 recapture 并可能改到 `blocked_slots`）。
   新增独立**「刷新库存」**入口：只读不写 profile。

### 4.5 多角色同时送同一种礼物（用户提出的问题）

这是必须设计对的点。分三块：

**(a) 库存是全局共享的**，同一种礼物只有一个库存。
A 每天送 2 个、B 每天送 3 个 → 该礼物日耗 5 个 → **可撑天数 = 库存 / 5**（不是按角色各算）。
所以计算器必须做「多角色共享库存」的联合模拟，看板上的分母也必须是所有角色日耗之和。

**(b) 全局 10 次会被争用**：4 个角色各想送 3 次 = 12 > 10，后面的角色送不满。
计划里按角色顺序分配，和实际自动赠礼一致；跑不满的角色其达标天数会被拉长，计算器要体现。

**(c) 优先级从「格位索引」升级成 `gift_id`（已定）**：
同一种礼物在不同角色页的格位不同；某礼物库存耗尽/列表刷新时格位会平移，
保存的索引就可能指向别的礼物。所以 profile 的优先级改存**礼物身份列表**
`priority_gift_ids`。迁移时用已保存帧把每个 `selected_slots` 格位换算成 `gift_id`
（图标模板匹配 + OCR 经验值），换算不出来的格位保留 `selected_slots` 兜底。
发送时仍用现有 `_find_gift_box()`（本来就按图标模板匹配），并按 `priority_gift_ids` 排序。

**(d) 差分粒度：粗略（已定）** — 只记「每角色总次数 + 每礼物总消耗」，
整个 run 前后各读一次页面，不逐角色读。

### 4.6 UI（扩展 `src/ui/GiftManagerTab.py`，不新建标签页）

- **库存预警看板**（左栏下方紧凑卡）：
  每件礼物「图标 / 经验 / 库存 / 每日消耗 / 可撑天数」，加一行「全部达标还需 N 天，需买 X 个」，
  以及「刷新库存」按钮。
- **好感升级计算器**（右栏）：
  - 设置卡新增「主角特技等级」下拉（1-5）。
  - 角色卡新增「当前等级 / 当前经验 / 目标等级 / 后续购买档位」+「从游戏读取」按钮，
    以及「每日额外经验：☑ + 数字框」。
  - 结果区（只读文本，仿旧版报告）：还需 N 个礼物、需 D 天、加成是否生效。
- 字符串走 `self.tr()`，同步 i18n。

---

## 5. 实施步骤（建议 7 步）

1. **数据模型 v2 + 迁移 + 单测**（`GiftDb` / `GiftManager`：新字段、目录、库存、账本；
   `selected_slots` → `priority_gift_ids` 迁移）。
2. **计算核心 + 单测**（`AffinityCalculator`：经验表、5% 阈值、单角色、多角色共享库存、
   每角色 3 + 全局 10、剩余天数）。
3. **礼物目录 + 赠礼页读取器 + 单测**（图标模板去重、OCR 经验/数量/等级；
   用 7 张截图做本地回归，缺图自动跳过；截图不入库）。
4. **`GiftTask` 接入 + 单测**（前后快照差分记账、刷新库存；可选自动读等级）。
5. **UI 库存预警看板**。
6. **UI 好感升级计算器**（每角色）。
7. **i18n + 文档 + 全量 `unittest discover`**。

（5、6 可合并；3、4 可合并。）

---

## 6. 待确认问题

全部已确认，无阻塞。可开工。

- 普通档位：100 / 200 / 400，**没有更高档**。
- 商店购买：**只显示「还需买 N 个」**，不需要价格/兑换方式。
- 其余：经验表、特殊礼物不算、无无限礼物、全局 10 次照抄自动赠礼、
  优先级升级成 `gift_id`、粗略粒度记账、天数以计划速率为准、扩展「羁遇赠礼」标签页、
  经验值在采集时 OCR 心形数字并存进礼物目录（老角色用保存帧回填）。

---

## 7. 实施记录

### Step 1（数据模型 v2 + 迁移 + 单测）— 已完成

- `src/gifts/GiftDb.py`：schema 升到 **v2**。
  - 新增顶层段：`settings`（`protagonist_skill_level` 1-5 / `daily_gift_limit_per_char` /
    `daily_gift_limit_global`）、`gift_catalog`、`stock`、`stock_snapshot_at`、`ledger`。
  - profile 新增字段：`priority_gift_ids`、`bond_level`、`bond_exp`、`target_level`、
    `buy_tier`（限定 100/200/400）、`daily_extra_exp`、`daily_extra_enabled`。
  - 新增 `normalize_settings` / `normalize_gift_ids` / `normalize_catalog` /
    `normalize_stock` / `normalize_ledger`；`validate_db` 顺带完成 **v1 -> v2 迁移**
    （补默认值 + 升版本号，旧 profile 字段原样保留）。
- `src/gifts/GiftManager.py`：新增 `get_settings` / `update_settings` /
  `get_catalog` / `upsert_gift` / `get_stock` / `set_stock` / `get_stock_snapshot_at` /
  `get_ledger` / `record_sent`（按天合并 + 保留最近 `LEDGER_MAX_DAYS=365` 天）；
  `update_profile` 增加新字段参数；`create_profile` / `recapture_profile` 统一走
  `normalize_profile`，保证新字段一定有默认值。
- 单测：新增 `tests/TestGiftDb.py`（11 个：默认库、各段归一化、v1->v2 迁移、
  读写往返、坏 JSON 恢复）；`tests/TestGiftManager.py` 扩到 17 个（settings / 目录 /
  库存 / 账本 / 新 profile 字段 / 迁移）。
- 验证：`py_compile` + `ruff` 通过；`unittest tests.TestGiftDb tests.TestGiftManager`
  **28 OK**；全量 `discover` **429 OK**（本分支 = 作者最新 + 文档，不含战斗测试）。
- 备注：`selected_slots` -> `priority_gift_ids` 的换算需要图标模板 + OCR，
  放到 step 3 用已保存帧回填；step 1 只保留 `selected_slots`、`priority_gift_ids` 默认空。

### Step 2（计算核心 `AffinityCalculator` + 单测）— 已完成

- 新文件 `src/gifts/AffinityCalculator.py`：纯计算层，不依赖 UI / OCR / 截图 / `GiftTask` /
  `GiftManager` I/O / 游戏进程，只用普通数据结构。
- 公开 API：
  - `required_exp_for_level(level)`：n -> n+1 的经验（满级 0）
  - `required_exp_to_target(current_level, current_exp, target_level)`
  - `bonus_threshold(skill_level)`：特技 n -> 阈值 n+3（特技 5 -> 8 级）
  - `gift_exp(base_exp, level, skill_level)`：单件礼物实际经验（含 5%）
  - `calculate_character_plan(...)` -> `CharacterPlan`
  - `calculate_multi_character_plan(...)` -> `MultiCharacterPlan`
  - `calculate_days_to_target(...)` -> `DaysPlan`
  - `calculate_purchase_need(required_exp, buy_tier, available_exp=0)` -> `PurchaseNeed`
- 固定语义：
  - `bond_exp` = **当前等级内已获得经验**（不是累计）；需求 = 经验表求和 - `bond_exp`。
  - `bond_level` 1-10，`0` = 未设置（多角色规划会跳过该角色）；纯函数对 0/11 等非法值抛 `ValueError`。
  - `target_level` > 10 夹到 10；<= 当前等级时需求为 0（不产生负需求）。
  - `daily_extra_exp`（约会）先结算、**不吃 5%**、不消耗库存、不占赠送次数；
    输出里的 `daily_extra_exp` 是**实际生效**的量（已达标时为 0）。
- 5% 加成：整数运算 `base * 105 // 100`（向零截断）。当前档位 100/200/400 ×1.05 都是整数，
  截断与四舍五入结果相同；**游戏实际取整方式尚未确认**，有实测再调。
  加成按「送礼那一刻的等级」逐件判定，升级跨过阈值后自动失效，且不叠加。
- 多角色：顺序 = 传入 `profiles` 的顺序（与 `GiftTask` 一致，不按缺口重排）；
  共享库存按顺序扣减；每角色 `daily_gift_limit_per_char` 次、全局 `daily_gift_limit_global` 次，
  都取自 `settings`（不硬编码 3/10）。
- 购买：`calculate_purchase_need` = `ceil((需求 - 库存经验) / 档位)`，库存与购买量分开返回；
  只按基础经验估算（不含 5%），属保守上界。
- 单测：新增 `tests/TestAffinityCalculator.py`（43 个，覆盖经验表边界、5% 阈值/跨级/取整、
  约会经验、优先级顺序、库存不足/刚好/多余、共享库存不重复计、每日/全局次数、购买量）。
- 验证：`py_compile` + `ruff` 通过；`unittest tests.TestAffinityCalculator` **43 OK**；
  全量 `discover` **472 OK**（429 + 43）。
- Step 1 schema 观察（**未改动**）：`bond_level` 默认 0 表示「未设置」，与审查建议的 1-10 有出入；
  计算器按「0 = 跳过」处理，UI/规划层需把 0 视为未配置。**无阻塞**。

### Step 3（礼物目录 + 赠礼页读取器 + 单测）— 已完成

方案按用户建议**从"图标自动认身份"改成"用户命名做身份"**（原因见 §4.2 的实测结论）。

- 新增 `src/gifts/GiftIdentity.py`：
  - `normalize_gift_name(name)` / `gift_id_for_name(name)`：用户命名 -> `gift_id`，
    同名同 id（只做 NFKC + 去空白 + 折叠空白，不模糊匹配）；空名字返回 `None`。
  - `match_score(icon, template)` / `suggest_gift_id(icon, templates, threshold)`：
    图标模板匹配，只用于**建议继承**已有标注（阈值 0.8）。
  - `GiftIconStore`：`gift_configs/gift_icons/<gift_id>.png` 的读写。
- 新增 `src/gifts/GiftPageReader.py`：
  - 纯函数：`parse_bond_progress` / `parse_int` / `parse_gift_counter` / `parse_global_remaining`；
    `gift_slot_boxes` / `gift_exp_box` / `gift_badge_box` / `special_badge_box` / `crop_region`；
    `label_slots` / `migrate_priority_gift_ids` / `merge_snapshot_into_catalog` / `gift_ids_for_names`。
  - 数据结构：`BondReading` / `DailyCountReading` / `GiftSlotReading` / `GiftPageSnapshot`。
  - `GiftPageReader`（需要 task 提供 OCR / `find_one`）：`read_bond` / `read_counts` /
    `read_slots` / `suggest_labels` / `learn_labels` / `read`。
  - **读取失败一律 `None`**，不会用 0 冒充"库存没了/没有经验"。
- `src/gifts/layout.py`：新增 `bond_level_box` / `bond_progress_box` / `gift_exp_*` /
  `gift_badge_*` / `daily_banner_box` / `gift_counter_button_box`（用 7 张截图标定过）。
- `src/gifts/GiftDb.py` / `GiftManager.py`：profile 新增 `slot_gift_ids`
  （`{slot: gift_id}`，用户标注；`update_profile` 支持写入）。**这是 Step 1 schema 的一处
  增量扩展**（纯新增字段，v1/v2 旧库读入自动补默认值，向后兼容）。
- 单测：`tests/TestGiftIdentity.py`（19）、`tests/TestGiftPageReader.py`（35）、
  `tests/TestGiftScreenshots.py`（7，真实截图回归，缺图自动跳过）。
- 身份不变量测试（冻结规则）：同名一定同 id（含 NFKC/去首尾空白）、内部空白折叠但仍有意义、
  相近名字必须不同 id、高相似度**不**自动赋值 `gift_id`、用户确认后才继承、
  同 gift_id 跨 slot/跨角色共用身份、**图标完全相同但命名不同也绝不合并**（目录里两条独立记录）。
- 真实截图回归：`tests/fixtures/gift/`（gitignore，不入库）放 7 张赠礼页截图；
  以 `8.png` 上标注的「票券/棉花糖/贝壳」为模板，验证在**其它角色页、其它 slot** 上
  仍能建议成同一礼物（0.957~1.000，共 21 条有效建议），且不同礼物不会互相建议。
  **不放 `screenshots/`**：那是工具运行目录，应用每次启动都会清空（见 `src/config.py`）。
- 验证：`py_compile` + `ruff` 通过；礼物相关单测 **61 OK**；全量 `discover` **533 OK**（7 skipped）。
- 已知限制（**不影响正确性**，因为身份由用户命名决定）：白色系（白条/白旗）与很暗的图标
  在图标中段细带里区分度不足，自动建议可能认错；用户改名即可。
- 注意：`tests/fixtures/gift/` 属于用户数据，**不入库**；缺图时相关测试跳过（不会伪造通过）。
