import tempfile
import unittest
import hashlib
from pathlib import Path

from TronnerRacing import MAP_SUFFIX, MapRepository, install_immutable_file


def map_bytes(version: str, spawn_x: int) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Resource type="aamap" name="Race" '
        f'version="{version}" author="Tester" category="maps">\n'
        ' <Map version="0.2.8">\n'
        '  <World><Field><Axes number="8"/>'
        f'<Spawn x="{spawn_x}" y="0" xdir="1" ydir="0"/>'
        '</Field></World>\n'
        ' </Map>\n'
        '</Resource>\n'
    ).encode()


def repository(root: Path) -> MapRepository:
    checkout = root / "repository"
    checkout.mkdir()
    dtd = root / "dtd"
    dtd.mkdir()
    return MapRepository(
        {
            "repository_git_url": "unused",
            "repository_checkout": str(checkout),
            "public_dir": str(root / "public"),
            "resource_cache_dir": str(root / "cache"),
            "map_override_dir": str(root / "overrides"),
            "map_revision_dir": str(root / "revisions"),
            "dtd_source_dir": str(dtd),
        }
    )


class ImmutableResourceTests(unittest.TestCase):
    def test_changed_same_version_gets_persistent_new_resource_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = repository(root)
            source = repo.checkout / "Tester/maps/Race-v1.aamap.xml"
            source.parent.mkdir(parents=True)
            original = map_bytes("v1", 1)
            source.write_bytes(original)

            repo.scan()
            v1_key = f"Tester/maps/Race-v1{MAP_SUFFIX}"
            self.assertEqual(set(repo.catalog), {v1_key})
            self.assertEqual(repo.catalog[v1_key].axes, 8)
            repo.cache_for_server(repo.catalog[v1_key])

            source.write_bytes(map_bytes("v1", 2))
            repo.scan()
            v2_key = f"Tester/maps/Race-v2{MAP_SUFFIX}"
            self.assertEqual(set(repo.catalog), {v2_key})
            self.assertEqual((repo.public_dir / v1_key).read_bytes(), original)
            self.assertEqual((repo.cache_dir / v1_key).read_bytes(), original)
            self.assertIn(b'version="v2"', (repo.public_dir / v2_key).read_bytes())
            self.assertIn(b'x="2"', (repo.public_dir / v2_key).read_bytes())

            # Restart/rescan stability: the same repository bytes reuse v2.
            repo.scan()
            self.assertEqual(set(repo.catalog), {v2_key})

            # A second unsafe edit cannot overwrite v1 or v2; it becomes v3.
            source.write_bytes(map_bytes("v1", 3))
            repo.scan()
            v3_key = f"Tester/maps/Race-v3{MAP_SUFFIX}"
            self.assertEqual(set(repo.catalog), {v3_key})
            self.assertIn(b'x="3"', (repo.public_dir / v3_key).read_bytes())
            self.assertIn(b'x="2"', (repo.public_dir / v2_key).read_bytes())

            # An active old key resolves to its exact cached bytes, not the
            # repository source alias now pointing at the synthesized key.
            active = repo.find_by_spec(v1_key)
            self.assertIsNotNone(active)
            self.assertEqual(active.version, "v1")
            self.assertIn(b'x="1"', active.local_path.read_bytes())

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "published"
            source.write_bytes(b"new")
            destination.write_bytes(b"old")

            with self.assertRaisesRegex(RuntimeError, "immutable resource conflict"):
                install_immutable_file(source, destination)
            self.assertEqual(destination.read_bytes(), b"old")

    def test_federated_resource_requires_exact_hash_and_internal_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = repository(root)
            data = map_bytes("v1", 1)
            key = f"Tester/maps/Race-v1{MAP_SUFFIX}"
            digest = hashlib.sha256(data).hexdigest()

            entry = repo.install_federated_resource(key, data, digest)

            self.assertEqual(entry.key, key)
            self.assertEqual((repo.public_dir / key).read_bytes(), data)
            self.assertEqual((repo.cache_dir / key).read_bytes(), data)
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                repo.install_federated_resource(key, data, "0" * 64)
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                repo.install_federated_resource(
                    f"Tester/maps/Other-v1{MAP_SUFFIX}", data, digest
                )

    def test_synthesized_version_never_collides_with_repository_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = repository(root)
            v1_source = repo.checkout / "Tester/maps/Race-v1.aamap.xml"
            v1_source.parent.mkdir(parents=True)
            v1_source.write_bytes(map_bytes("v1", 1))
            repo.scan()

            # The repository independently publishes a real v2 while also
            # changing v1 unsafely. The changed v1 must skip reserved v2.
            v1_source.write_bytes(map_bytes("v1", 2))
            v2_source = repo.checkout / "Tester/maps/Race-v2.aamap.xml"
            v2_source.write_bytes(map_bytes("v2", 9))
            repo.scan()

            v2_key = f"Tester/maps/Race-v2{MAP_SUFFIX}"
            v3_key = f"Tester/maps/Race-v3{MAP_SUFFIX}"
            self.assertEqual(set(repo.catalog), {v2_key, v3_key})
            self.assertIn(b'x="9"', repo.catalog[v2_key].local_path.read_bytes())
            self.assertIn(b'x="2"', repo.catalog[v3_key].local_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
