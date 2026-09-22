"""礼物身份: 由**用户命名**决定, 图标模板只用来在重新采集时"继承"已有标注.

赠礼页只有图标 + 数量角标 + 心形经验, 没有礼物名称, 靠图标自动认身份并不可靠
(白色系/很暗的图标会混)。所以身份的**唯一依据是用户给礼物起的名字**:

- 同一件礼物 = 用户命名相同(归一化后完全一致); 不做模糊匹配, 避免把两种礼物并成一个。
- 图标模板匹配只在"更新当前角色"时用来**建议**标注: 新截图里某个格子的图标和用户
  之前标注过的图标很像 -> 建议继承之前的名称/经验, 最终仍由用户确认。

``gift_id`` 由归一化后的名字派生(哈希), 名字相同 -> id 相同; 名字只用于展示。
"""

from __future__ import annotations

import hashlib
import os
import unicodedata

import cv2
import numpy as np

GIFT_ICON_DIR = os.path.join("gift_configs", "gift_icons")
# 图标模板匹配的阈值; 实测同一礼物跨 slot/跨角色 0.96~1.00, 不同礼物 <= 0.69
MATCH_THRESHOLD = 0.8
SEARCH_EXPAND_X = 0.014
SEARCH_EXPAND_Y = 0.030


def normalize_gift_name(name) -> str:
    """用户命名归一化: Unicode NFKC + 去首尾空白 + 折叠内部空白.

    只做这些不会改变语义的处理; **不做**大小写/模糊匹配, 避免合并两个不同礼物。
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    return " ".join(text.split())


def gift_id_for_name(name) -> str | None:
    """名字 -> ``gift_id``; 空名字返回 ``None``(表示未命名/不参与)。"""
    normalized = normalize_gift_name(name)
    if not normalized:
        return None
    return "gift_" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def match_score(icon: np.ndarray | None, template: np.ndarray | None) -> float:
    """图标与模板的归一化互相关最高分; 无法比较时返回 -1."""
    if icon is None or template is None:
        return -1.0
    if getattr(icon, "size", 0) == 0 or getattr(template, "size", 0) == 0:
        return -1.0
    if icon.shape[0] < template.shape[0] or icon.shape[1] < template.shape[1]:
        return -1.0
    result = cv2.matchTemplate(icon, template, cv2.TM_CCOEFF_NORMED)
    if result.size == 0:
        return -1.0
    return float(result.max())


def suggest_gift_id(
    icon: np.ndarray | None, templates: dict[str, np.ndarray], threshold: float = MATCH_THRESHOLD
) -> tuple[str | None, float]:
    """在已知图标里找 ``icon`` 最像的那个; 返回 ``(建议的 gift_id | None, 最高分)``.

    只用于"建议继承已有标注"; 低于阈值返回 ``None``, 不强行猜。
    """
    best_id: str | None = None
    best_score = -1.0
    for gift_id, template in templates.items():
        score = match_score(icon, template)
        if score > best_score:
            best_id, best_score = gift_id, score
    if best_id is None or best_score < threshold:
        return None, best_score
    return best_id, best_score


class GiftIconStore:
    """用户标注过的礼物图标落盘(每件礼物一个 PNG), 用于下次采集时建议继承."""

    def __init__(self, directory: str = GIFT_ICON_DIR):
        self.directory = directory

    def path_for(self, gift_id: str) -> str:
        return os.path.join(self.directory, f"{gift_id}.png")

    def load_all(self) -> dict[str, np.ndarray]:
        templates: dict[str, np.ndarray] = {}
        if not os.path.isdir(self.directory):
            return templates
        for name in sorted(os.listdir(self.directory)):
            if not name.endswith(".png"):
                continue
            image = cv2.imread(os.path.join(self.directory, name))
            if image is not None:
                templates[name[: -len(".png")]] = image
        return templates

    def save(self, gift_id: str, icon: np.ndarray) -> None:
        if icon is None or getattr(icon, "size", 0) == 0:
            raise ValueError("Gift icon is empty")
        os.makedirs(self.directory, exist_ok=True)
        if not cv2.imwrite(self.path_for(gift_id), icon):
            raise IOError(f"Failed to write gift icon: {gift_id}")

    def delete(self, gift_id: str) -> None:
        path = self.path_for(gift_id)
        if os.path.exists(path):
            os.unlink(path)
