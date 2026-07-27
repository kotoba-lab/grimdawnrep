from __future__ import annotations
import unittest
from grim_dawn_lab.item_query import query_items

ROWS=[
 {"record":"a","slot":"weapon_1h","classification":"Legendary","level_requirement":90,"name":{"en":"Chaos Axe","ja":"カオス斧"},"stats":{"chaos":10,"fire":2}},
 {"record":"b","slot":"weapon_1h","classification":"Legendary","level_requirement":94,"name":{"en":"Chaos Sword","ja":"炎の剣"},"stats":{"chaos":30,"fire":0}},
 {"record":"c","slot":"shield","classification":"Epic","level_requirement":100,"name":{"en":"Shield","ja":"盾"},"stats":{"chaos":20,"fire":4}},
 {"record":"missing","name":{},"stats":{}},
]
class ItemQueryTests(unittest.TestCase):
 def test_combined_filters_stat_sort_and_limit(self):
  rows=query_items(ROWS,slots=["weapon_1h","shield"],classifications=["Legendary"],min_level=90,stat_filters=["chaos>=1","fire"],limit=1)
  self.assertEqual(["a"],[row["record"] for row in rows])
 def test_name_japanese_and_level_sort_with_missing_fields(self):
  rows=query_items(ROWS,name="炎",max_level=95,limit=50)
  self.assertEqual(["b"],[row["record"] for row in rows])
  rows=query_items(ROWS,limit=2)
  self.assertEqual(["c","b"],[row["record"] for row in rows])
