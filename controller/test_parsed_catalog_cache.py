import json
import tempfile
import unittest
from pathlib import Path

from TronnerRacing import MapEntry, MapRepository, SpawnPoint


class ParsedCatalogCacheTests(unittest.TestCase):
    def repository(self, root: Path) -> MapRepository:
        repository = object.__new__(MapRepository)
        repository.firebase = object()
        repository.firebase_root = root / "firebase-catalog"
        repository.checkout = repository.firebase_root / "current"
        repository.public_dir = root / "public"
        repository.cache_dir = root / "resource-cache"
        repository.revision_dir = root / "revisions"
        repository.override_dir = root / "overrides"
        repository.dtd_source_dir = root / "dtd"
        repository.firebase_generation = "generation-1"
        repository.firebase_catalog_version = 42
        repository.excluded_keys = set()
        repository.catalog = {}
        repository.source_to_key = {}
        repository.issues = []
        return repository

    def test_round_trip_uses_local_files_and_preserves_map_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = self.repository(root)
            source = "Tester/maps/Fast-v1.aamap.xml"
            local_path = repository.checkout / source
            public_path = repository.public_dir / source
            local_path.parent.mkdir(parents=True)
            public_path.parent.mkdir(parents=True)
            local_path.write_text("<Resource/>", encoding="utf-8")
            public_path.write_text("<Resource/>", encoding="utf-8")
            entry = MapEntry(
                key=source,
                name="Fast",
                author="Tester",
                version="v1",
                category="maps",
                source_path=source,
                local_path=local_path,
                spawns=(SpawnPoint(1, 2, 1, 0),),
                axes=4,
                map_id="map-1",
                revision_id="revision-1",
                storage_path="maps/revision-1.xml",
                record_key=source,
                checkpoint_ids=(1, 2),
                checkpoint_mode="ordered",
            )
            repository.catalog = {source: entry}
            repository.source_to_key = {source: source}
            repository._write_parsed_catalog_cache()

            loaded = self.repository(root)
            self.assertTrue(loaded._load_parsed_catalog_cache())
            self.assertEqual(set(loaded.catalog), {source})
            self.assertEqual(loaded.catalog[source].spawns, entry.spawns)
            self.assertEqual(loaded.catalog[source].checkpoint_ids, (1, 2))

    def test_cache_is_rejected_when_local_exclusions_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = self.repository(root)
            repository._parsed_catalog_cache_path().parent.mkdir(parents=True)
            repository._parsed_catalog_cache_path().write_text(json.dumps({
                "schemaVersion": repository.PARSED_CATALOG_CACHE_SCHEMA,
                "generation": repository.firebase_generation,
                "catalogVersion": repository.firebase_catalog_version,
                "excludedKeys": [],
                "entries": [],
                "sourceToKey": {},
                "issues": [],
            }), encoding="utf-8")
            repository.excluded_keys = {"held-map"}
            self.assertFalse(repository._load_parsed_catalog_cache())


if __name__ == "__main__":
    unittest.main()
