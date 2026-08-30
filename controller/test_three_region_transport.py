import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from federation_sidecar import (
    FanoutPublisher,
    FederationConfig,
    FollowerQueues,
    MultiPeerNetworkProtocol,
)


def free_port(address: str) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((address, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class ThreeRegionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_hub_relays_follower_origin_and_fans_out_leader_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            addresses = {
                "region-a": "127.0.0.1",
                "region-b": "127.0.0.2",
                "region-c": "127.0.0.3",
            }
            regions = {"region-a": "A", "region-b": "B", "region-c": "C"}
            ports = {server_id: free_port(address) for server_id, address in addresses.items()}
            keys = {}
            for name, byte in {
                "a-to-b": b"a",
                "b-to-a": b"b",
                "a-to-c": b"c",
                "c-to-a": b"d",
            }.items():
                path = root / f"{name}.key"
                path.write_bytes(byte * 32)
                path.chmod(0o600)
                keys[name] = path

            def peer(local: str, remote: str, publish: str, receive: str):
                return {
                    "server_id": remote,
                    "region_label": regions[remote],
                    "host": addresses[remote],
                    "port": ports[remote],
                    "expected_ip": addresses[remote],
                    "publish_key_file": str(keys[publish]),
                    "receive_key_file": str(keys[receive]),
                }

            def config(server_id: str, role: str, peers: list[dict]):
                return FederationConfig.from_dict(
                    {
                        "protocol_version": 2,
                        "cluster_id": "tronner-racing",
                        "server_id": server_id,
                        "mode": "both",
                        "role": role,
                        "leader_server_id": "region-a",
                        "region_label": regions[server_id],
                        "members": regions,
                        "listen_host": addresses[server_id],
                        "listen_port": ports[server_id],
                        "peers": peers,
                        "ladderlog": str(root / f"{server_id}.ladderlog"),
                        "engine_export_socket": str(root / f"{server_id}.export.sock"),
                        "controller_publish_socket": str(root / f"{server_id}.publish.sock"),
                        "controller_import_socket": str(root / f"{server_id}.controller.sock"),
                        "engine_import_socket": str(root / f"{server_id}.engine.sock"),
                    }
                )

            configs = {
                "region-a": config(
                    "region-a",
                    "leader",
                    [
                        peer("region-a", "region-b", "a-to-b", "b-to-a"),
                        peer("region-a", "region-c", "a-to-c", "c-to-a"),
                    ],
                ),
                "region-b": config(
                    "region-b",
                    "follower",
                    [peer("region-b", "region-a", "b-to-a", "a-to-b")],
                ),
                "region-c": config(
                    "region-c",
                    "follower",
                    [peer("region-c", "region-a", "c-to-a", "a-to-c")],
                ),
            }
            publishers = {
                server_id: FanoutPublisher(item)
                for server_id, item in configs.items()
            }
            queues = {server_id: FollowerQueues() for server_id in configs}
            transports = []
            try:
                loop = asyncio.get_running_loop()
                for server_id, item in configs.items():
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda server_id=server_id, item=item: MultiPeerNetworkProtocol(
                            item, queues[server_id], publishers[server_id]
                        ),
                        family=socket.AF_INET,
                        local_addr=(item.listen_host, item.listen_port),
                    )
                    transports.append(transport)

                await publishers["region-b"].send(
                    "chat", {"player_id": "bob", "message": "hello from B"}
                )
                leader_packet = await asyncio.wait_for(
                    queues["region-a"].control.get(), timeout=1
                )
                relayed_packet = await asyncio.wait_for(
                    queues["region-c"].control.get(), timeout=1
                )
                self.assertEqual(leader_packet.server_id, "region-b")
                self.assertEqual(relayed_packet.sender_server_id, "region-a")
                self.assertEqual(relayed_packet.server_id, "region-b")

                await publishers["region-a"].send(
                    "round_sync", {"action": "release", "map_key": "map"}
                )
                for follower in ("region-b", "region-c"):
                    packet = await asyncio.wait_for(
                        queues[follower].control.get(), timeout=1
                    )
                    self.assertEqual(packet.server_id, "region-a")
                    self.assertEqual(packet.destination_server_id, follower)
            finally:
                for transport in transports:
                    transport.close()
                for publisher in publishers.values():
                    publisher.close()


if __name__ == "__main__":
    unittest.main()
