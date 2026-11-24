"""Root conftest: register shared fixture plugins once for the whole suite."""

pytest_plugins = ["tests.fixtures_api", "tests.fixtures_rag"]
