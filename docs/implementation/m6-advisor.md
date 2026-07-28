# M6: Encounter Advisor vertical slice

`advise` accepts either authored scenarios or normalized enemy views from a versioned dataset. It ranks phase/skill scenarios by worst-case health fraction, preserves the combat trace and classification, and evaluates these defense changes independently:

- +100 DA
- +10% health
- +200 armor on all six hit locations
- +5 percentage points damage absorption
- +10 percentage points uncapped resistance for each represented damage type

Results include damage reduction, health-fraction improvement, lethal state, confidence, evidence, references, and unknowns. JSON and Markdown are supported. Fixture regression covers `single_hit`, `shotgun`, `combo`, and `overlap`.

Owned-data verification now covers 13 phase records across eight English enemy names: Archmage Aleksander, Grava'Thul, Kaisan, Kubacabra, Moosilauke, Reaper of the Lost, and their available phase variants. The current real-data pass produced 67 scenarios: 61 single hits, four worst-case all-contact shotguns, and two debuff/follow-up windows.

The combo windows come from DBR defensive-ability-reduction duration fields and pair the debuff skill with the strongest other normalized damaging skill in that phase. They are deliberately marked low confidence with `ai_order_and_contact_timing_not_guaranteed`; static records establish a possible window, not that the AI will execute or connect that exact order. Projectile count likewise remains a worst-case all-contact branch and reports actual positioning/contact count as unknown.

Direct `offensivePoison*` damage is normalized as acid. Duration-bearing poison fields remain poison damage-over-time. The advisor also maps legacy generated `poison` / `acid_poison` packet labels to acid so older content-addressed datasets remain analyzable without silent resistance mismatches.
