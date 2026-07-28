from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from grim_dawn_lab.grimtools import (
    GrimToolsError, build_from_grimtools_html, build_from_grimtools_upload_response,
    canonical_build_url, compare_same_save_builds, fetch_grimtools_build,
)


FIXTURE = Path(__file__).parent / "fixtures/grimtools/shared.html"


class _Response:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit: int) -> bytes:
        return self.data


class GrimToolsTests(unittest.TestCase):
    def test_canonical_url_rejects_wrong_origin_and_shape(self) -> None:
        self.assertEqual(canonical_build_url("http://grimtools.com/calc/DV9G4mQN?x=1"), ("DV9G4mQN", "https://www.grimtools.com/calc/DV9G4mQN"))
        for url in ("https://example.com/calc/DV9G4mQN", "https://grimtools.com/db/DV9G4mQN", "https://grimtools.com/calc/short"):
            with self.subTest(url=url), self.assertRaises(GrimToolsError):
                canonical_build_url(url)

    def test_fixture_maps_public_metadata_and_explicit_unknown(self) -> None:
        result = build_from_grimtools_html("https://www.grimtools.com/calc/DV9G4mQN", FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(result["character"], {"level": 100, "class_display": "Purifier", "mastery_external_ids": ["02", "07"]})
        self.assertEqual(result["game_version"], "1.2.1.6")
        self.assertEqual(result["equipment"][0]["base_external_id"], "it1")
        self.assertEqual(result["skills"][0]["level"], 12)
        self.assertEqual(result["unknowns"][0]["code"], "grimtools_external_ids_require_local_record_mapping")

    def test_fetch_uses_cache_after_one_request(self) -> None:
        calls = []

        def opener(*args, **kwargs):
            calls.append((args, kwargs))
            return _Response(FIXTURE.read_bytes())

        with tempfile.TemporaryDirectory() as temporary:
            first = fetch_grimtools_build("https://grimtools.com/calc/DV9G4mQN", cache_root=Path(temporary), opener=opener)
            second = fetch_grimtools_build("https://grimtools.com/calc/DV9G4mQN", cache_root=Path(temporary), opener=opener)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["source"]["cache"], "miss")
        self.assertEqual(second["source"]["cache"], "hit")

    def test_same_save_upload_normalizes_and_compares_without_global_id_claim(self) -> None:
        payload = {"data": {
            "bio": {"level": 97, "physique": 730, "cunning": 178, "spirit": 58, "health": 2086, "energy": 266},
            "equipment": {"head": {"item": "it1"}, "ring1": {"item": "it2"}, "weapon1Alt": {}},
            "skills": [{"name": "sk1", "level": 12, "enabled": 1, "devotionLevel": 0}],
        }}
        uploaded = build_from_grimtools_upload_response(payload, source_hash="abc")
        local = {
            "character": {"level": 97},
            "attributes": {"physique": 730, "cunning": 178, "spirit": 58, "health": 2086, "energy": 266},
            "equipment": [{"slot": "head", "base_record": "records/items/head.dbr"}, {"slot": "ring_1", "base_record": "records/items/ring.dbr"}],
            "skills": [{"record": "records/skills/one.dbr", "level": 12, "enabled": True, "devotion_level": 0}],
        }
        comparison = compare_same_save_builds(local, uploaded)
        self.assertEqual(uploaded["equipment"][1]["slot"], "ring_1")
        self.assertTrue(comparison["equivalent_on_comparable_fields"])
        self.assertEqual(comparison["counts"]["skill_matches"], 1)
        self.assertIn("not claimed as a global", comparison["boundary"])

    def test_upload_response_rejects_missing_data(self) -> None:
        with self.assertRaisesRegex(GrimToolsError, "invalid_upload_response"):
            build_from_grimtools_upload_response({})


if __name__ == "__main__":
    unittest.main()
