"""
Agent for reporting virtual guest IDs to subscription-manager

Copyright (C) 2011 Radek Novacek <rnovacek@redhat.com>

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import sys
import os
import signal
import errno
import time
from multiprocessing import Event, Queue
import json

import atexit
from Queue import Empty
from httplib import BadStatusLine

try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6 doesn't have OrderedDict, we need to have our own
    from util import OrderedDict

from daemon import daemon
from virt import Virt, DomainListReport, HostGuestAssociationReport, ErrorReport
from manager import Manager, ManagerError, ManagerFatalError
from manager.subscriptionmanager import SubscriptionManager
from config import Config, ConfigManager, InvalidPasswordFormat
from password import InvalidKeyFile

import log
from log import QueueLogger, QueueHandler
import logging

from optparse import OptionParser, OptionGroup, SUPPRESS_HELP


class Job(object):
    """
    This class represents a job to be run
    Parameters:
        'target': this is the method to be executed with 'args' arguments
        'args': OPTIONAL the arguments list to be passed to 'target'
    """
    def __init__(self,
                 target,
                 args=None):
        self.target = target

        if args is None:
            self.args = []
        else:
            self.args = args

class ReloadRequest(Exception):
    ''' Reload of virt-who was requested by sending SIGHUP signal. '''


class OptionParserEpilog(OptionParser):
    """ Epilog is new in Python 2.5, we need to support Python 2.4. """
    def __init__(self, usage="%prog [options]", description=None, epilog=None):
        self.myepilog = epilog
        OptionParser.__init__(self, usage=usage, description=description)

    def format_help(self, formatter=None):
        if formatter is None:
            formatter = self.formatter
        help = OptionParser.format_help(self, formatter)
        return help + "\n" + self.format_myepilog(formatter) + "\n"

    def format_myepilog(self, formatter=None):
        if self.myepilog is not None:
            return formatter.format_description(self.myepilog)
        else:
            return ""

# Change detection will limit the sending if no changes exist
RetryInterval = 60  # One minute
# Default interval for sending list of UUIDs
DefaultInterval = 3600  # Once per hour
MinimumSendInterval = 600  # Ten minutes
# How many times it should try to report association to the server
RetrySendCount = 5

PIDFILE = "/var/run/virt-who.pid"
SAT5 = "satellite"
SAT6 = "sam"


class VirtWho(object):
    def __init__(self, logger, options, config_dir=None):
        """
        VirtWho class provides bridge between virtualization supervisor and
        Subscription Manager.

        logger - logger instance
        options - options for virt-who, parsed from command line arguments
        """
        self.logger = logger
        self.options = options
        self.terminate_event = Event()

        # Queue for getting events from virt backends
        self.queue = None
        # How long should we wait for new item in queue, it depends on
        # jobs we have and how long we have them
        self.queue_timeout = None
        # a heap to manage the jobs we have incoming
        self.jobs = []
        # A dictionary of reports previously sent
        self.reports = {}
        self.reloading = False

        self.configManager = ConfigManager(config_dir)
        for config in self.configManager.configs:
            logger.debug("Using config named '%s'" % config.name)

        self.unableToRecoverStr = "Unable to recover"
        if not options.oneshot:
            self.unableToRecoverStr += ", retry in %d seconds." % RetryInterval

        self.queue_logger = log.getDefaultQueueLogger()

    def addJob(self, job):
        # Add a job to be executed next time we have a report to send
        if (not isinstance(job, Job)):
            job = Job(*job)
        self.jobs.append(job)

    def runJobs(self):
        if not self.jobs:
            return
        # Run only those jobs added before this method was called
        # This prevents any issues with jobs that result in the creation of
        # another job
        jobsToRun = self.jobs
        self.jobs = []
        for job in jobsToRun:
            if hasattr(self, job.target):
                self.logger.debug('Running method "%s"' % job.target)
                try:
                    getattr(self, job.target)(*job.args)
                except Exception:
                    self.logger.exception("Job failed:")
            else:
                self.logger.debug('VirtWho has no method "%s"' % job.target)

    def send(self, report):
        """
        Send list of uuids to subscription manager

        return - True if sending is successful, False otherwise
        """
        try:
            if isinstance(report, DomainListReport):
                self._sendGuestList(report)
            elif isinstance(report, HostGuestAssociationReport):
                self._sendGuestAssociation(report)
            else:
                self.logger.warn("Unable to handle report of type: %s", type(report))
        except ManagerError as e:
            self.logger.error("Unable to send data: %s" % str(e))
            return False
        except ManagerFatalError as e:
            # Something really bad happened (system is not register), stop the backends
            self.logger.exception("Error in communication with subscription manager:")
            raise
        except Exception as e:
            if self.reloading:
                # We want to skip error reporting when reloading,
                # it is caused by interrupted syscall
                self.logger.debug("Communication with subscription manager interrupted")
                return False
            exceptionCheck(e)
            self.logger.exception("Error in communication with subscription manager:")
            return False
        return True

    def _sendGuestList(self, report):
        manager = Manager.fromOptions(self.logger, self.options)
        manager.sendVirtGuests(report.guests)
        self.logger.info("virt-who guest list update successful")
        self.reports[report.config.hash] = report.hash

    def _sendGuestAssociation(self, report):
        manager = Manager.fromOptions(self.logger, self.options)
        manager.addJob = self.addJob
        result = manager.hypervisorCheckIn(report.config,
                                           report.association,
                                           report.config.type)
        self.reports[report.config.hash] = report.hash

    def checkJobStatus(self, config, job_id):
        manager = SubscriptionManager(self.logger, self.options, self.addJob)
        result = manager.checkJobStatus(config, job_id)

        if 'failedUpdate' in result and not result['failedUpdate']:
            self.logger.info("virt-who host/guest association update successful")

    def run(self):
        self.reloading = False
        if not self.options.oneshot:
            self.logger.debug("Starting infinite loop with %d seconds interval" % self.options.interval)

        # Queue for getting events from virt backends
        if self.queue is None:
            self.queue = Queue(len(self.configManager.configs))

        # Run the virtualization backends
        self.virts = []
        for config in self.configManager.configs:
            try:
                logger = log.getLogger(self.options, config)
                virt = Virt.fromConfig(logger, config)
            except Exception as e:
                self.logger.error('Unable to use configuration "%s": %s' % (config.name, str(e)))
                continue
            # Run the process
            virt.start(self.queue, self.terminate_event, self.options.interval, self.options.oneshot)
            self.virts.append(virt)
        if self.options.oneshot:
            oneshot_remaining = set(virt.config.name for virt in self.virts)

        if len(self.virts) == 0:
            self.logger.error("No suitable virt backend found")
            return

        result = {}
        report = None
        while not self.terminate_event.is_set():
            # Wait for incoming report from virt backend
            try:
                report = self.queue.get(block=True, timeout=self.queue_timeout)
            except Empty:
                report = None
                pass
            except IOError:
                continue

            try:
                # Run all jobs that have been queued as a result of sending last
                # time
                self.runJobs()
                if self.jobs:
                    # We have jobs, check them after some timeout,
                    # increase the timeout each time
                    self.queue_timeout *= 2
                else:
                    # Wait indefinetly long for new item in the queue
                    self.queue_timeout = None

                if self.options.oneshot and not oneshot_remaining and not self.jobs:
                    break
            except Empty:
                pass
            except IOError:
                pass

            if report is not None:
                # We got new item in the queue, reset timeout for checking job status
                self.queue_timeout = 1
                if report == "exit":
                    break
                if report == "reload":
                    for virt in self.virts:
                        virt.terminate()
                    self.virts = []
                    raise ReloadRequest()

                # Send the report
                if not self.options.print_ and not isinstance(report, ErrorReport):
                    for i in range(RetrySendCount):
                        try:
                            # Do not send if the report hash is already in the
                            # list of reports sent
                            if hasattr(report, 'hash') and report.hash == self.reports.get(report.config.hash):
                                self.logger.info('No change in report gathered using config: "%s", report not sent.', report.config.name)
                                break
                            if self.send(report):
                                break
                        except ManagerFatalError:
                            # System not register (probably), stop the backends
                            for virt in self.virts:
                                virt.terminate()
                            self.virts = []
                            break
                    else:
                        self.logger.error("Sending data failed %d times, report skipped" % RetrySendCount)

                if self.options.oneshot:
                    try:
                        oneshot_remaining.remove(report.config.name)
                        if not isinstance(report, ErrorReport):
                            if self.options.print_:
                                result[report.config] = report
                            self.logger.debug("Association for config %s gathered" % report.config.name)
                        for virt in self.virts:
                            if virt.config.name == report.config.name:
                                virt.stop()
                    except KeyError:
                        self.logger.debug("Association for config %s already gathered, ignoring" % report.config.name)

                    if not oneshot_remaining and not self.jobs:
                        break

        self.queue = None
        self.jobs = None
        for virt in self.virts:
            virt.terminate()

        self.virt = []
        if self.options.print_:
            return result

    def terminate(self):
        self.logger.debug("virt-who shut down started")
        # Terminate the backends before clearing the queue, the queue must be empty
        # to end a child process, otherwise it will be stuck in queue.put()
        self.terminate_event.set()
        # Give backends some time to terminate properly
        time.sleep(0.5)

        if self.queue:
            # clear the queue and put "exit" there
            try:
                while True:
                    self.queue.get(False)
            except Empty:
                pass
            self.queue.put("exit")

        # Give backends some more time to terminate properly
        time.sleep(0.5)

        for virt in self.virts:
            virt.terminate()
        self.virt = []
        self.queue_logger = None

    def reload(self):
        self.logger.warn("virt-who reload")
        self.reports = {}
        # clear the queue and put "reload" there
        try:
            while True:
                self.queue.get(False)
        except Empty:
            pass
        self.reloading = True
        self.queue.put("reload")

    def getMapping(self):
        mapping = {}
        for config in self.configManager.configs:
            logger = log.getLogger(self.options, config)
            virt = Virt.fromConfig(logger, config)
            mapping[config.name or 'none'] = self._readGuests(virt)
        return mapping


def exceptionCheck(e):
    try:
        # This happens when connection to server is interrupted (CTRL+C or signal)
        if e.args[0] == errno.EALREADY:
            exit(0)
    except Exception:
        pass


class OptionError(Exception):
    pass

def parseOptions():
    parser = OptionParserEpilog(usage="virt-who [-d] [-i INTERVAL] [-o] [--sam|--satellite5|--satellite6] [--libvirt|--vdsm|--esx|--rhevm|--hyperv]",
                                description="Agent for reporting virtual guest IDs to subscription manager",
                                epilog="virt-who also reads enviroment variables. They have the same name as command line arguments but uppercased, with underscore instead of dash and prefixed with VIRTWHO_ (e.g. VIRTWHO_ONE_SHOT). Empty variables are considered as disabled, non-empty as enabled")
    parser.add_option("-d", "--debug", action="store_true", dest="debug", default=False, help="Enable debugging output")
    parser.add_option("-o", "--one-shot", action="store_true", dest="oneshot", default=False, help="Send the list of guest IDs and exit immediately")
    parser.add_option("-i", "--interval", type="int", dest="interval", default=0, help="Acquire list of virtual guest each N seconds. Send if changes are detected.")
    parser.add_option("-p", "--print", action="store_true", dest="print_", default=False, help="Print the host/guest association obtained from virtualization backend (implies oneshot)")
    parser.add_option("-c", "--config", action="append", dest="configs", default=[], help="Configuration file that will be processed, can be used multiple times")
    parser.add_option("-m", "--log-per-config", action="store_true", dest="log_per_config", default=False, help="Write one log file per configured virtualization backend.\nImplies a log_dir of %s/virtwho (Default: all messages are written to a single log file)" % log.DEFAULT_LOG_DIR)
    parser.add_option("-l", "--log-dir", action="store", dest="log_dir", default=log.DEFAULT_LOG_DIR, help="The absolute path of the directory to log to. (Default '%s')" % log.DEFAULT_LOG_DIR)
    parser.add_option("-f", "--log-file", action="store", dest="log_file", default=log.DEFAULT_LOG_FILE, help="The file name to write logs to. (Default '%s')" % log.DEFAULT_LOG_FILE)

    virtGroup = OptionGroup(parser, "Virtualization backend", "Choose virtualization backend that should be used to gather host/guest associations")
    virtGroup.add_option("--libvirt", action="store_const", dest="virtType", const="libvirt", default=None, help="Use libvirt to list virtual guests [default]")
    virtGroup.add_option("--vdsm", action="store_const", dest="virtType", const="vdsm", help="Use vdsm to list virtual guests")
    virtGroup.add_option("--esx", action="store_const", dest="virtType", const="esx", help="Register ESX machines using vCenter")
    virtGroup.add_option("--rhevm", action="store_const", dest="virtType", const="rhevm", help="Register guests using RHEV-M")
    virtGroup.add_option("--hyperv", action="store_const", dest="virtType", const="hyperv", help="Register guests using Hyper-V")
    parser.add_option_group(virtGroup)

    managerGroup = OptionGroup(parser, "Subscription manager", "Choose where the host/guest associations should be reported")
    managerGroup.add_option("--sam", action="store_const", dest="smType", const=SAT6, default=SAT6, help="Report host/guest associations to the Subscription Asset Manager [default]")
    managerGroup.add_option("--satellite6", action="store_const", dest="smType", const=SAT6, help="Report host/guest associations to the Satellite 6 server")
    managerGroup.add_option("--satellite5", action="store_const", dest="smType", const=SAT5, help="Report host/guest associations to the Satellite 5 server")
    managerGroup.add_option("--satellite", action="store_const", dest="smType", const=SAT5, help=SUPPRESS_HELP)
    parser.add_option_group(managerGroup)

    libvirtGroup = OptionGroup(parser, "Libvirt options", "Use these options with --libvirt")
    libvirtGroup.add_option("--libvirt-owner", action="store", dest="owner", default="", help="Organization who has purchased subscriptions of the products, default is owner of current system")
    libvirtGroup.add_option("--libvirt-env", action="store", dest="env", default="", help="Environment where the vCenter server belongs to, default is environment of current system")
    libvirtGroup.add_option("--libvirt-server", action="store", dest="server", default="", help="URL of the libvirt server to connect to, default is empty for libvirt on local computer")
    libvirtGroup.add_option("--libvirt-username", action="store", dest="username", default="", help="Username for connecting to the libvirt daemon")
    libvirtGroup.add_option("--libvirt-password", action="store", dest="password", default="", help="Password for connecting to the libvirt daemon")
    parser.add_option_group(libvirtGroup)

    esxGroup = OptionGroup(parser, "vCenter/ESX options", "Use these options with --esx")
    esxGroup.add_option("--esx-owner", action="store", dest="owner", default="", help="Organization who has purchased subscriptions of the products")
    esxGroup.add_option("--esx-env", action="store", dest="env", default="", help="Environment where the vCenter server belongs to")
    esxGroup.add_option("--esx-server", action="store", dest="server", default="", help="URL of the vCenter server to connect to")
    esxGroup.add_option("--esx-username", action="store", dest="username", default="", help="Username for connecting to vCenter")
    esxGroup.add_option("--esx-password", action="store", dest="password", default="", help="Password for connecting to vCenter")
    parser.add_option_group(esxGroup)

    rhevmGroup = OptionGroup(parser, "RHEV-M options", "Use these options with --rhevm")
    rhevmGroup.add_option("--rhevm-owner", action="store", dest="owner", default="", help="Organization who has purchased subscriptions of the products")
    rhevmGroup.add_option("--rhevm-env", action="store", dest="env", default="", help="Environment where the RHEV-M belongs to")
    rhevmGroup.add_option("--rhevm-server", action="store", dest="server", default="", help="URL of the RHEV-M server to connect to (preferable use secure connection - https://<ip or domain name>:<secure port, usually 8443>)")
    rhevmGroup.add_option("--rhevm-username", action="store", dest="username", default="", help="Username for connecting to RHEV-M in the format username@domain")
    rhevmGroup.add_option("--rhevm-password", action="store", dest="password", default="", help="Password for connecting to RHEV-M")
    parser.add_option_group(rhevmGroup)

    hypervGroup = OptionGroup(parser, "Hyper-V options", "Use these options with --hyperv")
    hypervGroup.add_option("--hyperv-owner", action="store", dest="owner", default="", help="Organization who has purchased subscriptions of the products")
    hypervGroup.add_option("--hyperv-env", action="store", dest="env", default="", help="Environment where the Hyper-V belongs to")
    hypervGroup.add_option("--hyperv-server", action="store", dest="server", default="", help="URL of the Hyper-V server to connect to")
    hypervGroup.add_option("--hyperv-username", action="store", dest="username", default="", help="Username for connecting to Hyper-V")
    hypervGroup.add_option("--hyperv-password", action="store", dest="password", default="", help="Password for connecting to Hyper-V")
    parser.add_option_group(hypervGroup)

    satelliteGroup = OptionGroup(parser, "Satellite 5 options", "Use these options with --satellite5")
    satelliteGroup.add_option("--satellite-server", action="store", dest="sat_server", default="", help="Satellite server URL")
    satelliteGroup.add_option("--satellite-username", action="store", dest="sat_username", default="", help="Username for connecting to Satellite server")
    satelliteGroup.add_option("--satellite-password", action="store", dest="sat_password", default="", help="Password for connecting to Satellite server")
    parser.add_option_group(satelliteGroup)

    (options, args) = parser.parse_args()

    # Handle enviroment variables

    env = os.getenv("VIRTWHO_LOG_PER_CONFIG", "0").strip().lower()
    if env in ["1", "true"]:
        options.log_per_config = True

    env = os.getenv("VIRTWHO_LOG_DIR", log.DEFAULT_LOG_DIR).strip()
    if env != log.DEFAULT_LOG_DIR:
        options.log_dir = env
    elif options.log_per_config:
        options.log_dir = os.path.join(log.DEFAULT_LOG_DIR, 'virtwho')

    env = os.getenv("VIRTWHO_LOG_FILE", log.DEFAULT_LOG_FILE).strip()
    if env != log.DEFAULT_LOG_FILE:
        options.log_file = env

    env = os.getenv("VIRTWHO_DEBUG", "0").strip().lower()
    if env in ["1", "true"]:
        options.debug = True

    # Used only when starting as service (initscript sets it to 1, systemd to 0)
    env = os.getenv("VIRTWHO_BACKGROUND", "0").strip().lower()
    options.background = env in ["1", "true"]

    logger = log.getLogger(options)

    env = os.getenv("VIRTWHO_ONE_SHOT", "0").strip().lower()
    if env in ["1", "true"]:
        options.oneshot = True

    if options.print_:
        options.oneshot = True

    env = os.getenv("VIRTWHO_INTERVAL", "0").strip().lower()
    try:
        if int(env) > 0 and options.interval == 0:
            options.interval = int(env)
    except ValueError:
        logger.warning("Interval is not number, ignoring")

    env = os.getenv("VIRTWHO_SAM", "0").strip().lower()
    if env in ["1", "true"]:
        options.smType = SAT6

    env = os.getenv("VIRTWHO_SATELLITE6", "0").strip().lower()
    if env in ["1", "true"]:
        options.smType = SAT6

    env = os.getenv("VIRTWHO_SATELLITE5", "0").strip().lower()
    if env in ["1", "true"]:
        options.smType = SAT5

    env = os.getenv("VIRTWHO_SATELLITE", "0").strip().lower()
    if env in ["1", "true"]:
        options.smType = SAT5

    env = os.getenv("VIRTWHO_LIBVIRT", "0").strip().lower()
    if env in ["1", "true"]:
        options.virtType = "libvirt"

    env = os.getenv("VIRTWHO_VDSM", "0").strip().lower()
    if env in ["1", "true"]:
        options.virtType = "vdsm"

    env = os.getenv("VIRTWHO_ESX", "0").strip().lower()
    if env in ["1", "true"]:
        options.virtType = "esx"

    env = os.getenv("VIRTWHO_RHEVM", "0").strip().lower()
    if env in ["1", "true"]:
        options.virtType = "rhevm"

    env = os.getenv("VIRTWHO_HYPERV", "0").strip().lower()
    if env in ["1", "true"]:
        options.virtType = "hyperv"

    def checkEnv(variable, option, name, required=True):
        """
        If `option` is empty, check enviroment `variable` and return its value.
        Exit if it's still empty
        """
        if len(option) == 0:
            option = os.getenv(variable, "").strip()
        if required and len(option) == 0:
            raise OptionError("Required parameter '%s' is not set, exiting" % name)
        return option

    if options.smType == SAT5:
        options.sat_server = checkEnv("VIRTWHO_SATELLITE_SERVER", options.sat_server, "satellite-server")
        options.sat_username = checkEnv("VIRTWHO_SATELLITE_USERNAME", options.sat_username, "satellite-username")
        if len(options.sat_password) == 0:
            options.sat_password = os.getenv("VIRTWHO_SATELLITE_PASSWORD", "")

    if options.virtType == "libvirt":
        options.owner = checkEnv("VIRTWHO_LIBVIRT_OWNER", options.owner, "owner", required=False)
        options.env = checkEnv("VIRTWHO_LIBVIRT_ENV", options.env, "env", required=False)
        options.server = checkEnv("VIRTWHO_LIBVIRT_SERVER", options.server, "server", required=False)
        options.username = checkEnv("VIRTWHO_LIBVIRT_USERNAME", options.username, "username", required=False)
        if len(options.password) == 0:
            options.password = os.getenv("VIRTWHO_LIBVIRT_PASSWORD", "")

    if options.virtType == "esx":
        options.owner = checkEnv("VIRTWHO_ESX_OWNER", options.owner, "owner", required=False)
        options.env = checkEnv("VIRTWHO_ESX_ENV", options.env, "env", required=False)
        options.server = checkEnv("VIRTWHO_ESX_SERVER", options.server, "server")
        options.username = checkEnv("VIRTWHO_ESX_USERNAME", options.username, "username")
        if len(options.password) == 0:
            options.password = os.getenv("VIRTWHO_ESX_PASSWORD", "")

    if options.virtType == "rhevm":
        options.owner = checkEnv("VIRTWHO_RHEVM_OWNER", options.owner, "owner", required=False)
        options.env = checkEnv("VIRTWHO_RHEVM_ENV", options.env, "env", required=False)
        options.server = checkEnv("VIRTWHO_RHEVM_SERVER", options.server, "server")
        options.username = checkEnv("VIRTWHO_RHEVM_USERNAME", options.username, "username")
        if len(options.password) == 0:
            options.password = os.getenv("VIRTWHO_RHEVM_PASSWORD", "")

    if options.virtType == "hyperv":
        options.owner = checkEnv("VIRTWHO_HYPERV_OWNER", options.owner, "owner", required=False)
        options.env = checkEnv("VIRTWHO_HYPERV_ENV", options.env, "env", required=False)
        options.server = checkEnv("VIRTWHO_HYPERV_SERVER", options.server, "server")
        options.username = checkEnv("VIRTWHO_HYPERV_USERNAME", options.username, "username")
        if len(options.password) == 0:
            options.password = os.getenv("VIRTWHO_HYPERV_PASSWORD", "")

    if options.smType == 'sam' and options.virtType in ('esx', 'rhevm', 'hyperv'):
        if not options.owner:
            raise OptionError("Option --%s-owner (or VIRTWHO_%s_OWNER environment variable) needs to be set" % (options.virtType, options.virtType.upper()))
        if not options.env:
            raise OptionError("Option --%s-env (or VIRTWHO_%s_ENV environment variable) needs to be set" % (options.virtType, options.virtType.upper()))

    if options.background and options.oneshot:
        logger.error("Background and oneshot can't be used together, using background mode")
        options.oneshot = False

    if options.oneshot:
        if options.interval > 0:
            logger.error("Interval doesn't make sense in oneshot mode, ignoring")

    else:
        if options.interval < MinimumSendInterval:
            if options.interval == 0:
                logger.info("Interval set to the default of %s seconds." % str(DefaultInterval))
            else:
                logger.warning("Interval value may not be set below the default of %s seconds. Will use default value." % str(MinimumSendInterval))
            options.interval = MinimumSendInterval

    return (logger, options)


class PIDLock(object):
    def __init__(self, filename):
        self.filename = filename

    def is_locked(self):
        try:
            f = open(self.filename, "r")
            pid = int(f.read().strip())
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                # Process no longer exists
                print >>sys.stderr, "PID file exists but associated process does not, deleting PID file"
                os.remove(self.filename)
                return False
        except Exception:
            return False

    def __enter__(self):
        # Write pid to pidfile
        try:
            f = open(self.filename, "w")
            f.write("%d" % os.getpid())
            f.close()
        except Exception as e:
            print >>sys.stderr, "Unable to create pid file: %s" % str(e)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            os.remove(self.filename)
        except Exception:
            pass

virtWho = None


def main():
    try:
        logger, options = parseOptions()
    except OptionError as e:
        print >>sys.stderr, str(e)
        exit(1)

    lock = PIDLock(PIDFILE)
    if lock.is_locked():
        print >>sys.stderr, "virt-who seems to be already running. If not, remove %s" % PIDFILE
        exit(1)

    def atexit_fn():
        global virtWho
        if virtWho:
            virtWho.terminate()
    atexit.register(atexit_fn)

    def reload(signal, stackframe):
        global virtWho
        virtWho.reload()

    signal.signal(signal.SIGHUP, reload)

    global RetryInterval
    if options.interval < RetryInterval:
        RetryInterval = options.interval


    global virtWho
    try:
        virtWho = VirtWho(logger, options)
    except (InvalidKeyFile, InvalidPasswordFormat) as e:
        logger.error(str(e))
        exit(1)

    if options.virtType is not None:
        config = Config("env/cmdline", options.virtType, options.server,
                        options.username, options.password, options.owner, options.env)
        virtWho.configManager.addConfig(config)
    for conffile in options.configs:
        try:
            virtWho.configManager.readFile(conffile)
        except Exception as e:
            logger.error('Config file "%s" skipped because of an error: %s' % (conffile, str(e)))
    if len(virtWho.configManager.configs) == 0:
        # In order to keep compatibility with older releases of virt-who,
        # fallback to using libvirt as default virt backend
        logger.info("No configurations found, using libvirt as backend")
        virtWho.configManager.addConfig(Config("env/cmdline", "libvirt"))

    for config in virtWho.configManager.configs:
        if config.name is None:
            logger.info('Using commandline or sysconfig configuration ("%s" mode)', config.type)
        else:
            logger.info('Using configuration "%s" ("%s" mode)' % (config.name, config.type))

    if options.background:
        locker = lambda: daemon.DaemonContext(pidfile=lock, files_preserve=[logger.handlers[0].stream])
    else:
        locker = lambda: lock

    with locker():
        while True:
            try:
                _main(virtWho)
                break
            except ReloadRequest:
                logger.info("Reloading")
                continue

def _main(virtWho):
    result = virtWho.run()

    if virtWho.options.print_:
        hypervisors = []
        for config, report in result.items():
            if isinstance(report, DomainListReport):
                hypervisors.append({
                    'guests': [guest.toDict() for guest in report.guests]
                })
            elif isinstance(report, HostGuestAssociationReport):
                for hypervisor in report.association['hypervisors']:
                    h = OrderedDict((
                        ('uuid', hypervisor.hypervisorId),
                        ('guests', [guest.toDict() for guest in hypervisor.guestIds])
                    ))
                    hypervisors.append(h)
        data = json.dumps({
            'hypervisors': hypervisors
        })
        virtWho.logger.debug("Associations found: %s" % json.dumps({
            'hypervisors': hypervisors
        }, indent=4))
        print data


def exit(code):
    """
    exits with the code provided, properly disposing of resources
    """
    if virtWho:
        virtWho.terminate()
    queueLogger = log.getDefaultQueueLogger()
    if queueLogger:
        queueLogger.terminate()
    sys.exit(code)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        exit(1)
    except Exception as e:
        print >>sys.stderr, e
        logger = logging.getLogger("virtwho.main")
        logger.exception("Fatal error:")
        exit(1)
    exit(0)
