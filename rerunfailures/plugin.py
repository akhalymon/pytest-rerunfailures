from _pytest.terminal import TerminalReporter
import py, pytest

from _pytest.runner import runtestprotocol

# Add command line options
def pytest_addoption(parser):
    group = parser.getgroup("rerunfailures", "re-run failing tests to eliminate flaky failures")
    group._addoption('--reruns',
                     action="store",
                     dest="reruns",
                     type="int",
                     default=0,
                     help="number of times to re-run failed tests. defaults to 0.")

    group._addoption('--timelimit',
                     action="store",
                     dest="timelimit",
                     type="int",
                     default=0,
                     help="if test failed after timelimit, it will be not rerunned. Defaults to 7200 (2hrs)")
    group._addoption('--rerun_time_threshold',
                     action="store",
                     dest="rerun_time_threshold",
                     type="int",
                     default=7200,
                     help="Allowed  time in seconds to spend on tests reruning. If total rerun time is  "
                          "more then threshold, then rerun is skipped")
    group._addoption('--skip_tests',
                     action="store",
                     dest="skip_tests",
                     default="",
                     help="Comma-separated list of tests that should be explicitly skipped. If test is parametrized")
    group._addoption('--rerun_after',
                     action="count",
                     dest="rerun_after",
                     default=0,
                     help="Rerun tests after whole test suite finishes")


@pytest.mark.trylast
def pytest_configure(config):
    if hasattr(config, 'slaveinput'):
        return  # xdist slave, we are already active on the master
    if config.option.reruns:
        # Get the standard terminal reporter plugin...
        standard_reporter = config.pluginmanager.getplugin('terminalreporter')
        reruninfo_reporter = RerunInfoTerminalReporter(standard_reporter)

        # ...and replace it with our own rerun info reporter.
        config.pluginmanager.unregister(standard_reporter)
        config.pluginmanager.register(reruninfo_reporter, 'terminalreporter')
    else:
        # If no rerun option, completely unload plugin
        rerun_plugin = config.pluginmanager.getplugin('rerunfailures')
        config.pluginmanager.unregister(rerun_plugin)


# making sure the options make sense
# should run before / at the begining of pytest_cmdline_main
def check_options(config):
    # TODO add assertations for other options
    val = config.getvalue
    if not val("collectonly"):
        if config.option.reruns != 0:
            if config.option.usepdb:  # a core option
                raise pytest.UsageError("--reruns incompatible with --pdb")


def pytest_sessionstart(session):
    # Initialising rerun time profiler
    session.ordinary_tests_durations = 0
    session.rerun_tests_durations = 0


# This mark means hook will be called before default hook
# (for reporting to use reduced number of tests, without re-started ones)
@pytest.mark.tryfirst
def pytest_sessionfinish(session, exitstatus):
    # Removing duplicate items, leaving only the very last instance of each test
    items = session.items
    for item in items:
        while items.count(item) > 1:
            items.remove(item)

# Init all elements to have attempt field
def pytest_collection_modifyitems(session, config, items):
    """ called after collection has been performed, may filter or re-order
    the items in-place."""
    for item in items:
        item.attempt = 1


def pytest_runtest_protocol(item, nextitem):
    """
    Note: when teardown fails, two reports are generated for the case, one for the test
    case and the other for the teardown error.
    """

    check_options(item.session.config)

    item.ihook.pytest_runtest_logstart(
        nodeid=item.nodeid, location=item.location,
    )
    # If rerun after is enabled, we should skip already scheduled reruns (that was scheduled before threshold reached)
    if  item.attempt > 1 and item.config.option.rerun_after and item.session.rerun_tests_durations > item.config.option.rerun_time_threshold:
        reason = "total rerun threshold reached"
        print "rerun skipped, reason: " + reason + " testcase: " + item.nodeid
        # Do not touch item report status here
        # Just decrease attempt count (was increased while scheduling test to rerun
        item.attempt -= 1
    else:
        # Do test execution and assign report status
        item.reports = runtestprotocol(item, nextitem=nextitem, log=False)
    # Update cumulative test durations
    update_test_durations(item.reports, item.session, item.attempt)
    # Get test status (aware of rerun)
    test_succeed, status_message = report_test_status(item, item.reports)

    if item.config.option.verbose:
        print item.nodeid, " attepmt " + str(item.attempt)

    qualify_rerun = False
    if test_succeed:
        pass
    else:
        # Check rerun conditions
        qualify, reason = qualify_for_rerun(item, item.reports)
        if not (qualify):
            print "rerun skipped, reason: " + reason + " testcase: " + item.nodeid
        else:
            # Schedule item to be executed somewhere in future
            schedule_item_rerun(item, item.config)
            qualify_rerun = True

    # Update report attempt field (to report these values)
    for report in item.reports:
        # Only update for "call" (not setup and teardown)
        if report.when in ("call"):
            report.attempt = item.attempt
        # If test is scheduled for rerun, results are not final, so we don't generate report
        if not qualify_rerun:
            item.ihook.pytest_runtest_logreport(report=report)
        # For debug puproses
        verbose_output(item)

    # pytest_runtest_protocol returns True
    return True


def verbose_output(item):
    if item.config.option.verbose:
        # For debug purposes
        print "\n    time spent on runs: ", item.session.ordinary_tests_durations
        print "    time spent on reruns: \n", item.session.rerun_tests_durations

# Get test execution results
def report_test_status(item, reports):
    is_rerun = item.attempt > 1
    status_message = []
    test_succeed = reports[0].passed and reports[1].passed

    if test_succeed and not is_rerun:
        status_message.append("PASS: " + item.nodeid)
    if test_succeed and is_rerun:
        status_message.append("PASS_ON_RERUN: " + item.nodeid)
    if not test_succeed and not is_rerun:
        status_message.append("FAIL: " + item.nodeid)
    if not test_succeed and is_rerun:
        status_message.append("FAIL_ON_RERUN: " + item.nodeid)
    return test_succeed, "".join(status_message)

# Depending on option, schedule rerun just after this item, or at the run end
def schedule_item_rerun(item, config):
    item.attempt += 1
    if config.option.rerun_after:
        item.session.items.append(item)
    else:
        item.session.items.insert(item.session.items.index(item) + 1, item)

# Decide if test is qulified for rerun
def qualify_for_rerun(item, reports):
    reason = []
    # Check if test is in [skipped tests]
    if item.config.option.skip_tests.split:
        tests_to_skip = [testname.strip() for testname in item.config.option.skip_tests.split(',')]
        for testname in tests_to_skip:
            if testname in item.location:
                reason.append("rerun explicitly disabled for this test case")
                return False, "".join(reason)

    # Check if there attempts for rerun left
    if item.attempt > item.config.option.reruns + 1:
        reason.append("failure rerun attempt limit reached ")
        return False, "".join(reason)


    # If test duration exceeds time limit, skip
    if get_test_duration(reports) > item.config.option.timelimit:
        reason.append("test exceeds timelimit")
        return False, "".join(reason)

    # TODO this limitation will not work correctly with --rerun_after option
    # If overall rerun time exceeds threshold, skip
    if item.session.rerun_tests_durations + get_test_duration(reports) > item.config.option.rerun_time_threshold:
        reason.append("total rerun threshold reached")
        return False, "".join(reason)

    # If qualify, reason is empty
    return True, "".join(reason)


def update_test_durations(reports, session, attempt):
    current_test_duration = get_test_duration(reports)
    # If this is not a first try, add duration to reruns time, else to runs time
    if attempt > 1:
        session.rerun_tests_durations += current_test_duration
    else:
        session.ordinary_tests_durations += current_test_duration


def get_test_duration(reports):
    current_test_duration = 0
    # reports is a list of stuff, executed for an item (setup, call, teardown)
    # We count cumulative duration of it
    for j in range(len(reports)):
        current_test_duration += reports[j].duration
    return current_test_duration


def pytest_report_teststatus(report):
    """ adapted from
    https://bitbucket.org/hpk42/pytest/src/a5e7a5fa3c7e/_pytest/skipping.py#cl-170
    """
    if report.when in ("call"):
        if report.attempt > 1:
            if report.outcome == "failed":
                return "rerun failed", "e", "FAILED"
            if report.outcome == "passed":
                return "rerun passed", "R", "PASSED_ON_RERUN"

# Adopted from https://github.com/jpvanhal/pytest-instafail/blob/master/pytest_instafail.py
# With slight changes
class RerunInfoTerminalReporter(TerminalReporter):
    def __init__(self, reporter):
        TerminalReporter.__init__(self, reporter.config)
        self._tw = reporter._tw

    def summary_stats(self):
        session_duration = py.std.time.time() - self._sessionstarttime

        keys = "failed passed skipped deselected xfailed xpassed".split()

        for key in self.stats.keys():
            if key not in keys:
                keys.append(key)
        parts = []
        for key in keys:
            if key:  # setup/teardown reports have an empty key, ignore them
                val = self.stats.get(key, None)
                if val:
                    parts.append("%d %s" % (len(val), key))
        line = ", ".join(parts)
        msg = "%s in %.2f seconds" % (line, session_duration)

        markup = {'bold': True}
        # Modification here: if all tests were failed on rerun, make terminal red
        if 'failed' in self.stats or 'error' in self.stats or 'rerun failed':
            markup = {'red': True, 'bold': True}
        else:
            markup = {'green': True, 'bold': True}

        if self.verbosity >= 0:
            self.write_sep("=", msg, **markup)
        if self.verbosity == -1:
            self.write_line(msg, **markup)

    def pytest_sessionfinish(self, exitstatus, __multicall__):
        __multicall__.execute()
        self._tw.line("")
        if exitstatus in (0, 1, 2, 4):
            self.summary_errors()
            self.summary_failures()
            # Added summary for reruns
            self.summary_rerun_failed()
            self.summary_rerun_passed()
            self.summary_hints()
            self.config.hook.pytest_terminal_summary(terminalreporter=self)
        if exitstatus == 2:
            self._report_keyboardinterrupt()
            del self._keyboardinterrupt_memo
        self.summary_deselected()
        self.summary_stats()

    def summary_rerun_passed(self):
        if self.config.option.tbstyle != "no":
            reports = self.getreports('rerun passed')
            if not reports:
                return
            self.write_sep("=", "PASSED ON RERUN")
            for rep in reports:
                line = rep.nodeid + " duration: " + "%.2f" % rep.duration
                if hasattr(rep, "attempt"):
                    line = line + " attempt: " + str(rep.attempt)
                self.write_line(line)

    def summary_rerun_failed(self):
        if self.config.option.tbstyle != "no":
            reports = self.getreports('rerun failed')
            if not reports:
                return
            self.write_sep("=", "FAILED ON RERUN")
            for rep in reports:
                if self.config.option.tbstyle == "line":
                    line = self._getcrashline(rep)
                    self.write_line(line)
                else:
                    msg = self._getfailureheadline(rep)
                    self.write_sep("_", msg)
                    self._outrep_summary(rep)