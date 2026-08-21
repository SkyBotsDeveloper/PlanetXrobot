import unittest

from PLANETXROBOT.core.call import normalize_stream_position


class StreamPositionNormalizationTests(unittest.TestCase):
    def test_timestamp_and_numeric_positions(self):
        self.assertEqual(normalize_stream_position("00:24"), 24.0)
        self.assertEqual(normalize_stream_position("1:02:03"), 3723.0)
        self.assertEqual(normalize_stream_position(24), 24.0)
        self.assertEqual(normalize_stream_position("24.5"), 24.5)
        self.assertEqual(normalize_stream_position(None), 0.0)


if __name__ == "__main__":
    unittest.main()
