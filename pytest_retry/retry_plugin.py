import pytest
import bdb
from time import sleep
from io import StringIO
from traceback import format_exception
from typing import Generator, Optional
from _pytest.terminal import TerminalReporter
from _pytest.logging import caplog_records_key


outcome_key = pytest.StashKey[str]()
attempts_key = pytest.StashKey[int]()
duration_key = pytest.StashKey[float]()
stages = ("setup", "call", "teardown")


class RetryHandler:
    """
    Stores statistics and reports for flaky tests and fixtures which have
    failed at least once during the test session and need to be retried
    """

    def __init__(self) -> None:
        self.stream = StringIO()
        self.trace_limit: Optional[int] = -1
        self.node_stats: dict[str, dict] = {}

    def log_test_retry(self, attempt: int, test_name: str, err: tuple) -> None:
        formatted_trace = (
            "".join(format_exception(*err, limit=self.trace_limit)).replace("\n", "\n\t").rstrip()
        )
        message = f" failed on attempt {attempt}! Retrying!\n\t"
        self.stream.writelines([f"\t{test_name}", message, formatted_trace, "\n\n"])

    def log_test_totally_failed(self, attempt: int, test_name: str, err: tuple) -> None:
        formatted_trace = (
            "".join(format_exception(*err, limit=self.trace_limit)).replace("\n", "\n\t").rstrip()
        )
        message = f" failed after {attempt} attempts!\n\t"
        self.stream.writelines([f"\t{test_name}", message, formatted_trace, "\n\n"])

    def log_test_finalizer_failed(self, attempt: int, test_name: str, err: tuple) -> None:
        formatted_trace = (
            "".join(format_exception(*err, limit=self.trace_limit)).replace("\n", "\n\t").rstrip()
        )
        message = f" finalizer failed on attempt {attempt}! Exiting immediately!\n\t"
        self.stream.writelines([f"\t{test_name}", message, formatted_trace, "\n\n"])

    def add_retry_report(self, terminalreporter: TerminalReporter) -> None:
        contents = self.stream.getvalue()
        if not contents:
            return

        terminalreporter.write("\n")
        terminalreporter.section(
            "the following tests were retried", sep="=", bold=True, yellow=True
        )
        terminalreporter.write(contents)
        terminalreporter.section("end of test retry report", sep="=", bold=True, yellow=True)
        terminalreporter.write("\n")

    def record_node_stats(self, report: pytest.TestReport) -> None:
        self.node_stats[report.nodeid]["outcomes"][report.when].append(report.outcome)
        self.node_stats[report.nodeid]["durations"][report.when].append(report.duration)

    def simple_outcome(self, item: pytest.Item) -> str:
        """
        Return failed if setup, teardown, or final call outcome is 'failed'
        Return skipped if test was skipped
        """
        test_outcomes = self.node_stats[item.nodeid]["outcomes"]
        for outcome in ("skipped", "failed"):
            if outcome in test_outcomes["setup"]:
                return outcome
        if not test_outcomes["call"] or test_outcomes["call"][-1] == "failed":
            return "failed"
        if "failed" in test_outcomes["teardown"]:
            return "failed"
        return "passed"

    def simple_duration(self, item: pytest.Item) -> float:
        """
        Return total duration for test summing setup, teardown, and final call
        """
        return sum(self.node_stats[item.nodeid]["durations"][stage][-1] for stage in stages)

    def sum_attempts(self, item: pytest.Item) -> int:
        return len(self.node_stats[item.nodeid]["outcomes"]["call"])


retry_manager = RetryHandler()


def has_interactive_exception(call: pytest.CallInfo) -> bool:
    # Check whether the call raised an exception that should be reported as interactive.
    if call.excinfo is None:
        # Didn't raise.
        return False
    if isinstance(call.excinfo.value, bdb.BdbQuit):
        # Special control flow exception.
        return False
    return True


def should_handle_retry(rep: pytest.TestReport) -> bool:
    # if test passed, don't retry
    if rep.passed:
        return False
    # if teardown stage, don't retry
    if rep.when == "teardown":
        return False
    # if test was skipped, don't retry
    if rep.skipped:
        return False
    # if test is xfail, don't retry
    if hasattr(rep, "wasxfail"):
        return False
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Optional[object]:
    retry_manager.node_stats[item.nodeid] = {
        "outcomes": {k: [] for k in stages},
        "durations": {k: [0.0] for k in stages},
    }
    yield
    item.stash[outcome_key] = retry_manager.simple_outcome(item)
    item.stash[duration_key] = retry_manager.simple_duration(item)  # always overwrite, for now
    item.stash[attempts_key] = retry_manager.sum_attempts(item)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo
) -> Generator[None, pytest.TestReport, None]:
    outcome = yield
    original_report: pytest.TestReport = outcome.get_result()
    retry_manager.record_node_stats(original_report)
    # Set dynamic outcome for each stage until runtest protocol has completed.
    item.stash[outcome_key] = original_report.outcome
    if not should_handle_retry(original_report):
        return
    flake_mark = item.get_closest_marker("flaky")
    if flake_mark is None:
        return
    delay = flake_mark.kwargs.get("delay", 0)
    retries = flake_mark.kwargs.get("retries", 1)
    timing = flake_mark.kwargs.get("timing", "overwrite")
    if timing not in ("overwrite", "cumulative"):
        raise ValueError(f"Unknown timing type: {timing}! Must be `cumulative` or `overwrite`.")
    attempts = 1
    hook = item.ihook

    while True:
        # Parse error info from original run
        exc_info = (call.excinfo.type, call.excinfo.value, call.excinfo.tb)  # type: ignore
        if call.when == "setup":
            break  # will handle fixture setup retries in v2, if necessary. For now, this is fine.
        # Default teardowns are already excluded, so this must be the `call` stage
        # Try preliminary teardown using a fake item to ensure every local fixture (i.e.
        # excluding session) is torn down. Yes, including module and class fixtures
        t_call = pytest.CallInfo.from_call(
            lambda: hook.pytest_runtest_teardown(
                item=item,
                nextitem=pytest.Item.from_parent(item.session, name="fakeboi"),
            ),
            when="teardown",
        )
        # If teardown fails, break. Flaky teardowns are not acceptable and should raise immediately
        if t_call.excinfo:
            item.stash[outcome_key] = "failed"
            t_exc_info = (t_call.excinfo.type, t_call.excinfo.value, t_call.excinfo.tb)
            retry_manager.log_test_finalizer_failed(
                attempt=attempts,
                test_name=item.name,
                err=t_exc_info,
            )
            # Prevents a KeyError when an error during retry teardown causes a redundant teardown
            item.stash[caplog_records_key] = {}  # type: ignore
            break

        # If teardown passes, send report that the test is being retried
        if attempts == 1:
            original_report.outcome = "retried"  # type: ignore
            hook.pytest_runtest_logreport(report=original_report)
            original_report.outcome = "failed"
        retry_manager.log_test_retry(attempt=attempts, test_name=item.name, err=exc_info)
        sleep(delay)
        # Call _initrequest(). Only way to get the setup to run again
        item._initrequest()  # type: ignore[attr-defined]

        pytest.CallInfo.from_call(lambda: hook.pytest_runtest_setup(item=item), when="setup")
        call = pytest.CallInfo.from_call(lambda: hook.pytest_runtest_call(item=item), when="call")
        retry_report = pytest.TestReport.from_item_and_call(item, call)
        retry_manager.record_node_stats(retry_report)
        # Do the exception interaction step
        # (may not bother to support this since this is designed for automated runs, not debugging)
        if has_interactive_exception(call):
            hook.pytest_exception_interact(node=item, call=call, report=retry_report)

        attempts += 1
        should_keep_retrying = not retry_report.passed and attempts <= retries

        if not should_keep_retrying:
            original_report.outcome = retry_report.outcome
            original_report.longrepr = retry_report.longrepr
            if timing == "overwrite":
                original_report.duration = retry_report.duration
            else:
                original_report.duration += retry_report.duration

            if retry_report.failed:
                exc_info = (call.excinfo.type, call.excinfo.value, call.excinfo.tb)  # type: ignore
                retry_manager.log_test_totally_failed(
                    attempt=attempts, test_name=item.name, err=exc_info
                )
            break


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    retry_manager.add_retry_report(terminalreporter)


def pytest_report_teststatus(
    report: pytest.TestReport,
) -> Optional[tuple[str, str, tuple[str, dict]]]:
    if report.outcome == "retried":
        return "retried", "R", ("RETRY", {"yellow": True})
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "flaky(retries=1, delay=0): indicate a flaky test which"
        "will be retried the number of times specified with an"
        "(optional) specified delay between each attempt",
    )
    if config.getoption("verbose"):
        # if pytest config has -v enabled, then don't limit traceback length
        retry_manager.trace_limit = None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup(
        "pytest-retry", "retry flaky tests to compensate for intermittent failures"
    )
    group.addoption(
        "--retries",
        action="store",
        dest="retries",
        type=int,
        default=0,
        help="number of times to retry failed tests. Defaults to 0.",
    )
    group.addoption(
        "--retry-delay",
        action="store",
        dest="retry_delay",
        type=float,
        default=0,
        help="add a delay (in seconds) between retries.",
    )
    group.addoption(
        "--cumulative-timing",
        action="store",
        dest="cumulative_timing",
        type=bool,
        default=False,
        help="if True, retry duration will be included in overall reported test duration",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--retries"):
        return
    retry_delay = config.getoption("--retry-delay") or 0
    timing = "cumulative" if config.getoption("--cumulative-timing") else "overwrite"
    flaky = pytest.mark.flaky(retries=config.option.retries, delay=retry_delay, timing=timing)
    for item in items:
        if "flaky" not in item.keywords:
            item.add_marker(flaky)
