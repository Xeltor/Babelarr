from babelarr.profiling import WorkloadProfiler
from babelarr.profiling_ui import ProfilingDashboard


def test_profiling_dashboard_renders_content() -> None:
    profiler = WorkloadProfiler(enabled=True)
    with profiler.track("demo"):
        pass
    dashboard = ProfilingDashboard(profiler)
    dashboard.register_status_provider("queue", lambda: {"size": 1})

    html = dashboard.render_page()
    assert "Profiling Dashboard" in html
    payload = dashboard.metrics_payload()
    assert "timings" in payload and "demo" in payload["timings"]
    assert payload["status"]["queue"]["size"] == 1


def test_metrics_payload_skips_bad_providers() -> None:
    profiler = WorkloadProfiler(enabled=True)
    dashboard = ProfilingDashboard(profiler)

    def bad_provider() -> dict[str, object]:
        raise RuntimeError("boom")

    dashboard.register_status_provider("error", bad_provider)
    payload = dashboard.metrics_payload()
    assert payload["status"] == {}
