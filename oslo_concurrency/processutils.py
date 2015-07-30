# Copyright 2011 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
System-level utilities and helper functions.
"""

import logging
import multiprocessing
import os
import random
import shlex
import signal
import time

from oslo_utils import importutils
from oslo_utils import strutils
from oslo_utils import timeutils
import six

from oslo_concurrency._i18n import _


# NOTE(bnemec): eventlet doesn't monkey patch subprocess, so we need to
# determine the proper subprocess module to use ourselves.  I'm using the
# time module as the check because that's a monkey patched module we use
# in combination with subprocess below, so they need to match.
eventlet = importutils.try_import('eventlet')
if eventlet and eventlet.patcher.is_monkey_patched(time):
    from eventlet.green import subprocess
else:
    import subprocess


LOG = logging.getLogger(__name__)


class InvalidArgumentError(Exception):
    def __init__(self, message=None):
        super(InvalidArgumentError, self).__init__(message)


class UnknownArgumentError(Exception):
    def __init__(self, message=None):
        super(UnknownArgumentError, self).__init__(message)


class ProcessExecutionError(Exception):
    def __init__(self, stdout=None, stderr=None, exit_code=None, cmd=None,
                 description=None):
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        self.cmd = cmd
        self.description = description

        if description is None:
            description = _("Unexpected error while running command.")
        if exit_code is None:
            exit_code = '-'
        message = _('%(description)s\n'
                    'Command: %(cmd)s\n'
                    'Exit code: %(exit_code)s\n'
                    'Stdout: %(stdout)r\n'
                    'Stderr: %(stderr)r') % {'description': description,
                                             'cmd': cmd,
                                             'exit_code': exit_code,
                                             'stdout': stdout,
                                             'stderr': stderr}
        super(ProcessExecutionError, self).__init__(message)


class NoRootWrapSpecified(Exception):
    def __init__(self, message=None):
        super(NoRootWrapSpecified, self).__init__(message)


def _subprocess_setup():
    # Python installs a SIGPIPE handler by default. This is usually not what
    # non-Python subprocesses expect.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


LOG_ALL_ERRORS = 1
LOG_FINAL_ERROR = 2


def execute(*cmd, **kwargs):
    """Helper method to shell out and execute a command through subprocess.

    Allows optional retry.

    :param cmd:             Passed to subprocess.Popen.
    :type cmd:              string
    :param cwd:             Set the current working directory
    :type cwd:              string
    :param process_input:   Send to opened process.
    :type process_input:    string
    :param env_variables:   Environment variables and their values that
                            will be set for the process.
    :type env_variables:    dict
    :param check_exit_code: Single bool, int, or list of allowed exit
                            codes.  Defaults to [0].  Raise
                            :class:`ProcessExecutionError` unless
                            program exits with one of these code.
    :type check_exit_code:  boolean, int, or [int]
    :param delay_on_retry:  True | False. Defaults to True. If set to True,
                            wait a short amount of time before retrying.
    :type delay_on_retry:   boolean
    :param attempts:        How many times to retry cmd.
    :type attempts:         int
    :param run_as_root:     True | False. Defaults to False. If set to True,
                            the command is prefixed by the command specified
                            in the root_helper kwarg.
    :type run_as_root:      boolean
    :param root_helper:     command to prefix to commands called with
                            run_as_root=True
    :type root_helper:      string
    :param shell:           whether or not there should be a shell used to
                            execute this command. Defaults to false.
    :type shell:            boolean
    :param loglevel:        log level for execute commands.
    :type loglevel:         int.  (Should be logging.DEBUG or logging.INFO)
    :param log_errors:      Should stdout and stderr be logged on error?
                            Possible values are None=default,
                            LOG_FINAL_ERROR, or LOG_ALL_ERRORS. None
                            implies no logging on errors. The values
                            LOG_FINAL_ERROR and LOG_ALL_ERRORS are
                            relevant when multiple attempts of command
                            execution are requested using the
                            'attempts' parameter. If LOG_FINAL_ERROR
                            is specified then only log an error on the
                            last attempt, and LOG_ALL_ERRORS requires
                            logging on each occurence of an error.
    :type log_errors:       integer.
    :param binary:          On Python 3, return stdout and stderr as bytes if
                            binary is True, as Unicode otherwise.
    :type binary:           boolean
    :param on_execute:      This function will be called upon process creation
                            with the object as a argument.  The Purpose of this
                            is to allow the caller of `processutils.execute` to
                            track process creation asynchronously.
    :type on_execute:       function(:class:`subprocess.Popen`)
    :param on_completion:   This function will be called upon process
                            completion with the object as a argument.  The
                            Purpose of this is to allow the caller of
                            `processutils.execute` to track process completion
                            asynchronously.
    :type on_completion:    function(:class:`subprocess.Popen`)
    :returns:               (stdout, stderr) from process execution
    :raises:                :class:`UnknownArgumentError` on
                            receiving unknown arguments
    :raises:                :class:`ProcessExecutionError`
    :raises:                :class:`OSError`
    """

    cwd = kwargs.pop('cwd', None)
    process_input = kwargs.pop('process_input', None)
    env_variables = kwargs.pop('env_variables', None)
    check_exit_code = kwargs.pop('check_exit_code', [0])
    ignore_exit_code = False
    delay_on_retry = kwargs.pop('delay_on_retry', True)
    attempts = kwargs.pop('attempts', 1)
    run_as_root = kwargs.pop('run_as_root', False)
    root_helper = kwargs.pop('root_helper', '')
    shell = kwargs.pop('shell', False)
    loglevel = kwargs.pop('loglevel', logging.DEBUG)
    log_errors = kwargs.pop('log_errors', None)
    binary = kwargs.pop('binary', False)
    on_execute = kwargs.pop('on_execute', None)
    on_completion = kwargs.pop('on_completion', None)

    if isinstance(check_exit_code, bool):
        ignore_exit_code = not check_exit_code
        check_exit_code = [0]
    elif isinstance(check_exit_code, int):
        check_exit_code = [check_exit_code]

    if kwargs:
        raise UnknownArgumentError(_('Got unknown keyword args: %r') % kwargs)

    if log_errors not in [None, LOG_ALL_ERRORS, LOG_FINAL_ERROR]:
        raise InvalidArgumentError(_('Got invalid arg log_errors: %r') %
                                   log_errors)

    if run_as_root and hasattr(os, 'geteuid') and os.geteuid() != 0:
        if not root_helper:
            raise NoRootWrapSpecified(
                message=_('Command requested root, but did not '
                          'specify a root helper.'))
        if shell:
            # root helper has to be injected into the command string
            cmd = [' '.join((root_helper, cmd[0]))] + list(cmd[1:])
        else:
            # root helper has to be tokenized into argument list
            cmd = shlex.split(root_helper) + list(cmd)

    cmd = [str(c) for c in cmd]
    sanitized_cmd = strutils.mask_password(' '.join(cmd))

    watch = timeutils.StopWatch()
    while attempts > 0:
        attempts -= 1
        watch.restart()

        try:
            LOG.log(loglevel, _('Running cmd (subprocess): %s'), sanitized_cmd)
            _PIPE = subprocess.PIPE  # pylint: disable=E1101

            if os.name == 'nt':
                preexec_fn = None
                close_fds = False
            else:
                preexec_fn = _subprocess_setup
                close_fds = True

            obj = subprocess.Popen(cmd,
                                   stdin=_PIPE,
                                   stdout=_PIPE,
                                   stderr=_PIPE,
                                   close_fds=close_fds,
                                   preexec_fn=preexec_fn,
                                   shell=shell,
                                   cwd=cwd,
                                   env=env_variables)

            if on_execute:
                on_execute(obj)

            result = obj.communicate(process_input)

            obj.stdin.close()  # pylint: disable=E1101
            _returncode = obj.returncode  # pylint: disable=E1101
            LOG.log(loglevel, 'CMD "%s" returned: %s in %0.3fs',
                    sanitized_cmd, _returncode, watch.elapsed())

            if on_completion:
                on_completion(obj)

            if not ignore_exit_code and _returncode not in check_exit_code:
                (stdout, stderr) = result
                if six.PY3:
                    stdout = os.fsdecode(stdout)
                    stderr = os.fsdecode(stderr)
                sanitized_stdout = strutils.mask_password(stdout)
                sanitized_stderr = strutils.mask_password(stderr)
                raise ProcessExecutionError(exit_code=_returncode,
                                            stdout=sanitized_stdout,
                                            stderr=sanitized_stderr,
                                            cmd=sanitized_cmd)
            if six.PY3 and not binary and result is not None:
                (stdout, stderr) = result
                # Decode from the locale using using the surrogateescape error
                # handler (decoding cannot fail)
                stdout = os.fsdecode(stdout)
                stderr = os.fsdecode(stderr)
                return (stdout, stderr)
            else:
                return result

        except (ProcessExecutionError, OSError) as err:
            # if we want to always log the errors or if this is
            # the final attempt that failed and we want to log that.
            if log_errors == LOG_ALL_ERRORS or (
                    log_errors == LOG_FINAL_ERROR and not attempts):
                if isinstance(err, ProcessExecutionError):
                    format = _('%(desc)r\ncommand: %(cmd)r\n'
                               'exit code: %(code)r\nstdout: %(stdout)r\n'
                               'stderr: %(stderr)r')
                    LOG.log(loglevel, format, {"desc": err.description,
                                               "cmd": err.cmd,
                                               "code": err.exit_code,
                                               "stdout": err.stdout,
                                               "stderr": err.stderr})
                else:
                    format = _('Got an OSError\ncommand: %(cmd)r\n'
                               'errno: %(errno)r')
                    LOG.log(loglevel, format, {"cmd": sanitized_cmd,
                                               "errno": err.errno})

            if not attempts:
                LOG.log(loglevel, _('%r failed. Not Retrying.'),
                        sanitized_cmd)
                raise
            else:
                LOG.log(loglevel, _('%r failed. Retrying.'),
                        sanitized_cmd)
                if delay_on_retry:
                    time.sleep(random.randint(20, 200) / 100.0)
        finally:
            # NOTE(termie): this appears to be necessary to let the subprocess
            #               call clean something up in between calls, without
            #               it two execute calls in a row hangs the second one
            # NOTE(bnemec): termie's comment above is probably specific to the
            #               eventlet subprocess module, but since we still
            #               have to support that we're leaving the sleep.  It
            #               won't hurt anything in the stdlib case anyway.
            time.sleep(0)


def trycmd(*args, **kwargs):
    """A wrapper around execute() to more easily handle warnings and errors.

    Returns an (out, err) tuple of strings containing the output of
    the command's stdout and stderr.  If 'err' is not empty then the
    command can be considered to have failed.

    :discard_warnings   True | False. Defaults to False. If set to True,
                        then for succeeding commands, stderr is cleared

    """
    discard_warnings = kwargs.pop('discard_warnings', False)

    try:
        out, err = execute(*args, **kwargs)
        failed = False
    except ProcessExecutionError as exn:
        out, err = '', six.text_type(exn)
        failed = True

    if not failed and discard_warnings and err:
        # Handle commands that output to stderr but otherwise succeed
        err = ''

    return out, err


def ssh_execute(ssh, cmd, process_input=None,
                addl_env=None, check_exit_code=True,
                binary=False):
    sanitized_cmd = strutils.mask_password(cmd)
    LOG.debug('Running cmd (SSH): %s', sanitized_cmd)
    if addl_env:
        raise InvalidArgumentError(_('Environment not supported over SSH'))

    if process_input:
        # This is (probably) fixable if we need it...
        raise InvalidArgumentError(_('process_input not supported over SSH'))

    stdin_stream, stdout_stream, stderr_stream = ssh.exec_command(cmd)
    channel = stdout_stream.channel

    # NOTE(justinsb): This seems suspicious...
    # ...other SSH clients have buffering issues with this approach
    stdout = stdout_stream.read()
    stderr = stderr_stream.read()

    stdin_stream.close()

    exit_status = channel.recv_exit_status()

    if six.PY3:
        # Decode from the locale using using the surrogateescape error handler
        # (decoding cannot fail). Decode even if binary is True because
        # mask_password() requires Unicode on Python 3
        stdout = os.fsdecode(stdout)
        stderr = os.fsdecode(stderr)
    stdout = strutils.mask_password(stdout)
    stderr = strutils.mask_password(stderr)

    # exit_status == -1 if no exit code was returned
    if exit_status != -1:
        LOG.debug('Result was %s' % exit_status)
        if check_exit_code and exit_status != 0:
            raise ProcessExecutionError(exit_code=exit_status,
                                        stdout=stdout,
                                        stderr=stderr,
                                        cmd=sanitized_cmd)

    if binary:
        if six.PY2:
            # On Python 2, stdout is a bytes string if mask_password() failed
            # to decode it, or an Unicode string otherwise. Encode to the
            # default encoding (ASCII) because mask_password() decodes from
            # the same encoding.
            if isinstance(stdout, unicode):
                stdout = stdout.encode()
            if isinstance(stderr, unicode):
                stderr = stderr.encode()
        else:
            # fsencode() is the reverse operation of fsdecode()
            stdout = os.fsencode(stdout)
            stderr = os.fsencode(stderr)

    return (stdout, stderr)


def get_worker_count():
    """Utility to get the default worker count.

    @return: The number of CPUs if that can be determined, else a default
             worker count of 1 is returned.
    """
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:
        return 1
