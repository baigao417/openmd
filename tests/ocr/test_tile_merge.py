from tests.ocr.seam_chain import merge_texts
"""Unit tests for tile_long_image merge/split. Run: python -B -m unittest test_tile_merge -v"""
import sys
import unittest
from pathlib import Path

from openmd.ocr import tiling as T


class MergeTests(unittest.TestCase):
    def test_zh_overlap_with_garbled_edges(self):
        a = ("第一段：本季度新增并网项目 23 个。\n\n"
             "第二段：会议讨论了三季度预算，确定了下一步计划。\n\n"
             "第三段开头被切掉的那一行乱码壹贰")          # last line cut at tile bottom
        b = ("划。\n\n第三段开头被切掉的那一行现在完整了。\n\n"   # garbled head, then overlap
             "第四段：明年一季度启动扩容工程。")
        merged, rep = merge_texts([a, b])
        self.assertEqual(rep["seams"][0]["status"], "merge_uncertain")
        self.assertIn("第三段开头被切掉的那一行现在完整了。", merged)
        self.assertIn(a, merged)
        self.assertIn(b, merged)
        self.assertTrue(rep["warnings"])
        self.assertEqual(merged.count("第三段开头"), 2)
        self.assertEqual(merged.count("确定了下一步计划"), 1)
        self.assertIn("[openmd variant A]\n\n第一段", merged)
        self.assertIn("第四段：明年一季度启动扩容工程。", merged)

    def test_en_overlap_with_ocr_variance_and_heading_markup(self):
        a = ("Intro paragraph about the market stall.\n\n"
             "The biggest surprise was how much the weather mattered. On the eight rainy nights, "
             "sales fell by an average of 46%. I eventually bought a sec")
        b = ("weather rnattered. On the eight rainy nights, sales fell by an average of 46%. "
             "I eventually bought a second canopy.\n\n## Pricing\n\nPricing was the hardest decision.")
        merged, rep = merge_texts([a, b])
        self.assertEqual(rep["seams"][0]["status"], "merge_uncertain")
        self.assertIn("I eventually bought a second canopy.", merged)
        self.assertEqual(merged.count("average of 46%"), 2)
        self.assertIn("## Pricing", merged)
        self.assertTrue(merged.startswith("Intro paragraph about the market stall."))
        self.assertTrue(merged.endswith("Pricing was the hardest decision."))
        self.assertIn(a.split("\n\n", 1)[1], merged)
        self.assertIn(b.split("\n\n", 1)[0], merged)
        self.assertIn("weather mattered", merged)
        self.assertIn("weather rnattered", merged)
        self.assertTrue(rep["warnings"])

    def test_no_overlap_warns(self):
        merged, rep = merge_texts(["完全不同的第一片内容。", "Totally unrelated second tile."])
        self.assertEqual(rep["seams"][0]["status"], "merge_uncertain")
        self.assertTrue(rep["warnings"])
        self.assertIn("完全不同的第一片内容。", merged)
        self.assertIn("Totally unrelated second tile.", merged)

    def test_three_tile_chain(self):
        full = "".join(f"第{i}行：编号 A{i:03d} 的数据为 {i * 37 % 1000} 点。\n" for i in range(1, 61))
        lines = full.splitlines(keepends=True)
        tiles = ["".join(lines[0:25]), "".join(lines[20:45]), "".join(lines[40:60])]
        merged, rep = merge_texts(tiles)
        self.assertEqual([s["status"] for s in rep["seams"]], ["merged", "merged"])
        self.assertEqual(merged, full)
        self.assertEqual(rep["repeated_spans"], [])


class SplitTests(unittest.TestCase):
    def test_cuts_land_on_blank_rows(self):
        import tempfile
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (400, 6000), "white")
        d = ImageDraw.Draw(img)
        for y in range(40, 5960, 70):          # 40px "text lines", 30px gaps
            d.rectangle([30, y, 370, y + 40], fill="black")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "long.png"
            img.save(p)
            meta = T.split_image(p, Path(tmp) / "tiles", tile_height=1440, overlap=0.18)
        tiles = meta["tiles"]
        self.assertGreaterEqual(len(tiles), 4)
        self.assertEqual(tiles[0]["y0"], 0)
        self.assertEqual(tiles[-1]["y1"], 6000)
        for t in tiles:
            self.assertLessEqual(t["height"], int(1440 * 1.1))
            if t["end_cut"] != "bottom":
                self.assertEqual(t["end_cut"], "blank")
                self.assertEqual(t["ink_at_end"], 0.0)
        for prev, cur in zip(tiles, tiles[1:]):
            self.assertGreater(cur["overlap_prev"], 0.1 * 1440)
            self.assertLess(cur["overlap_prev"], 0.3 * 1440)
            self.assertEqual(cur["start_cut"], "blank")


if __name__ == "__main__":
    unittest.main()
