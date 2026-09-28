"""Test markers the orchestrator's suite registers (``--strict-markers``)."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "k8s: against a real Kubernetes apiserver; skipped unless "
        "FISHSENSE_K8S_ITEST_KUBECONFIG names a disposable cluster",
    )
