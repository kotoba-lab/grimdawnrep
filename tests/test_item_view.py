from __future__ import annotations
import unittest
from grim_dawn_lab.item_view import build_item_view, build_item_view_v2

class ItemViewTests(unittest.TestCase):
    def test_normalizes_name_stats_references_and_fail_closed_cases(self) -> None:
        dataset={"localization":{"en":{"tagQuality":"Mythical","tagStyle":"Awakened","tagName":"Axe"},"ja":{"tagQuality":"神話級","tagStyle":"覚醒","tagName":"斧"}},"records":{
            "records/items/axe.dbr":{"fields":{"Class":"WeaponMelee_Axe","itemClassification":"Legendary","itemLevel":"94","levelRequirement":"90","itemQualityTag":"tagQuality","itemStyleTag":"tagStyle","itemNameTag":"tagName","offensiveChaosModifier":"12","itemSkillName":"records/skills/grant.dbr","augmentSkillName1":"records/skills/augment.dbr","modifiedSkillName1":"records/skills/modified.dbr"},"provenance":{"source_layer":"gdx3","overrides":["base"]}},
            "records/items/unknown.dbr":{"fields":{"Class":"FutureItem","itemNameTag":"tagMissing"},"provenance":{"source_layer":"base","overrides":[]}},
            "records/items/unresolved.dbr":{"fields":{"Class":"WeaponMelee_Axe","itemNameTag":"tagMissing"},"provenance":{"source_layer":"base","overrides":[]}},
        }}
        rows, excluded=build_item_view(dataset)
        self.assertEqual([{"record":"records/items/unknown.dbr","class":"FutureItem","reason":"unknown_class"}], excluded)
        axe=next(row for row in rows if row["record"].endswith("axe.dbr"))
        self.assertEqual("weapon_1h", axe["slot"]); self.assertEqual("Mythical Awakened Axe", axe["name"]["en"])
        self.assertEqual("神話級 覚醒 斧", axe["name"]["ja"]); self.assertEqual(12.0, axe["stats"]["offensiveChaosModifier"])
        self.assertEqual("records/skills/grant.dbr", axe["references"]["item_skill"]); self.assertEqual(["base"], axe["provenance"]["overrides"])
        unresolved=next(row for row in rows if row["record"].endswith("unresolved.dbr"))
        self.assertIn("unresolved_tags", unresolved["name"])

    def test_v2_resolves_grants_conversions_missing_references_and_set_rows(self) -> None:
        dataset={"localization":{"en":{"item":"Item","skill":"Skill","set":"Set"},"ja":{"item":"品","skill":"技能","set":"セット"}},"records":{
            "records/items/item.dbr":{"fields":{"Class":"WeaponMelee_Axe","itemNameTag":"item","itemSkillName":"records/skills/a.dbr","augmentSkillName1":"records/skills/missing.dbr","itemSetName":"records/items/lootsets/set.dbr","conversionInType":"Fire","conversionOutType":"Cold","conversionPercentage":"50","conversionInType2":"Cold","conversionOutType2":"Chaos","conversionPercentage2":"25"},"provenance":{}},
            "records/items/item2.dbr":{"fields":{"Class":"WeaponMelee_Axe","itemNameTag":"item","itemSetName":"records/items/lootsets/set.dbr"},"provenance":{}},
            "records/skills/a.dbr":{"fields":{"skillDisplayName":"skill","offensiveChaosModifier":"7"},"provenance":{}},
            "records/items/lootsets/set.dbr":{"fields":{"itemSetNameTag":"set","itemLevel":"94","offensiveFireModifier":"0;10;20","bonus2":"0;1;2"},"provenance":{}},
        }}
        rows, _, sets=build_item_view_v2(dataset); row=rows[0]
        self.assertEqual("Skill",row["granted"]["references"][0]["name"]["en"]); self.assertEqual(50.0,row["granted"]["conversions"][0]["percentage"])
        self.assertEqual((2,"Cold","Chaos",25.0),(row["granted"]["conversions"][1]["index"],row["granted"]["conversions"][1]["in"],row["granted"]["conversions"][1]["out"],row["granted"]["conversions"][1]["percentage"]))
        self.assertEqual("records/skills/missing.dbr",row["granted"]["unresolved"][0]["record"]); self.assertEqual(1,len(sets)); self.assertEqual(["bonus2","offensiveFireModifier"],[bonus["field"] for bonus in sets[0]["bonuses"]]); self.assertNotIn("itemLevel",[bonus["field"] for bonus in sets[0]["bonuses"]])
