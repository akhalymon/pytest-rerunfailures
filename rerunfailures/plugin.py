import sys, time
import py, pytest

from _pytest.runner import runtestprotocol

# command line options
def pytest_addoption(parser):
    group = parser.getgroup("rerunfailures", "re-run failing tests to eliminate flakey failures")
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


def pytest_configure(config):
    #Add flaky marker
    config.addinivalue_line("markers", "flaky(reruns=1): mark test to re-run up to 'reruns' times")

# making sure the options make sense
# should run before / at the begining of pytest_cmdline_main
def check_options(config):
    val = config.getvalue
    if not val("collectonly"):
        if config.option.reruns != 0:
            if config.option.usepdb:   # a core option
                raise pytest.UsageError("--reruns incompatible with --pdb")


def pytest_runtest_protocol(item, nextitem):
    """
    Note: when teardown fails, two reports are generated for the case, one for the test
    case and the other for the teardown error.

    Note: in some versions of py.test, when setup fails on a test that has been marked with xfail, 
    it gets an XPASS rather than an XFAIL 
    (https://bitbucket.org/hpk42/pytest/issue/160/an-exception-thrown-in)
    fix should be released in version 2.2.5
    """

    if not hasattr(item.session, 'ordinary_tests_durations'):
        item.session.ordinary_tests_durations=0
    if not hasattr(item.session, 'rerun_tests_durations'):
        item.session.rerun_tests_durations=0
    if not hasattr(item, 'get_marker'):
        # pytest < 2.4.2 doesn't support get_marker
        rerun_marker = None
        val = item.keywords.get("flaky", None)
        if val is not None:
            from _pytest.mark import MarkInfo, MarkDecorator
            if isinstance(val, (MarkDecorator, MarkInfo)):
                rerun_marker = val
    else:
        #In pytest 2.4.2, we can do this pretty easily.
        rerun_marker = item.get_marker("flaky")

    #Use the marker as a priority over the global setting.
    if rerun_marker is not None:
        if "reruns" in rerun_marker.kwargs:
            #Check for keyword arguments
            reruns = rerun_marker.kwargs["reruns"]
        elif len(rerun_marker.args) > 0:
            #Check for arguments
            reruns = rerun_marker.args[0]
    elif item.session.config.option.reruns is not None:
        #Default to the global setting
        reruns = item.session.config.option.reruns
    else:
        #Global setting is not specified, and this test is not marked with flaky
        return
    
    # while this doesn't need to be run with every item, it will fail on the first 
    # item if necessary
    check_options(item.session.config)

    item.ihook.pytest_runtest_logstart(
        nodeid=item.nodeid, location=item.location,
    )

    for attempt in range(reruns+1):  # ensure at least one run of each item
        # Execute the very test
        reports = runtestprotocol(item, nextitem=nextitem, log=False)
        update_test_durations(reports, item.session, attempt)
        test_succeed, status_message = report_test_status(item, reports, attempt)
        print status_message
        if test_succeed:
            break
        else:
            qualify, reason = qualify_for_rerun(item, reports, attempt)
            if not(qualify):
                print "rerun skipped, reason: "+reason+" testcase: " + item.nodeid
                break

        # break if test marked xfail
        evalxfail = getattr(item, '_evalxfail', None)
        if evalxfail:
            break

    for report in reports:
        if report.when in ("call"):
            if attempt > 0:
                report.rerun = attempt
        item.ihook.pytest_runtest_logreport(report=report)

        verbose_output(item)


    # pytest_runtest_protocol returns True
    return True

def verbose_output(item):
    if item.config.getoption("--verbose"):
        # For debug purposes
        print "\n    time spent on runs: ", item.session.ordinary_tests_durations
        print "    time spent on reruns: \n", item.session.rerun_tests_durations


def report_test_status(item, reports, attempt):
    is_rerun = attempt > 0
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


def qualify_for_rerun(item, reports, attempt):
    result = True
    reason = []
    tests_to_skip = [testname.strip() for testname in item.config.getoption("--skip_tests").split(',')]
    for testname in tests_to_skip:
        if testname in item.location:
            result = False
            reason.append("rerun explicitly disabled for this test case")
            return result, "".join(reason)

    if attempt > item.config.getoption("--reruns"):
        result = False
        reason.append("failure rerun attempt limit reached ")
        return result, "".join(reason)


    # If test duration exceeds time limit, skip
    if get_test_duration(reports) > item.config.getoption("--timelimit"):
        result = False
        reason.append("test exceeds timelimit")
        return result, "".join(reason)

    # If overall rerun time exceeds threshold, skip
    if item.session.rerun_tests_durations+get_test_duration(reports) > item.config.getoption("--rerun_time_threshold"):
        result = False
        reason.append("total rerun threshold reached")
        return result, "".join(reason)

    return result, "".join(reason)

def update_test_durations(reports, session, attempt):
    current_test_duration = get_test_duration(reports)
    # If this is not a first try, add duration to reruns time, else to runs time
    if attempt>1:
        session.rerun_tests_durations += current_test_duration
    else:
        session.ordinary_tests_durations += current_test_duration

def get_test_duration(reports):
    current_test_duration = 0
    for j in range(len(reports)):
         current_test_duration+= reports[j].duration
    return current_test_duration



def pytest_report_teststatus(report):
    """ adapted from
    https://bitbucket.org/hpk42/pytest/src/a5e7a5fa3c7e/_pytest/skipping.py#cl-170
    """
    if report.when in ("call"):
        if hasattr(report, "rerun") and report.rerun > 0:
            if report.outcome == "failed":
                return "rerun failed", "e", "FAILED"
            if report.outcome == "passed":
                return "rerun passed", "R", "PASSED_ON_RERUN"


def pytest_terminal_summary(terminalreporter):
    """ adapted from
    https://bitbucket.org/hpk42/pytest/src/a5e7a5fa3c7e/_pytest/skipping.py#cl-179
    """
    tr = terminalreporter
    if not tr.reportchars:
        return

    lines = []

    tr._tw.sep("=", "PASSED ON RERUN")
    show_simple(terminalreporter, lines, 'rerun passed', "RERUN_PASSED %s")
    if lines:
        for line in lines:
            tr._tw.line(line)

    lines = []
    tr._tw.sep("=", "FAILED ON RERUN")
    show_simple(terminalreporter, lines, 'rerun failed', "RERUN_FAILED %s")
    if lines:
        for line in lines:
            tr._tw.line(line)


def show_rerun(terminalreporter, lines):
    rerun = terminalreporter.stats.get("rerun")
    if rerun:
        for rep in rerun:
            pos = rep.nodeid
            lines.append("RERUN %s" % (pos,))


def show_simple(terminalreporter, lines, stat, format):
    failed = terminalreporter.stats.get(stat)
    if failed:
        for rep in failed:
            pos = rep.nodeid + " " + "%.2f" % rep.duration
            lines.append(format %(pos, ))
