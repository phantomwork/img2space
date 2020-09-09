import os
import errno
import json
import datetime
import sys
import subprocess
import string
import signal

from opendm import context
from opendm import log


def get_ccd_widths():
    """Return the CCD Width of the camera listed in the JSON defs file."""
    with open(context.ccd_widths_path) as f:
        sensor_data = json.loads(f.read())
    return dict(zip(map(string.lower, sensor_data.keys()), sensor_data.values()))

running_subprocesses = []
cleanup_callbacks = []

def add_cleanup_callback(func):
    global cleanup_callbacks
    cleanup_callbacks.append(func)

def remove_cleanup_callback(func):
    global cleanup_callbacks

    try:
        cleanup_callbacks.remove(func)
    except ValueError as e:
        log.ODM_EXCEPTION("Tried to remove %s from cleanup_callbacks but got: %s" % (str(func), str(e)))

def exit_gracefully():
    global running_subprocesses
    global cleanup_callbacks

    log.ODM_WARNING("Caught TERM/INT signal, attempting to exit gracefully...")

    for cb in cleanup_callbacks:
        cb()

    for sp in running_subprocesses:
        log.ODM_WARNING("Sending TERM signal to PID %s..." % sp.pid)
        os.killpg(os.getpgid(sp.pid), signal.SIGTERM)
    
    os._exit(1)

def sighandler(signum, frame):
    exit_gracefully()

signal.signal(signal.SIGINT, sighandler)
signal.signal(signal.SIGTERM, sighandler)

def run(cmd, env_paths=[context.superbuild_bin_path], env_vars={}, packages_paths=context.python_packages_paths):
    """Run a system command"""
    global running_subprocesses

    log.ODM_INFO('running %s' % cmd)

    env = os.environ.copy()
    if len(env_paths) > 0:
        env["PATH"] = env["PATH"] + ":" + ":".join(env_paths)
    
    if len(packages_paths) > 0:
        env["PYTHONPATH"] = env.get("PYTHONPATH", "") + ":" + ":".join(packages_paths) 
    
    for k in env_vars:
        env[k] = str(env_vars[k])

    p = subprocess.Popen(cmd, shell=True, env=env, preexec_fn=os.setsid)
    running_subprocesses.append(p)
    retcode = p.wait()
    running_subprocesses.remove(p)
    if retcode < 0:
        raise Exception("Child was terminated by signal {}".format(-retcode))
    elif retcode > 0:
        raise Exception("Child returned {}".format(retcode))


def now():
    """Return the current time"""
    return datetime.datetime.now().strftime('%a %b %d %H:%M:%S %Z %Y')


def now_raw():
    return datetime.datetime.now()


def benchmark(start, benchmarking_file, process):
    """
    runs a benchmark with a start datetime object
    :return: the running time (delta)
    """
    # Write to benchmark file
    delta = (datetime.datetime.now() - start).total_seconds()
    with open(benchmarking_file, 'a') as b:
        b.write('%s runtime: %s seconds\n' % (process, delta))

def mkdir_p(path):
    """Make a directory including parent directories.
    """
    try:
        os.makedirs(path)
    except os.error as exc:
        if exc.errno != errno.EEXIST or not os.path.isdir(path):
            raise

# Python2 shutil.which
def which(program):
    path=os.getenv('PATH')
    for p in path.split(os.path.pathsep):
        p=os.path.join(p,program)
        if os.path.exists(p) and os.access(p,os.X_OK):
            return p
