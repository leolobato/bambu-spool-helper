from __future__ import annotations

import unittest

from app.services.print_tracking.gcode_parser import evaluate_gcode


class GcodeParserTests(unittest.TestCase):
    def test_relative_multicolor_arcs_retractions_and_sparse_indices(self):
        gcode = """
        M83
        M620 S2A
        M73 L0
        G1 E2
        G1 E-0.8
        G2 X2 E1.5
        M620 S7
        G3 E3
        M73 L1
        """
        self.assertEqual(
            evaluate_gcode(gcode),
            {0: {2: 2.7, 7: 3.0}},
        )

    def test_absolute_extrusion_g92_and_retraction(self):
        gcode = """
        M82
        T0
        M73 L0
        G1 E10
        G1 E8
        G1 E12
        G92 E0
        G1 E2
        M73 L1
        """
        # 10 + 2 after repaying the 2mm retraction + 2 after reset.
        self.assertEqual(evaluate_gcode(gcode), {0: {0: 14.0}})

    def test_unload_and_malformed_lines_are_ignored(self):
        gcode = """
        M83
        M620 S0
        M73 L0
        G1 E1
        nonsense E999
        M620 S255
        G1 E10
        M620 S99999
        G1 E10
        """
        self.assertEqual(evaluate_gcode(gcode), {0: {0: 1.0}})
