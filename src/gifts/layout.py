from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GiftLayout:
    name_ratio: tuple[float, float, float, float] = (0.524, 0.166, 0.750, 0.240)
    gift_first_ratio: tuple[float, float, float, float] = (0.533, 0.497, 0.584, 0.534)
    gift_columns: int = 5
    gift_rows: int = 2
    gift_column_step: float = 0.0651
    gift_row_step: float = 0.1351
    unlimited_icon_x_offset_ratio: float = 0.10
    unlimited_icon_y_offset_ratio: float = 0.74
    unlimited_icon_width_reduction_ratio: float = 0.45
    character_slot_x: float = 0.946
    character_slot_ys: tuple[float, ...] = (0.177, 0.326, 0.472, 0.624, 0.772)
    sidebar_box: tuple[float, float, float, float] = (0.936, 0.146, 0.971, 0.205)
    sidebar_scroll_x: float = 0.947
    sidebar_scroll_y: float = 0.500
    sidebar_scroll_step: int = -5
    sidebar_reset_step: int = 40
    sidebar_scrolls_per_character: int = 5
    max_sidebar_pages: int = 30
    send_button: tuple[float, float] = (0.713, 0.806)
    counter_box: tuple[float, float, float, float] = (0.646, 0.780, 0.790, 0.840)
    # 羁遇等级大数字(圆环中心)
    bond_level_box: tuple[float, float, float, float] = (0.785, 0.195, 0.835, 0.248)
    # "当前经验/升级需求", 例如 1550/12000
    bond_progress_box: tuple[float, float, float, float] = (0.755, 0.258, 0.865, 0.295)
    # 礼物心形经验数字: 位于礼物卡下方, 默认视角只看得见第一排, 需要下滚
    gift_exp_x_expand: float = 0.008
    gift_exp_y_offset: float = 0.066
    gift_exp_height: float = 0.033
    # 礼物库存角标: 位于图标右下角, 两排默认都可见
    gift_badge_x_offset: float = 0.030
    gift_badge_y_offset: float = 0.042
    gift_badge_width: float = 0.026
    gift_badge_height: float = 0.030
    # 底部横幅: "今日还能赠送N次礼物" 或 "该礼物为特殊礼物..."
    daily_banner_box: tuple[float, float, float, float] = (0.530, 0.680, 0.855, 0.725)
    # 按钮 "赠送R/L": R=该角色今日剩余次数, L=每角色上限
    gift_counter_button_box: tuple[float, float, float, float] = (0.625, 0.790, 0.795, 0.825)


GIFT_LAYOUT = GiftLayout()
