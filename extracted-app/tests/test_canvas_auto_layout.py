import json
import shutil
import subprocess
import unittest
from pathlib import Path


UI_PATH = Path(__file__).parents[1] / "src" / "recovery_service" / "static" / "ui.html"


class CanvasAutoLayoutTests(unittest.TestCase):
    def test_auto_layout_controls_are_wired_to_editable_versions(self) -> None:
        html = UI_PATH.read_text(encoding="utf-8")

        self.assertIn('id="dataPlatformAutoLayoutBtn"', html)
        self.assertIn('dataPlatformAutoLayoutBtn: !isProd && hasAction("dataPlatform:design")', html)
        self.assertIn('persistDataPlatformVersionAfterNodeChange("画布已自动排布并保存。")', html)
        self.assertIn("updateDataPlatformCanvasGeometry(inner, edgeSvg, nodes, edges)", html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the layout behavior test")
    def test_layout_is_deterministic_and_handles_branches_cycles_and_isolated_nodes(self) -> None:
        html = UI_PATH.read_text(encoding="utf-8")
        start = html.index("function computeDataPlatformAutoLayout")
        end = html.index("\n\n      async function autoLayoutDataPlatformCanvas", start)
        function_source = html[start:end]
        script = f"""
const compute = new Function({json.dumps(function_source)} + '; return computeDataPlatformAutoLayout;')();
const nodes = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((key, index) => ({{ key, x: index, y: index }}));
const edges = [
  {{ source: 'A', target: 'B' }}, {{ source: 'A', target: 'C' }},
  {{ source: 'B', target: 'D' }}, {{ source: 'C', target: 'D' }},
  {{ source: 'E', target: 'F' }}, {{ source: 'F', target: 'E' }}
];
const first = compute(nodes, edges);
const second = compute(nodes, edges);
const byKey = Object.fromEntries(first.map((node) => [node.key, node]));
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
assert(JSON.stringify(first) === JSON.stringify(second), 'layout must be deterministic');
assert(byKey.A.y < byKey.B.y && byKey.A.y < byKey.C.y, 'upstream level is invalid');
assert(byKey.B.y < byKey.D.y && byKey.C.y < byKey.D.y, 'downstream level is invalid');
assert(byKey.B.x !== byKey.C.x, 'same-level branches overlap');
assert(Number.isFinite(byKey.E.x) && Number.isFinite(byKey.F.y), 'cycle coordinates are invalid');
assert(new Set(first.map((node) => `${{node.x}},${{node.y}}`)).size === first.length, 'node coordinates overlap');
assert(byKey.G.x !== byKey.H.x || byKey.G.y !== byKey.H.y, 'isolated nodes overlap');
const wideNodes = ['root', 'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'end'].map((key) => ({{ key }}));
const wideEdges = ['b1', 'b2', 'b3', 'b4', 'b5', 'b6'].flatMap((key) => [
  {{ source: 'root', target: key }},
  {{ source: key, target: 'end' }}
]);
const wide = compute(wideNodes, wideEdges);
assert(Math.max(...wide.map((node) => node.x + 230)) > 980, 'wide branch layout must expand the canvas');
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
