import pathlib
import tempfile
import unittest

import yaml

import render_kubernetes


class RenderKubernetesTest(unittest.TestCase):
    def setUp(self):
        self.project = next(project for project in render_kubernetes.load_projects() if project["slug"] == "graphite-message-lab")

    def test_system_routes_only_through_cluster_services(self):
        documents = render_kubernetes.system_documents([self.project])
        config = next(document for document in documents if document["kind"] == "ConfigMap")["data"]["Caddyfile"]
        services = [document for document in documents if document["kind"] == "Service"]
        self.assertIn("web.graphite-message-lab.svc.cluster.local:8000", config)
        self.assertTrue(all(service["spec"].get("type", "ClusterIP") == "ClusterIP" for service in services))

    def test_bootstrap_isolates_the_namespace(self):
        documents = render_kubernetes.bootstrap_documents(self.project)
        namespace = next(document for document in documents if document["kind"] == "Namespace")
        policies = [document for document in documents if document["kind"] == "NetworkPolicy"]
        self.assertEqual("restricted", namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"])
        self.assertIn("default-deny", [policy["metadata"]["name"] for policy in policies])
        self.assertNotIn("Secret", [document["kind"] for document in documents])

    def test_workloads_use_recorded_images_and_secret_references(self):
        documents = render_kubernetes.workload_documents(self.project)
        deployments = [document for document in documents if document["kind"] == "Deployment"]
        images = [deployment["spec"]["template"]["spec"]["containers"][0]["image"] for deployment in deployments]
        serialized = yaml.safe_dump_all(documents)
        self.assertTrue(all(":sha-" in image for image in images))
        self.assertIn("secretKeyRef", serialized)
        self.assertNotIn("kind: Secret", serialized)

    def test_rendered_files_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            render_kubernetes.write_documents(output / "system.yaml", render_kubernetes.system_documents([self.project]))
            documents = list(yaml.safe_load_all((output / "system.yaml").read_text(encoding="utf-8")))
            self.assertGreater(len(documents), 1)


if __name__ == "__main__":
    unittest.main()
