"""Paper step 4 helpers: the slicer's status and replies, the estimator, the unit."""

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import has

from carsavings import slicing


class Helpers(unittest.TestCase):
    def test_job_idle(self):
        self.assertTrue(slicing.job_idle({}))
        self.assertTrue(slicing.job_idle({"error": "no job"}))
        self.assertTrue(slicing.job_idle({"state": "Idle"}))
        self.assertTrue(slicing.job_idle({"busy": False}))
        self.assertFalse(slicing.job_idle({"running": True}))
        self.assertFalse(slicing.job_idle({"state": "slicing"}))
        # what the OrcaSlicer fork's /api/v1/jobs/status actually returns
        self.assertTrue(slicing.job_idle({"idle": True}))
        self.assertFalse(slicing.job_idle({"idle": False}))

    def test_tool_json(self):
        block = lambda text: SimpleNamespace(text=text)   # noqa: E731
        self.assertEqual(slicing.tool_json(SimpleNamespace(structuredContent={"result": {"grams": 3.2}})),
                         {"grams": 3.2})
        self.assertEqual(slicing.tool_json(SimpleNamespace(structuredContent={"result": [1, 2]})),
                         {"result": [1, 2]})
        self.assertEqual(slicing.tool_json(SimpleNamespace(structuredContent=None,
                                                           content=[block('{"weight_g": 7.5}')])),
                         {"weight_g": 7.5})
        self.assertEqual(slicing.tool_json(SimpleNamespace(content=[block("plate cleared")])),
                         {"text": "plate cleared"})
        err = slicing.tool_json(SimpleNamespace(content=[block("no model loaded")], isError=True))
        self.assertEqual(err["error"], "no model loaded")

    @unittest.skipUnless(has("mcp"), "needs mcp")
    def test_tool_json_reads_sdk_results(self):
        # MCP SDK 2.x names the fields structured_content and is_error; 1.x used camelCase.
        from mcp.types import CallToolResult

        sliced = CallToolResult.model_validate({
            "content": [{"type": "text", "text": "{}"}],
            "structuredContent": {"result": {"state": "done", "stats": {"filament_used_g": 17.2}}}})
        self.assertEqual(slicing.tool_json(sliced), {"state": "done", "stats": {"filament_used_g": 17.2}})
        failed = CallToolResult.model_validate({
            "content": [{"type": "text", "text": "Error executing tool load_model: file not found"}],
            "isError": True})
        self.assertEqual(slicing.tool_json(failed)["error"], "Error executing tool load_model: file not found")
        reply = CallToolResult.model_validate({
            "content": [{"type": "text", "text": '{"error": "OrcaSlicer not reachable"}'}]})
        self.assertEqual(slicing.tool_json(reply), {"error": "OrcaSlicer not reachable"})

    def test_estimate_grams(self):
        # 1 cm3 with 6 cm2 of surface: 0.72 cm3 of shell, the rest at 15 % infill, PLA at 1.24 g/cm3
        self.assertTrue(math.isclose(slicing.estimate_grams(1000, 600), 0.94488, rel_tol=1e-9))
        self.assertTrue(math.isclose(slicing.estimate_grams(1000, 600, "20%"), 1.24 * (0.72 + 0.28 * 0.2)))
        self.assertTrue(math.isclose(slicing.estimate_grams(1000, 600, watertight=False), 1.24 * 0.72))

    def test_infill_fraction(self):
        self.assertEqual(slicing.infill_fraction("20%"), 0.2)
        self.assertEqual(slicing.infill_fraction("abc"), slicing.DEFAULT_INFILL)
        self.assertEqual(slicing.infill_fraction("150"), slicing.DEFAULT_INFILL)

    def test_pick_entries(self):
        h = {"files": [{"name": "cap.stl"}, {"name": "cap_v2.stl"}, {"name": "photo.jpg"}, {"name": "kit.3mf"}]}
        self.assertEqual([f["name"] for f in slicing.pick_entries(h, ["cap_v2.stl"])], ["cap_v2.stl"])
        self.assertEqual([f["name"] for f in slicing.pick_entries(h, ["gone.stl"])],
                         ["cap.stl", "cap_v2.stl", "kit.3mf"])
        # a 3MF or OBJ with an STL of the same name is sliced from the STL
        h = {"files": [{"name": "EV6_Knob.3mf"}, {"name": "EV6_Knob.stl"}, {"name": "Clip.OBJ"}]}
        self.assertEqual([f["name"] for f in slicing.pick_entries(h, ["EV6_Knob.3mf"])], ["EV6_Knob.stl"])
        self.assertEqual([f["name"] for f in slicing.pick_entries(h, [])], ["EV6_Knob.stl", "Clip.OBJ"])

    def test_no_slicer_backend(self):
        self.assertIsNone(slicing.open_slicer(SimpleNamespace(slicer_backend="none")))
        with self.assertRaises(slicing.SlicerUnavailable):
            slicing.open_slicer(SimpleNamespace(slicer_backend="mcp", orca_api_token=""))


class ScriptedSlicer(slicing.Slicer):
    """Replays OrcaSlicer's replies in the shapes its Remote API sends; the last reply repeats."""

    def __init__(self, replies):
        super().__init__(SimpleNamespace(slice_timeout=60))
        self.replies = {name: list(queue) for name, queue in replies.items()}
        self.calls = []

    def call(self, name, args=None, wait=120.0):
        self.calls.append((name, args))
        queue = self.replies[name]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class SliceFlow(unittest.TestCase):
    def test_busy_plate_then_slice_then_restore(self):
        s = ScriptedSlicer({
            "get_status": [{"app": {"version": "2.4.2"}}],
            "get_config": [{"config": {"sparse_infill_density": "15%"}}],
            "list_objects": [{"objects": []}],
            "load_model": [{"loaded": True, "objects": [{"index": 0, "name": "clip.stl"}]}],
            "get_job_status": [{"idle": False}, {"idle": True}],
            "auto_orient": [{"error": "job_running"}, {"started": True}],
            "arrange_plate": [{"started": True}],
            "set_config": [{"applied": ["sparse_infill_density"], "errors": {}}],
            "slice_and_wait": [{"state": "done", "percent": 100, "message": "",
                                "stats": {"filament_used_g": 12.5, "estimated_time_seconds": 3600}}],
        })
        with mock.patch.object(slicing.time, "sleep", lambda _seconds: None):
            s.start()
            self.assertEqual(s.slice_file("clip.stl", "20%"), (12.5, 3600.0))
            s.close()
        names = [name for name, _ in s.calls]
        # the plate was busy after the load: orient waited for it, then retried once on job_running
        self.assertEqual(names[:names.index("auto_orient")].count("get_job_status"), 2)
        self.assertEqual(names.count("auto_orient"), 2)
        self.assertIn(("set_config", {"changes": {"sparse_infill_density": "20%"}}), s.calls)
        # closing puts the user's own infill back
        self.assertEqual(s.calls[-2], ("set_config", {"changes": {"sparse_infill_density": "15%"}}))

    def test_unfinished_slice_is_a_failure(self):
        s = ScriptedSlicer({
            "list_objects": [{"objects": [{"id": 3, "name": "old.stl"}]}],
            "delete_object": [{"deleted": True}],
            "load_model": [{"loaded": True, "objects": []}],
            "get_job_status": [{"idle": True}],
            "auto_orient": [{"started": True}],
            "arrange_plate": [{"started": True}],
            "slice_and_wait": [{"state": "error", "message": "object outside the plate"}],
        })
        with self.assertRaises(slicing.SliceFailed):
            s.slice_file("clip.stl")
        self.assertIn(("delete_object", {"object_id": 3}), s.calls)


class FakeSlicer:
    def __init__(self, grams):
        self.grams = list(grams)
        self.files = []

    def slice_file(self, path, infill=""):
        self.files.append(Path(path).name)
        g = self.grams.pop(0)
        if g is None:
            raise slicing.SliceFailed("slicing failed")
        return g, 1800.0


@unittest.skipUnless(has("trimesh") and has("numpy"), "needs trimesh and numpy")
class Geometry(unittest.TestCase):
    def setUp(self):
        import trimesh

        self.trimesh = trimesh
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def stl(self, name, *boxes):
        meshes = []
        for extents, offset in boxes:
            m = self.trimesh.creation.box(extents=extents)
            m.apply_translation(offset)
            meshes.append(m)
        path = self.dir / name
        self.trimesh.util.concatenate(meshes).export(path)
        return {"name": name, "path": path}

    def test_plated_copies_are_costed_once(self):
        f = self.stl("clips.stl", ((10, 20, 5), (0, 0, 0)), ((10, 20, 5), (40, 0, 0)))
        info = slicing.analyze(f["path"])
        self.assertEqual((info["pieces"], info["unique"], info["dup"], info["mm"]), (2, 1, True, 20.0))
        slicer = FakeSlicer([4.2])
        res = slicing.cost_unit([f], slicer=slicer, infill="", work_dir=self.dir / "work")
        self.assertEqual(slicer.files, ["clips.unit.stl"])
        self.assertEqual((res["ms"], res["g"], res["hrs"], res["files"]), ("orcaslicer", 4.2, 0.5, ["clips.stl"]))

    def test_estimator_mixed_and_duplicate_files(self):
        a = self.stl("a.stl", ((10, 10, 10), (0, 0, 0)))
        b = self.stl("b.stl", ((30, 10, 10), (0, 0, 0)))
        copy = self.stl("a_copy.stl", ((10, 10, 10), (5, 5, 5)))
        est = slicing.cost_unit([a], slicer=None, infill="", work_dir=self.dir / "w")
        self.assertEqual(est["ms"], "estimator")
        self.assertAlmostEqual(est["g"], round(slicing.estimate_grams(1000, 600), 2))
        mixed = slicing.cost_unit([a, b, copy], slicer=FakeSlicer([2.0, None]), infill="",
                                  work_dir=self.dir / "w")
        self.assertEqual((mixed["ms"], mixed["files"], mixed["mm"]), ("mixed", ["a.stl", "b.stl"], 30.0))
        self.assertTrue(any("duplicate" in n for n in mixed["notes"]))

    def test_long_part_reports_its_length(self):
        f = self.stl("bumper.stl", ((300, 40, 20), (0, 0, 0)))
        res = slicing.cost_unit([f], slicer=None, infill="", work_dir=self.dir / "w")
        self.assertEqual(res["mm"], 300.0)

    def test_unreadable_file_falls_back_to_the_default(self):
        bad = self.dir / "broken.stl"
        bad.write_bytes(b"not a mesh")
        res = slicing.cost_unit([{"name": "broken.stl", "path": bad}], slicer=None, infill="",
                                work_dir=self.dir / "w")
        self.assertEqual((res["ms"], res["g"]), ("default", None))


if __name__ == "__main__":
    unittest.main()
