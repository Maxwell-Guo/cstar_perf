#!/usr/bin/env python2

from __future__ import unicode_literals

import time
import sys
import argparse
import requests
import json
import logging
import traceback
from Queue import Queue
from threading import Thread
#from cstar_perf.frontend.server.email_notifications import RegressionTestEmail

logging.basicConfig()
logger = logging.getLogger('cstar_perf_regression_monitor')
logger.setLevel(logging.INFO)

DEFAULT_WINDOW_TIME = 90  # days
DEFAULT_NUMBER_LAST_RUNS = 5  # the last 2 runs


class CstarPerfClient(object):
    """cstar_perf api rest client"""

    server = "cstar.datastax.com"

    urls = {
        'get_series_list': '/api/series',
        'get_series': '/api/series/{name}/{start_timestamp}/{stop_timestamp}',
        'get_test_summary': '/tests/artifacts/{job_id}/stats_summary/stats_summary.{job_id}.json'
    }

    def __init__(self, server=None):
        if server:
            self.server = server

    def build_url(self, url, **kwargs):
        url = self.urls[url].format(**kwargs)
        return "http://{}{}".format(self.server, url)

    def get(self, url):
        r = requests.get(url)
        if r.status_code != 200:
            raise Exception('Error while doing request: {}'.format(url))

        return r.text


class CstarTestJob(CstarPerfClient):

    job_id = None

    # metrics list we are interested in
    # '95th_percentile', '999th_percentile', '99th_percentile','latency_max', 'latency_mean', 'latency_median'
    metrics_list = ['op rate']

    # job metrics per operation
    metrics = None

    def __init__(self, job_id, **kwargs):
        super(CstarTestJob, self).__init__(**kwargs)
        self.job_id = job_id
        self.metrics = {}

    def __repr__(self):
        return self.__str__()

    def __unicode__(self):
        return self.__str__()


    def __str__(self):
        return "<CstarTestJob({})>".format(self.job_id)

    def __eq__(self, other):
        return self.job_id == other.job_id

    def _read_job_metrics(self, data):
        """Read the job metrics"""

        for operation in data['stats']:
            operation_name = operation.get('test', operation['id'])
            self.metrics[operation_name] = {}
            s = self.metrics[operation_name]
            for m in self.metrics_list:
                s[m] = float(operation['op rate'].split(' ')[0])

    def get_operations(self):
        return self.metrics.keys()

    def fetch(self):
        url = self.build_url('get_test_summary', job_id=self.job_id)
        logger.debug("Fetching job {} from: {}".format(self.job_id, url))
        # the summary file is small, so we currently keep it in memory
        response = self.get(url)
        data = json.loads(response)
        self._read_job_metrics(data)


class RegressionSeries(CstarPerfClient):
    """Represent a regression serie"""

    # Serie name
    name = None

    # window time
    start_timestamp = None
    stop_timestamp = None

    # Job ids of the serie
    jobs = None

    # job metrics
    metrics = None

    # Current performance and Historical Performance used to detect regressions
    current_performance = None
    historical_performance = None

    def __init__(self, name, start_timestamp, stop_timestamp, **kwargs):
        super(RegressionSeries, self).__init__(**kwargs)

        self.name = name
        self.start_timestamp = start_timestamp
        self.stop_timestamp = stop_timestamp
        self.jobs = []

        self.metrics = {}  # metrics per operation

    def __repr__(self):
        return self.__str__()

    def __unicode__(self):
        return self.__str__()

    def __str__(self):
        return "{}: {} tests)".format(
            self.name, len(self.job))

    def fetch(self):
        url = self.build_url('get_series', name=self.name,
                             start_timestamp=self.start_timestamp,
                             stop_timestamp=self.stop_timestamp)
        logger.debug("Fetching job ids of series '{}' from: {}".format(self.name, url))
        response = self.get(url)

        self.jobs = [CstarTestJob(s, server=self.server) for s in json.loads(response)['series']]
        for job in self.jobs:
            job.fetch()

    def _compute_performance(self, jobs):
        """Compute the average performance of a subset of jobs

        It throws out the fastest and slowest jobs.
        """

        if not len(jobs):
            return {}

        average_performance = {}
        operations = jobs[0].get_operations()

        for operation in operations:
            average_performance[operation] = {}
            jobs_tmp = list(jobs)
            # Remove the fastest and slowest job
            if len(jobs_tmp) > 2:
                fastest_job = max(jobs_tmp, key=lambda j: j.metrics[operation]['op rate'])
                slowest_job = min(jobs_tmp, key=lambda j: j.metrics[operation]['op rate'])
                jobs_tmp.remove(fastest_job)
                jobs_tmp.remove(slowest_job)

            op_rates = map(lambda j: j.metrics[operation]['op rate'], jobs_tmp)
            average_op_rate = sum(op_rates) / len(jobs_tmp)
            average_performance[operation]['op rate'] = average_op_rate

        return average_performance

    def _compute_current_performance(self):
        """Compute the current performance per operation using the last N runs"""

        jobs = self.jobs[-DEFAULT_NUMBER_LAST_RUNS:]
        return self._compute_performance(jobs)

    def do_regression_check(self):
        self.fetch()

        self.current_performance = self._compute_current_performance()
        self.historical_performance = self._compute_performance(self.jobs)


class RegressionMonitor(CstarPerfClient):
    """cstar_perf regression monitor tool"""

    tolerance = 10  # performance deviation tolerance in %

    concurrency = 1

    def __init__(self, start_timestamp, stop_timestamp, **kwargs):
        super(RegressionMonitor, self).__init__(**kwargs)

        self.start_timestamp = start_timestamp
        self.stop_timestamp = stop_timestamp

    def _get_series(self):
        url = self.build_url('get_series_list')
        logger.debug("Fetching series from: {}".format(url))
        response = self.get(url)

        series = json.loads(response)
        return [RegressionSeries(s, self.start_timestamp, self.stop_timestamp,
                                 server=self.server) for s in series]

    def run(self):
        series = self._get_series()

        q = Queue()
        for s in series:
            q.put(s)

        def worker():
            while True:
                serie = q.get()

                try:
                    serie.do_regression_check()
                except Exception as e:
                    logger.error(e)
                    logger.error(traceback.print_exc())

                q.task_done()

        for i in range(self.concurrency):
            t = Thread(target=worker)
            t.daemon = True
            t.start()

        q.join()

        for serie in series:
            for operation_name, metrics in serie.current_performance.iteritems():
                op_rate = metrics['op rate']
                average_rate = serie.historical_performance[operation_name]['op rate']

                has_regression = False
                if op_rate < average_rate and abs(op_rate - average_rate) > (average_rate * self.tolerance):
                    has_regression = True
                    #RegressionTestEmail(name=serie.name, current_performance=op_rate,
                    #                    historical_performance=average_op).send()

                logger.info(("Regression check for series '{}' operation '{}': "
                             "historical({}) - current({}) -- {}").format(
                                 serie.name, operation_name, int(average_rate), int(op_rate),
                                 'REGRESSION DETECTED' if has_regression else 'OK'))


def main(args):

    start_timestamp = args.start_timestamp
    stop_timestamp = args.stop_timestamp

    if not start_timestamp and not stop_timestamp:
        time_delta = DEFAULT_WINDOW_TIME * 24 * 60 * 60
        stop_timestamp = int(time.time())
        start_timestamp = stop_timestamp - time_delta

    monitor = RegressionMonitor(
        start_timestamp,
        stop_timestamp,
        server=args.server
    )

    if args.command == 'run':
        monitor.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='cstar_cstar_perf_regression_monitor.py - '
                                     'Monitor performance regression',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser_subparsers = parser.add_subparsers(dest='command')

    run = parser_subparsers.add_parser('run', description="Run the regression monitoring process")
    run.add_argument('-s', '--server', required=False, help='The hostname of the server')
    run.add_argument('--start-timestamp', default=None, help='The start timestamp')
    run.add_argument('--stop-timestamp', default=None, help='The stop timestamp')

    try:
        args = parser.parse_args()
    finally:
        # Print verbose help if they didn't give any command:
        if len(sys.argv) == 1:
            parser.print_help()

    main(args)
