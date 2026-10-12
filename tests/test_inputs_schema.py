"""Reading the input sheets, and writing and reading the output table."""

import tempfile
import unittest
from pathlib import Path

from helpers import PRIUS, ROOT, sep2_rows

from carsavings.inputs import parse_thing_id, read_seeds, split_brand_model
from carsavings.schema import COLUMNS, CORE, blank_row, read_rows, write_rows


class Inputs(unittest.TestCase):
    def test_prius_sheet(self):
        seeds = read_seeds(PRIUS)
        self.assertEqual(len(seeds), 60)
        self.assertEqual({(s["br"], s["mdl"]) for s in seeds}, {("Toyota", "Prius")})
        self.assertEqual((seeds[0]["id"], seeds[0]["dl"]), ("2334961", 435))

    def test_examples(self):
        smoke = read_seeds(ROOT / "examples" / "smoke.csv")
        self.assertEqual([s["id"] for s in smoke], ["3013735", "5998784", "7186295", "5019865", "5770631"])
        self.assertEqual((smoke[0]["dl"], smoke[0]["br"], smoke[0]["mdl"]), (309, "Ford", "Mustang"))
        self.assertEqual(len(read_seeds(ROOT / "examples" / "sample_things.csv")), 17)

    def test_text_list_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ids.txt"
            p.write_text("# smoke test\n3013735\nhttps://www.thingiverse.com/thing:5998784/files\n"
                         "thing:7186295\n3013735\n\n", encoding="utf-8")
            seeds = read_seeds(p, ids=["https://www.thingiverse.com/thing:5019865", "7186295"])
        self.assertEqual([s["id"] for s in seeds], ["3013735", "5998784", "7186295", "5019865"])
        self.assertIsNone(seeds[0]["dl"])
        self.assertEqual(seeds[1]["url"], "https://www.thingiverse.com/thing:5998784")

    def test_generic_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mine.csv"
            p.write_text("url,Brand & Model,downloads\nhttps://www.thingiverse.com/thing:123456,Land Rover "
                         "Defender,\"1,204\"\nnot a thing,Ford Focus,3\n", encoding="utf-8")
            seeds = read_seeds(p)
        self.assertEqual(len(seeds), 1)
        self.assertEqual((seeds[0]["id"], seeds[0]["br"], seeds[0]["mdl"], seeds[0]["dl"]),
                         ("123456", "Land Rover", "Defender", 1204))

    def test_split_brand_model(self):
        self.assertEqual(split_brand_model("Land Rover Defender 110"), ("Land Rover", "Defender 110"))
        self.assertEqual(split_brand_model("Mercedes-Benz C-Class"), ("Mercedes-Benz", "C-Class"))
        self.assertEqual(split_brand_model("Mini Cooper"), ("MINI", "Cooper"))
        self.assertEqual(split_brand_model("Zastava Yugo"), ("Zastava", "Yugo"))

    def test_parse_thing_id(self):
        self.assertEqual(parse_thing_id("https://www.thingiverse.com/thing:3013735/comments"), "3013735")
        self.assertEqual(parse_thing_id(" 5998784 "), "5998784")
        with self.assertRaises(ValueError):
            parse_thing_id("no id here")


class Schema(unittest.TestCase):
    def test_columns(self):
        self.assertEqual(len(CORE), 30)
        self.assertEqual(COLUMNS[30:], ("gen", "yrs", "at", "now", "cands",
                                        "g", "hrs", "mm", "infill", "files", "dl_src"))

    def test_round_trip_gz_and_extra_columns(self):
        rows = [r for r in sep2_rows() if r["id"] in ("3013735", "5019865", "2552176", "1803934", "128092")]
        mine = blank_row("42")
        mine.update(t="Clip, \"quoted\"\nand multi-line", g=12.5, hrs=0.75, mm=88.1, files=["a.stl"],
                    human_review="vetoed")
        rows.append(mine)
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("t.csv", "t.csv.gz"):
                path = Path(tmp) / name
                write_rows(path, rows, extra_columns=("human_review",))
                back = read_rows(path)
                self.assertEqual(len(back), len(rows))
                for a, b in zip(rows, back, strict=True):
                    for k in COLUMNS:
                        self.assertEqual(a.get(k, ""), b[k], (name, a["id"], k))
                self.assertEqual(back[-1]["human_review"], "vetoed")
                self.assertEqual(back[0]["human_review"], "")


if __name__ == "__main__":
    unittest.main()
