from __future__ import annotations
import unittest
from grim_dawn_lab.affix_view import build_affix_view
from grim_dawn_lab.item_query import query_items
class AffixViewTests(unittest.TestCase):
 def test_stats_unresolved_tag_unknown_and_query_compatibility(self):
  d={"localization":{"en":{},"ja":{}},"records":{"records/items/lootaffixes/a.dbr":{"fields":{"Class":"LootRandomizer","offensiveChaosModifier":"12","levelRequirement":"50","lootRandomizerName":"tagMissing"},"provenance":{}},"records/items/lootaffixes/t.dbr":{"fields":{"Class":"LootRandomizerTable","randomizerName1":"records/items/lootaffixes/a.dbr"},"provenance":{}}}}
  rows,excluded=build_affix_view(d);self.assertEqual(1,len(rows));self.assertEqual("unknown",rows[0]["applicable"]);self.assertIn("unresolved_tags",rows[0]["name"]);self.assertEqual([rows[0]],query_items(rows,stat_filters=["offensiveChaosModifier>=1"]))
