from __future__ import annotations

import unittest

from app.services.query_runner_mindsdb import _serialize


class Latin1Utf8RepairTests(unittest.TestCase):
    def test_repairs_mojibake_hangul_with_ascii_and_parens(self) -> None:
        original = "금강유역본부(충청)"
        mojibake = original.encode("utf-8").decode("latin-1")
        self.assertNotEqual(mojibake, original)
        self.assertEqual(_serialize(mojibake), original)

    def test_leaves_real_hangul_and_ascii(self) -> None:
        self.assertEqual(_serialize("금강유역본부(충청)"), "금강유역본부(충청)")
        self.assertEqual(_serialize("RWIS"), "RWIS")

    def test_leaves_undecodable_latin1(self) -> None:
        broken = "\x80\x81"
        self.assertEqual(_serialize(broken), broken)
