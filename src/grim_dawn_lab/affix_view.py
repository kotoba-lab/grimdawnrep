from __future__ import annotations
import json
from pathlib import Path
from typing import Mapping
from grim_dawn_lab.item_view import _numeric

AFFIX_VIEW_RULE_ID="affix-view-v1"
def build_affix_view(dataset: Mapping):
 rows=[]; excluded=[]
 tables={}
 for table_id,table in dataset.get("records",{}).items():
  if not table_id.startswith("records/items/lootaffixes/") or table["fields"].get("Class") != "LootRandomizerTable": continue
  for key,value in table["fields"].items():
   if key.startswith("randomizerName") and isinstance(value,str): tables.setdefault(value.lower(),[]).append(table_id)
 for record_id,record in sorted(dataset.get("records",{}).items()):
  if not record_id.startswith("records/items/lootaffixes/"): continue
  fields=record["fields"]
  if fields.get("Class") == "LootRandomizerTable":
   continue
  if fields.get("Class") != "LootRandomizer":
   excluded.append({"record":record_id,"class":fields.get("Class"),"reason":"unknown_structure"}); continue
  stats={k:v for k,x in fields.items() if (v:=_numeric(x)) is not None}
  if not stats: excluded.append({"record":record_id,"reason":"no_numeric_stats"}); continue
  tag=next((fields.get(k) for k in ("lootRandomizerName","prefixName","suffixName","itemNameTag") if isinstance(fields.get(k),str)),None)
  name={"tags":[tag] if tag else []}; unresolved=[]
  for locale in ("en","ja"):
   value=dataset.get("localization",{}).get(locale,{}).get(tag) if tag else None; name[locale]=value
   if tag and value is None: unresolved.append({"locale":locale,"tag":tag})
  if unresolved:name["unresolved_tags"]=unresolved
  applicable="unknown"
  rows.append({"record":record_id,"rule":AFFIX_VIEW_RULE_ID,"name":name,"stats":stats,"level_requirement":_numeric(fields.get("levelRequirement")),"applicable":applicable,"provenance":record["provenance"]})
 return rows,excluded
def write_affix_view(dataset:Mapping,root:Path):
 rows,excluded=build_affix_view(dataset);target=root/str(dataset["dataset_id"]);target.mkdir(parents=True,exist_ok=True);out=target/'affixes-v1.jsonl';exc=target/'affixes-v1.excluded.jsonl'
 out.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf8');exc.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in excluded),encoding='utf8');return out,exc,len(rows),len(excluded)
