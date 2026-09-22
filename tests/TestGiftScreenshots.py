"""真实赠礼页截图的本地回归.

截图属于用户数据, **不入库**(``tests/fixtures/gift/`` 已被 .gitignore); 需要时把赠礼页
截图放到该目录下, 缺失则跳过(不会伪造通过)。

**不要**放 ``screenshots/``: 那是工具的运行目录, 应用每次启动都会清空(见
``src/config.py`` 的 ``screenshots_folder`` 注释)。

验证的是 Step 3 的身份方案: 用户在某个角色页给礼物命名后, **换一个角色页**再采集时,
图标模板匹配能把这些格子**建议**成同一个礼物(用户确认后即继承名称/经验)。
"""

import unittest
from pathlib import Path

import cv2

from src.gifts.GiftIdentity import (
    SEARCH_EXPAND_X,
    SEARCH_EXPAND_Y,
    gift_id_for_name,
    suggest_gift_id,
)
from src.gifts.GiftPageReader import crop_region, expand_box, gift_slot_boxes

FIXTURES = ("1.png", "2.png", "3.png", "4.png", "8.png", "9.png", "10.png")
SCROLLED_FIXTURE = "10_scrolled.png"
REFERENCE = "8.png"
# 用户在 8.png 上给礼物起的名字(slot -> 名字)
REFERENCE_LABELS = {0: "票券", 4: "棉花糖", 5: "贝壳"}


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "gift"


def _load_fixtures() -> dict:
    directory = _fixture_dir()
    images = {}
    for name in FIXTURES:
        path = directory / name
        if path.exists():
            image = cv2.imread(str(path))
            if image is not None:
                images[name] = image
    return images


IMAGES = _load_fixtures()


def _slot_icon(image, index):
    height, width = image.shape[:2]
    return crop_region(image, gift_slot_boxes(width, height)[index])


def _search_icon(image, index):
    height, width = image.shape[:2]
    box = gift_slot_boxes(width, height)[index]
    return crop_region(image, expand_box(box, SEARCH_EXPAND_X, SEARCH_EXPAND_Y))


def _templates():
    reference = IMAGES[REFERENCE]
    return {
        gift_id_for_name(name): _slot_icon(reference, slot)
        for slot, name in REFERENCE_LABELS.items()
    }


def _suggest_all():
    templates = _templates()
    results = {}
    for name, image in IMAGES.items():
        for index in range(10):
            gift_id, score = suggest_gift_id(_search_icon(image, index), templates)
            results[(name, index)] = (gift_id, score)
    return results


class TestGiftScreenshotFixtures(unittest.TestCase):
    def test_report_fixture_availability(self):
        found = len(IMAGES)
        missing = [name for name in FIXTURES if name not in IMAGES]
        print(f"[gift fixtures] 找到 {found}/{len(FIXTURES)} 张, 缺失 {missing}")
        if not IMAGES:
            self.skipTest("screenshots/gift 下没有赠礼页截图, 跳过回归(需手动放入)")


@unittest.skipUnless(len(IMAGES) >= 2, "需要至少 2 张赠礼页截图才能验证跨角色建议")
class TestGiftScreenshotSuggestions(unittest.TestCase):
    """8.png 上标注了票券/棉花糖/贝壳, 看其它角色页能不能建议成同一个礼物.

    只放目视可确认的礼物; 白色系(白条/白旗)与很暗的图标在图标中段细带里区分度不足,
    属于已知限制(见文档), 不在这里断言 —— 反正最终身份由用户命名决定。
    """

    GROUPS = {
        "票券": [
            ("8.png", 0), ("2.png", 0), ("3.png", 0), ("1.png", 0),
            ("10.png", 0), ("4.png", 1), ("9.png", 1),
        ],
        "棉花糖": [
            ("8.png", 4), ("2.png", 4), ("3.png", 4), ("4.png", 4),
            ("9.png", 4), ("10.png", 4), ("1.png", 5),
        ],
        "贝壳": [
            ("8.png", 5), ("2.png", 5), ("3.png", 3), ("4.png", 5),
            ("9.png", 5), ("10.png", 3), ("1.png", 4),
        ],
    }

    @classmethod
    def setUpClass(cls):
        cls.results = _suggest_all()
        for (name, index), (gift_id, score) in sorted(cls.results.items()):
            print(f"[gift suggest] {name}:{index} -> {gift_id} ({score:.3f})")

    def test_same_gift_suggests_same_label_across_characters_and_slots(self):
        for label, members in self.GROUPS.items():
            expected = gift_id_for_name(label)
            for member in members:
                self.assertEqual(
                    self.results[member][0],
                    expected,
                    f"{member} 没有建议成 {label}",
                )

    def test_matches_are_high_confidence(self):
        for label, members in self.GROUPS.items():
            for member in members:
                self.assertGreaterEqual(self.results[member][1], 0.8, f"{label} {member} 分数偏低")

    def test_different_labels_are_not_confused(self):
        labels = list(self.GROUPS)
        for i, first in enumerate(labels):
            for second in labels[i + 1 :]:
                self.assertNotEqual(
                    self.results[self.GROUPS[first][0]][0],
                    self.results[self.GROUPS[second][0]][0],
                    f"{first} 与 {second} 被建议成同一个礼物",
                )

    def test_slot_does_not_affect_suggestion(self):
        # 票券在 8.png 是 slot 0, 在 4.png 是 slot 1
        self.assertEqual(self.results[("8.png", 0)][0], self.results[("4.png", 1)][0])

    def test_suggestions_are_deterministic(self):
        self.assertEqual(_suggest_all(), self.results)


@unittest.skipUnless((_fixture_dir() / SCROLLED_FIXTURE).exists(), "缺少下滚后的截图")
class TestScrolledFixture(unittest.TestCase):
    def test_scrolled_fixture_loads(self):
        image = cv2.imread(str(_fixture_dir() / SCROLLED_FIXTURE))
        self.assertIsNotNone(image)
        self.assertEqual(image.shape[:2], (1080, 1920))


if __name__ == "__main__":
    unittest.main()
