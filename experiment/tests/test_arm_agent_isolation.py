"""Each variant's agents see their own tech's tools and no other's: one
agent layer per tech over a base that carries only the clients, and a
skeleton every variant receives that names no tech."""
import unittest
from pathlib import Path

from _ctx import ROOT, tech_of, variant_classes

EXP = Path(ROOT) / "experiment"
REPO = Path(ROOT)


def layer_text(cls):
    return (Path(cls.AGENT_IMAGE_DIR) / "Dockerfile").read_text().lower()


class TestTheAgentLayers(unittest.TestCase):
    def test_every_variant_declares_the_layer_of_its_own_tech(self):
        for cls in variant_classes():
            with self.subTest(variant=cls.ID):
                self.assertIsNotNone(cls.AGENT_IMAGE_DIR)
                self.assertEqual(Path(cls.AGENT_IMAGE_DIR).parent.name, tech_of(cls))

    def test_cluster_tools_only_in_the_keda_layer_and_docker_only_in_pulumi(self):
        for cls in variant_classes():
            with self.subTest(variant=cls.ID):
                text = layer_text(cls)
                self.assertEqual("kubectl" in text, tech_of(cls) == "keda")
                self.assertEqual("docker-${docker_cli_version}" in text, tech_of(cls) == "pulumi")

    def test_the_base_carries_the_clients_and_no_arm_tool(self):
        text = (REPO / "Dockerfile.agent-base").read_text().lower()
        for client in ("claude-code", "opencode-ai", "antigravity"):
            self.assertIn(client, text)
        for arm_tool in ("pip install", "kubectl", "kubeconform", "docker-"):
            self.assertNotIn(arm_tool, text)


class TestTheSharedSkeleton(unittest.TestCase):
    def test_the_skeleton_every_arm_receives_names_no_arm_sdk(self):
        hits = []
        for f in (EXP / "task" / "skeleton").rglob("*"):
            if f.is_file() and f.suffix in ("", ".txt", ".py", ".md", ".toml") or f.name == "Dockerfile":
                text = f.read_text(errors="replace").lower()
                for word in ("import pulumi", "pulumi_docker", "scaledobject", "keda.sh"):
                    if word in text:
                        hits.append((str(f.relative_to(EXP)), word))
        self.assertEqual(hits, [])

    def test_the_skeleton_every_arm_receives_names_no_variant(self):
        names = set()
        for cls in variant_classes():
            names |= {cls.ID, tech_of(cls), *cls.ID.split("_")[:1]}
        names.add("conv_")
        hits = []
        for f in (EXP / "task" / "skeleton").rglob("*"):
            if f.is_file():
                text = f.read_text(errors="replace").lower()
                hits += [(str(f.relative_to(EXP)), n) for n in sorted(names) if n.lower() in text]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
