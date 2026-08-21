import unittest

from PLANETXROBOT.core.call import (
    HRTF_MOVEMENT_HZ,
    _spatial_filter,
    normalize_stream_position,
)


class StreamPositionNormalizationTests(unittest.TestCase):
    def test_timestamp_and_numeric_positions(self):
        self.assertEqual(normalize_stream_position("00:24"), 24.0)
        self.assertEqual(normalize_stream_position("1:02:03"), 3723.0)
        self.assertEqual(normalize_stream_position(24), 24.0)
        self.assertEqual(normalize_stream_position("24.5"), 24.5)
        self.assertEqual(normalize_stream_position(None), 0.0)

    def test_timestamp_phase_is_deterministic(self):
        graph = _spatial_filter("00:24")
        self.assertIn("t+24.000", graph)
        self.assertEqual(HRTF_MOVEMENT_HZ, 1 / 8)
        self.assertIn("[sidebed]", graph)
        self.assertNotIn("[drybed]", graph)


if __name__ == "__main__":
    unittest.main()
