import argparse
import hashlib
import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

SIZES = {
    "tiny": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}},
    "small": {"requests": {"cpu": "100m", "memory": "192Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
    "medium": {"requests": {"cpu": "250m", "memory": "384Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}},
}


def metadata(name, namespace=None, labels=None):
    value = {"name": name}
    if namespace:
        value["namespace"] = namespace
    if labels:
        value["labels"] = labels
    return value


def resource(api_version, kind, name, namespace=None, labels=None, **fields):
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": metadata(name, namespace, labels),
        **fields,
    }


def pod_security():
    return {
        "runAsNonRoot": True,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def container_security():
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


def load_projects():
    projects = []
    for path in sorted((ROOT / "projects").glob("*.yml")):
        project = yaml.safe_load(path.read_text(encoding="utf-8"))
        if project.get("deploy", {}).get("kind") == "kubernetes":
            projects.append(project)
    return projects


def image_tags(slug):
    path = ROOT / "tags" / f"{slug}.json"
    if not path.exists():
        raise ValueError(f"{slug}: missing {path.relative_to(ROOT)}")
    return json.loads(path.read_text(encoding="utf-8"))


def env_entries(project, names):
    config = project["deploy"].get("config", {})
    secrets = set(project["deploy"].get("secrets", []))
    entries = []
    for name in names:
        source = "configMapKeyRef" if name in config else "secretKeyRef"
        ref_name = f"{project['slug']}-config" if name in config else f"{project['slug']}-secrets"
        if name not in config and name not in secrets:
            raise ValueError(f"{project['slug']}: undeclared environment variable {name}")
        entries.append({"name": name, "valueFrom": {source: {"name": ref_name, "key": name}}})
    return entries


def system_documents(projects):
    routes = {}
    for project in projects:
        for name, service_definition in project["deploy"]["services"].items():
            route = service_definition.get("route")
            if route and route != "none":
                routes.setdefault(project["deploy"]["host"], []).append((route, name, project["slug"], service_definition["port"]))

    config = ["{", "  admin off", "  auto_https off", "}", ""]
    for host, host_routes in sorted(routes.items()):
        config.append(f"http://{host}:8080 {{")
        for route, service_name, namespace, port in sorted(host_routes, key=lambda item: (item[0] == "/", -len(item[0]))):
            target = f"http://{service_name}.{namespace}.svc.cluster.local:{port}"
            matcher = "handle" if route == "/" else f"handle {route}*"
            config.extend([f"  {matcher} {{", f"    reverse_proxy {target}", "  }"])
        config.extend(["}", ""])

    labels = {"app.kubernetes.io/part-of": "graphite-platform"}
    gateway_labels = {**labels, "app.kubernetes.io/name": "graphite-gateway"}
    tunnel_labels = {**labels, "app.kubernetes.io/name": "cloudflared"}
    return [
        resource(
            "v1",
            "Namespace",
            "graphite-system",
            labels={
                **labels,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/warn": "restricted",
            },
        ),
        resource(
            "v1",
            "ConfigMap",
            "graphite-gateway",
            "graphite-system",
            labels,
            data={"Caddyfile": "\n".join(config)},
        ),
        deployment(
            "graphite-gateway",
            "graphite-system",
            gateway_labels,
            [{
                "name": "gateway",
                "image": "caddy:2.10.2-alpine",
                "ports": [{"name": "http", "containerPort": 8080}],
                "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}},
                "securityContext": {**container_security(), "runAsUser": 1000, "runAsGroup": 1000},
                "volumeMounts": [{"name": "config", "mountPath": "/etc/caddy", "readOnly": True}, {"name": "data", "mountPath": "/data"}, {"name": "config-data", "mountPath": "/config"}],
                "readinessProbe": {"tcpSocket": {"port": "http"}, "periodSeconds": 10},
            }],
            volumes=[{"name": "config", "configMap": {"name": "graphite-gateway"}}, {"name": "data", "emptyDir": {}}, {"name": "config-data", "emptyDir": {}}],
        ),
        service("graphite-gateway", "graphite-system", gateway_labels, 8080),
        deployment(
            "cloudflared",
            "graphite-system",
            tunnel_labels,
            [{
                "name": "cloudflared",
                "image": "cloudflare/cloudflared:2026.9.1",
                "args": ["tunnel", "--no-autoupdate", "run"],
                "env": [{"name": "TUNNEL_TOKEN", "valueFrom": {"secretKeyRef": {"name": "cloudflare-tunnel", "key": "TUNNEL_TOKEN"}}}],
                "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "128Mi"}},
                "securityContext": container_security(),
            }],
        ),
    ]


def deployment(name, namespace, labels, containers, volumes=None, replicas=1):
    spec = {
        "serviceAccountName": f"{namespace}-workload" if namespace != "graphite-system" else "default",
        "automountServiceAccountToken": False,
        "securityContext": pod_security(),
        "containers": containers,
    }
    if volumes:
        spec["volumes"] = volumes
    return resource(
        "apps/v1",
        "Deployment",
        name,
        namespace,
        labels,
        spec={"replicas": replicas, "selector": {"matchLabels": {"app.kubernetes.io/name": name}}, "template": {"metadata": {"labels": labels}, "spec": spec}},
    )


def service(name, namespace, labels, port):
    return resource(
        "v1",
        "Service",
        name,
        namespace,
        labels,
        spec={"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": "http", "port": port, "targetPort": port}]},
    )


def bootstrap_documents(project):
    slug = project["slug"]
    labels = {"app.kubernetes.io/part-of": slug}
    return [
        resource(
            "v1",
            "Namespace",
            slug,
            labels={
                **labels,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/warn": "restricted",
            },
        ),
        resource("v1", "ServiceAccount", f"{slug}-workload", slug, labels, automountServiceAccountToken=False),
        resource(
            "v1",
            "ResourceQuota",
            f"{slug}-quota",
            slug,
            labels,
            spec={"hard": {"requests.cpu": "750m", "requests.memory": "1Gi", "limits.cpu": "2", "limits.memory": "2Gi", "persistentvolumeclaims": "3", "requests.storage": "10Gi"}},
        ),
        resource(
            "v1",
            "LimitRange",
            f"{slug}-limits",
            slug,
            labels,
            spec={"limits": [{"type": "Container", "defaultRequest": {"cpu": "25m", "memory": "64Mi"}, "default": {"cpu": "200m", "memory": "128Mi"}}]},
        ),
        resource("v1", "ConfigMap", f"{slug}-config", slug, labels, data=project["deploy"].get("config", {})),
        resource("networking.k8s.io/v1", "NetworkPolicy", "default-deny", slug, labels, spec={"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
        resource("networking.k8s.io/v1", "NetworkPolicy", "allow-namespace", slug, labels, spec={"podSelector": {}, "ingress": [{"from": [{"podSelector": {}}]}], "egress": [{"to": [{"podSelector": {}}]}], "policyTypes": ["Ingress", "Egress"]}),
        resource("networking.k8s.io/v1", "NetworkPolicy", "allow-dns", slug, labels, spec={"podSelector": {}, "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}], "policyTypes": ["Egress"]}),
        resource("networking.k8s.io/v1", "NetworkPolicy", "allow-internet", slug, labels, spec={"podSelector": {}, "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]}}]}], "policyTypes": ["Egress"]}),
        resource("networking.k8s.io/v1", "NetworkPolicy", "allow-gateway", slug, labels, spec={"podSelector": {"matchExpressions": [{"key": "graphite.project/route", "operator": "Exists"}]}, "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "graphite-system"}}, "podSelector": {"matchLabels": {"app.kubernetes.io/name": "graphite-gateway"}}}]}], "policyTypes": ["Ingress"]}),
    ]


def dependency_documents(project):
    slug = project["slug"]
    documents = []
    dependencies = project["deploy"].get("dependencies", {})
    if "postgres" in dependencies:
        documents.extend(stateful_dependency(project, "postgres", "postgres:18.6-trixie", 5432, dependencies["postgres"]["storage"], ["-c", "password_encryption=scram-sha-256"], ["POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"], {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": "384Mi"}}))
    if "redis" in dependencies:
        documents.extend(stateful_dependency(project, "redis", "redis:8.10.1-trixie", 6379, dependencies["redis"]["storage"], ["redis-server", "--appendonly", "yes", "--maxmemory", "64mb", "--maxmemory-policy", "noeviction"], [], {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "200m", "memory": "96Mi"}}))
    return documents


def stateful_dependency(project, name, image, port, storage, args, env_names, resources):
    slug = project["slug"]
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": slug}
    container = {
        "name": name,
        "image": image,
        "ports": [{"name": name, "containerPort": port}],
        "args": args,
        "env": env_entries(project, env_names),
        "resources": resources,
        "securityContext": {**container_security(), **({"runAsUser": 999, "runAsGroup": 999} if name == "postgres" else {})},
        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql" if name == "postgres" else "/data"}],
    }
    stateful_set = resource(
        "apps/v1",
        "StatefulSet",
        name,
        slug,
        labels,
        spec={
            "serviceName": name,
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {"metadata": {"labels": labels}, "spec": {"serviceAccountName": f"{slug}-workload", "automountServiceAccountToken": False, "securityContext": pod_security(), "containers": [container]}},
            "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": storage}}}}],
        },
    )
    dependency_service = resource("v1", "Service", name, slug, labels, spec={"selector": {"app.kubernetes.io/name": name}, "ports": [{"name": name, "port": port, "targetPort": port}]})
    return [stateful_set, dependency_service]


def workload_documents(project):
    slug = project["slug"]
    tags = image_tags(slug)
    documents = dependency_documents(project)
    for name, service_definition in project["deploy"]["services"].items():
        if name not in tags:
            raise ValueError(f"{slug}: missing image tag for service {name}")
        labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": slug}
        if service_definition.get("route") not in (None, "none"):
            labels["graphite.project/route"] = "true"
        container = {
            "name": name,
            "image": f"ghcr.io/project-graphite/{slug}/{name}:{tags[name]}",
            "env": env_entries(project, service_definition.get("env", [])),
            "resources": SIZES[service_definition.get("size", "tiny")],
            "securityContext": container_security(),
        }
        if "command" in service_definition:
            container["command"] = service_definition["command"]
        if "port" in service_definition:
            container["ports"] = [{"name": "http", "containerPort": service_definition["port"]}]
        health = service_definition.get("health", {})
        if "readiness" in health:
            container["readinessProbe"] = {"httpGet": {"path": health["readiness"], "port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10}
        if "liveness" in health:
            container["livenessProbe"] = {"httpGet": {"path": health["liveness"], "port": "http"}, "initialDelaySeconds": 15, "periodSeconds": 20}
        documents.append(deployment(name, slug, labels, [container], replicas=service_definition.get("replicas", 1)))
        if "port" in service_definition:
            documents.append(service(name, slug, labels, service_definition["port"]))

    migration = project["deploy"].get("migration")
    if migration:
        service_definition = project["deploy"]["services"][migration["service"]]
        tag = tags[migration["service"]]
        digest = hashlib.sha256(tag.encode()).hexdigest()[:10]
        labels = {"app.kubernetes.io/name": "migration", "app.kubernetes.io/part-of": slug}
        documents.append(resource(
            "batch/v1",
            "Job",
            f"migration-{digest}",
            slug,
            labels,
            spec={"backoffLimit": 3, "ttlSecondsAfterFinished": 86400, "template": {"metadata": {"labels": labels}, "spec": {"restartPolicy": "OnFailure", "serviceAccountName": f"{slug}-workload", "automountServiceAccountToken": False, "securityContext": pod_security(), "containers": [{"name": "migration", "image": f"ghcr.io/project-graphite/{slug}/{migration['service']}:{tag}", "command": migration["command"], "env": env_entries(project, service_definition.get("env", [])), "resources": SIZES["tiny"], "securityContext": container_security()}]}}},
        ))
    return documents


def write_documents(path, documents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    projects = load_projects()
    system = system_documents(projects)
    write_documents(args.output / "cluster" / "bootstrap.yaml", [document for document in system if document["kind"] != "Deployment"])
    write_documents(args.output / "cluster" / "workloads.yaml", [document for document in system if document["kind"] == "Deployment"])
    for project in projects:
        destination = args.output / project["slug"]
        write_documents(destination / "bootstrap.yaml", bootstrap_documents(project))
        write_documents(destination / "workloads.yaml", workload_documents(project))
    print(f"Rendered {len(projects)} Kubernetes projects to {args.output}.")


if __name__ == "__main__":
    main()
