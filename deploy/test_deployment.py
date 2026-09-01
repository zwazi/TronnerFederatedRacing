import argparse
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import enrollment
import render_node


ROOT = Path(__file__).resolve().parents[1]


class RenderTests(unittest.TestCase):
    def load(self, name):
        return render_node.load_object(ROOT / "config" / name)

    def test_standalone_example_is_inert(self):
        rendered = render_node.render(
            self.load("cluster.example.json"),
            self.load("node.example.json"),
            production=False,
        )
        manifest = json.loads(rendered["manifest.json"])
        self.assertFalse(manifest["federationEnabled"])
        self.assertFalse(manifest["masterListEnabled"])
        self.assertFalse(manifest["firebaseEnabled"])
        self.assertNotIn("federation.json", rendered)
        self.assertIn("FEDERATION_IMPORT_ENABLED 0", rendered["federation.cfg"])
        self.assertIn("TALK_TO_MASTER 0", rendered["server.cfg"])

    def test_leader_and_follower_render_directional_keys(self):
        cluster = self.load("cluster.example.json")
        leader = render_node.render(
            cluster, self.load("node-leader.example.json"), production=False
        )
        follower = render_node.render(
            cluster, self.load("node-follower.example.json"), production=False
        )
        leader_network = json.loads(leader["federation.json"])
        follower_network = json.loads(follower["federation.json"])
        self.assertEqual(leader_network["protocol_version"], 2)
        self.assertEqual(
            [peer["server_id"] for peer in leader_network["peers"]],
            ["region-b", "region-c"],
        )
        self.assertEqual(follower_network["peers"][0]["server_id"], "region-a")
        self.assertEqual(
            Path(leader_network["peers"][0]["publish_key_file"]).name,
            Path(follower_network["peers"][0]["receive_key_file"]).name,
        )
        self.assertEqual(
            Path(leader_network["peers"][0]["receive_key_file"]).name,
            Path(follower_network["peers"][0]["publish_key_file"]).name,
        )

    def test_production_mode_rejects_examples(self):
        with self.assertRaises(render_node.ConfigurationError):
            render_node.render(
                self.load("cluster.example.json"),
                self.load("node.example.json"),
                production=True,
            )

    def test_production_standalone_retains_firebase_integrations(self):
        cluster = self.load("cluster.example.json")
        cluster.update(
            {
                "cluster_id": "tronner-racing",
                "leader_server_id": "nyc1",
                "members": {"nyc1": "NY"},
                "map_repository": {
                    "source": "firebase",
                    "url": "https://github.com/zwazi/TronnerRepository.git",
                    "branch": "main",
                },
                "firebase": {
                    "enabled": True,
                    "project_id": "tronner-racing",
                    "storage_bucket": "tronner-racing.appspot.com",
                    "database_url": (
                        "https://tronner-racing-default-rtdb." + "firebaseio.com"
                    ),
                    "catalog_enabled": True,
                    "live_dashboard_enabled": True,
                    "management_enabled": True,
                },
            }
        )
        node = self.load("node.example.json")
        node.update(
            {
                "server_id": "nyc1",
                "region_label": "NY",
                "server_name": "Tronner Racing",
                "website_url": "https://tronner.io/",
                "server_dns": "race.tronner.io",
                "public_base_url": "http://maps.tronner.io:8080/",
                "master_list": True,
            }
        )

        rendered = render_node.render(cluster, node, production=True)
        manifest = json.loads(rendered["manifest.json"])
        controller = json.loads(rendered["controller.json"])
        self.assertFalse(manifest["federationEnabled"])
        self.assertTrue(manifest["firebaseEnabled"])
        self.assertTrue(manifest["masterListEnabled"])
        self.assertEqual(controller["server_id"], "nyc1")
        self.assertEqual(controller["federation"], {"role": "off"})
        self.assertTrue(controller["live_dashboard"]["enabled"])
        self.assertTrue(controller["live_dashboard"]["management_enabled"])
        self.assertNotIn("federation.json", rendered)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaises(render_node.ConfigurationError):
                render_node.load_object(path)

    def test_safe_nested_git_branch_is_accepted(self):
        cluster = self.load("cluster.example.json")
        cluster["map_repository"]["branch"] = "release/2026-08"
        rendered = render_node.render(
            cluster, self.load("node.example.json"), production=False
        )
        self.assertEqual(
            json.loads(rendered["controller.json"])["repository_branch"],
            "release/2026-08",
        )

    def test_unsafe_git_branch_is_rejected(self):
        cluster = self.load("cluster.example.json")
        for branch in ("../main", "main..other", "main.lock", "-main"):
            with self.subTest(branch=branch):
                cluster["map_repository"]["branch"] = branch
                with self.assertRaises(render_node.ConfigurationError):
                    render_node.render(
                        cluster, self.load("node.example.json"), production=False
                    )

    def test_production_federation_requires_private_overlay_addresses(self):
        with self.assertRaisesRegex(
            render_node.ConfigurationError, "private overlay"
        ):
            render_node.private_overlay_address("0.0.0.0", "peer host")


class EnrollmentTests(unittest.TestCase):
    def test_approval_creates_private_matching_bundles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            request_args = argparse.Namespace(
                server_id="region-b",
                region_label="B",
                overlay_address="10.77.0.2",
                wireguard_public_key="",
                output=request_path,
            )
            self.assertEqual(enrollment.create_request(request_args), 0)
            output = root / "approved"
            approve_args = argparse.Namespace(
                request=request_path,
                leader_node=ROOT / "config" / "node-leader.example.json",
                leader_overlay_address="10.77.0.1",
                port=4540,
                output=output,
            )
            self.assertEqual(enrollment.approve_request(approve_args), 0)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

            leader = json.loads((output / "leader/federation-fragment.json").read_text())
            follower = json.loads((output / "follower/federation-fragment.json").read_text())
            leader_peer = leader["peer"]
            follower_peer = follower["peers"][0]
            self.assertEqual(
                leader_peer["publish_key_name"], follower_peer["receive_key_name"]
            )
            self.assertEqual(
                leader_peer["receive_key_name"], follower_peer["publish_key_name"]
            )
            for name in (
                leader_peer["publish_key_name"],
                leader_peer["receive_key_name"],
            ):
                leader_key = output / "leader/secrets" / name
                follower_key = output / "follower/secrets" / name
                self.assertEqual(leader_key.read_bytes(), follower_key.read_bytes())
                self.assertEqual(stat.S_IMODE(leader_key.stat().st_mode), 0o600)
                self.assertEqual(len(bytes.fromhex(leader_key.read_text().strip())), 32)

    def test_approval_refuses_nonempty_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "approved"
            output.mkdir()
            (output / "keep").write_text("operator data", encoding="utf-8")
            with self.assertRaises(render_node.ConfigurationError):
                enrollment.require_empty_private_directory(output)


if __name__ == "__main__":
    unittest.main()
