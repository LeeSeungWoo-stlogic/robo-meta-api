"""v3.1 generic config 파싱 unit test."""
from __future__ import annotations

import importlib
import os
import unittest
import unittest.mock


class ConfigJoinParseTest(unittest.TestCase):
    def test_csv_list_and_set_defaults(self) -> None:
        env = {
            "JOIN_ONTOLOGY_REL_TYPES": "fkToTable,dataFlowsTo",
            "JOIN_CONVENTION_EXCLUDE": "id, SEQ ,",
            "DECISION_SCHEMA_ALLOWLIST": "",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            import app.config as cfg

            importlib.reload(cfg)
            s = cfg.Settings()
            self.assertEqual(s.join_ontology_rel_types, ("fkToTable", "dataFlowsTo"))
            self.assertEqual(s.join_convention_exclude, frozenset({"id", "seq"}))
            self.assertEqual(s.decision_schema_allowlist, ())
            importlib.reload(cfg)


if __name__ == "__main__":
    unittest.main()
