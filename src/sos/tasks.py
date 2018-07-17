#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.
import os
import fasteners
import pickle
import time
from stat import S_IREAD, S_IRGRP, S_IROTH
from typing import Union, Dict

from .utils import (env, expand_time, linecount_of_file, sample_of_file,
                    short_repr, tail_of_file)
from .task_executor import monitor_interval, taskTags, taskDuration, loadTask

# return {} if result is unchanged
# otherwise return a dictionary of with keys 'status' and 'files'


def check_task(task, hint={}) -> Dict[str, Union[str, Dict[str, float]]]:
    #
    # for pending status, a new file might have been created to change its status
    # so we cannot use cached signature.
    #
    # for running status, static pulse file actually means something wrong.
    #
    # when testing. if the timestamp is 0, the file does not exist originally, it should
    # still does not exist. Otherwise the file should exist and has the same timestamp
    if hint and hint['status'] not in ('pending', 'running') and \
            all((os.path.isfile(f) and os.stat(f).st_mtime == v) if v else (not os.path.isfile(f)) for f, v in hint['files'].items()):
        return {}

    # status of the job, please refer to https://github.com/vatlab/SOS/issues/529
    # for details.
    #
    task_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.task')
    if not os.path.isfile(task_file):
        return dict(status='missing', files={task_file: 0})
    pulse_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.pulse')
    if not os.path.isfile(pulse_file):
        pulse_file = os.path.join(os.path.expanduser(
            '~'), '.sos', 'tasks', task + '.status')

    def has_pulse():
        # for whatever reason, sometimes the pulse file might appear to be slightly
        # before the task file, and if the task is very short so the pulse file is
        # not updated, the task will appear to be in pending mode forever
        return os.path.isfile(pulse_file) and os.stat(pulse_file).st_mtime >= os.stat(task_file).st_mtime - 1

    res_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.res')

    def has_res():
        return os.path.isfile(res_file) and os.stat(res_file).st_mtime >= os.stat(task_file).st_mtime

    job_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.sh')

    def has_job():
        job_id_file = os.path.join(os.path.expanduser(
            '~'), '.sos', 'tasks', task + '.job_id')
        return os.path.isfile(job_file) and os.stat(job_file).st_mtime >= os.stat(task_file).st_mtime \
            and os.path.isfile(job_id_file) and os.stat(job_id_file).st_mtime >= os.stat(job_file).st_mtime

    if has_res():
        try:
            from .targets import file_target
            with open(res_file, 'rb') as result:
                res = pickle.load(result)
            status_files = {task_file: os.stat(task_file).st_mtime,
                            res_file: os.stat(res_file).st_mtime,
                            pulse_file: os.stat(pulse_file).st_mtime if os.path.isfile(
                                pulse_file) else 0
                            }

            if ('ret_code' in res and res['ret_code'] == 0) or ('succ' in res and res['succ'] == 0):
                for var in ('input', 'output', 'depends'):
                    if var not in res or not isinstance(res[var], dict):
                        continue
                    for x, y in res[var].items():
                        if not file_target(x).target_exists() or file_target(x).target_signature() != y:
                            env.logger.debug(
                                f'{x} not found or signature mismatch')
                            return dict(status='signature-mismatch',
                                        files=status_files)
                return dict(status='completed', files=status_files)
            else:
                return dict(status='failed', files=status_files)
        except Exception as e:
            # sometimes the resfile is changed while we are reading it
            # so we wait a bit and try again.
            env.logger.warning(e)
            time.sleep(.5)
            return check_task(task)
    #
    if has_pulse():
        status_files = {task_file: os.stat(task_file).st_mtime,
                        pulse_file: os.stat(pulse_file).st_mtime}
        # dead?
        # if the status file is readonly
        if not os.access(pulse_file, os.W_OK):
            return dict(status='aborted', files={task_file: os.stat(task_file).st_mtime,
                                                 pulse_file: os.stat(pulse_file).st_mtime})
        start_stamp = os.stat(pulse_file).st_mtime
        elapsed = time.time() - start_stamp
        if elapsed < 0:
            env.logger.debug(
                f'{pulse_file} is created in the future. Your system time might be problematic')
        # if the file is within 5 seconds
        if elapsed < monitor_interval:
            # if running, we return old hint files even if the timestamp has been changed
            # because we will check the status of running jobs anyway.
            if hint and hint['status'] == 'running':
                return {}
            else:
                return dict(status='running', files=status_files)
        elif elapsed > 2 * monitor_interval:
            if has_res():
                # result file appears during sos tatus run
                return check_task(task)
            else:
                return dict(status='aborted', files=status_files)
        # otherwise, let us be patient ... perhaps there is some problem with the filesystem etc
        time.sleep(2 * monitor_interval)
        end_stamp = os.stat(pulse_file).st_mtime
        # the process is still alive
        if has_res():
            return check_task(task)
        elif start_stamp != end_stamp:
            if hint and hint['status'] == 'running':
                return {}
            else:
                return dict(status='running', files=status_files)
        else:
            return dict(status='aborted', files=status_files)
    # if there is no status file
    if has_job():
        return dict(status='submitted', files={task_file: os.stat(task_file).st_mtime,
                                               job_file: os.stat(job_file).st_mtime,
                                               pulse_file: 0})
    else:
        # status not changed
        if hint and hint['status'] == 'pending' and hint['files'][task_file] == os.stat(task_file).st_mtime:
            return {}
        else:
            return dict(status='pending', files={task_file: os.stat(task_file).st_mtime,
                                                 job_file: 0})


def print_task_status(tasks, verbosity: int=1, html: bool=False, start_time=False, age=None, tags=None, status=None):
    # verbose is ignored for now
    import glob
    from multiprocessing.pool import ThreadPool as Pool
    check_all: bool = not tasks
    if check_all:
        tasks = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*.task'))
        all_tasks = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                     for x in tasks]
        if not all_tasks:
            return
    else:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(os.path.join(os.path.expanduser('~'),
                                             '.sos', 'tasks', f'{t}*.task'))
            matched = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                       for x in matched]
            if not matched:
                all_tasks.append((t, None))
            else:
                all_tasks.extend(matched)
    if age is not None:
        age = expand_time(age, default_unit='d')
        if age > 0:
            all_tasks = [x for x in all_tasks if time.time() - x[1] >= age]
        else:
            all_tasks = [x for x in all_tasks if time.time() - x[1] <= -age]

    all_tasks = sorted(list(set(all_tasks)),
                       key=lambda x: 0 if x[1] is None else x[1])

    if tags:
        all_tasks = [x for x in all_tasks if any(
            x in tags for x in taskTags(x[0]).split(' '))]

    if not all_tasks:
        env.logger.info('No matching tasks')
        return
    cache_file: str = os.path.join(
        os.path.expanduser('~'), '.sos', 'tasks', 'status_cache.pickle')
    #
    status_cache = {}
    if os.path.isfile(cache_file):
        with fasteners.InterProcessLock(cache_file + '_'):
            with open(cache_file, 'rb') as cache:
                status_cache = pickle.load(cache)
    #
    # at most 20 threads
    p = Pool(min(20, len(all_tasks)))
    # the result can be {} for unchanged, or real results
    raw_status = p.starmap(
        check_task, [(x[0], status_cache.get(x[0], {})) for x in all_tasks])
    # if check all, we clear the cache and record all existing tasks
    has_changes: bool = any(x for x in raw_status)
    if has_changes:
        if check_all:
            status_cache = {k[0]: v if v else status_cache[k[0]]
                            for k, v in zip(all_tasks, raw_status)}
        else:
            status_cache.update(
                {k[0]: v for k, v in zip(all_tasks, raw_status) if v})
    obtained_status = [status_cache[x[0]]['status'] for x in all_tasks]
    if status:
        all_tasks = [x for x, s in zip(
            all_tasks, obtained_status) if s in status]
        obtained_status = [x for x in obtained_status if x in status]
    #
    # automatically remove non-running tasks that are more than 30 days old
    to_be_removed = []
    #
    if html:
        verbosity = -1
    if verbosity == 0:
        print('\n'.join(obtained_status))
    elif verbosity == 1:
        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != 'running':
                to_be_removed.append(t)
                continue
            print(f'{t}\t{s}')
    elif verbosity == 2:
        from .utils import PrettyRelativeTime
        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != 'running':
                to_be_removed.append(t)
                continue
            if start_time:
                if d is None:
                    print(f'{t}\t{taskTags(t)}\t{time.time()}\t{s}')
                else:
                    print(f'{t}\t{taskTags(t)}\t{d}\t{s}')
            else:
                if d is None:
                    print(f'{t}\t{taskTags(t)}\t{"":>15}\t{s}')
                elif s in ('pending', 'submitted', 'running'):
                    print(
                        f'{t}\t{taskTags(t)}\t{PrettyRelativeTime(time.time() - d):>15}\t{s}')
                else:
                    # completed or failed
                    print(
                        f'{t}\t{taskTags(t)}\t{PrettyRelativeTime(taskDuration(t)):>15}\t{s}')
    elif verbosity > 2:
        from .utils import PrettyRelativeTime
        import pprint
        from .monitor import summarizeExecution

        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60 and s != 'running':
                to_be_removed.append(t)
                continue
            print(f'{t}\t{s}\n')
            task_file = os.path.join(os.path.expanduser(
                '~'), '.sos', 'tasks', t + '.task')
            if not os.path.isfile(task_file):
                continue
            if d is not None:
                print(f'Started {PrettyRelativeTime(time.time() - d)} ago')
            if s not in ('pending', 'submitted', 'running'):
                print(f'Duration {PrettyRelativeTime(taskDuration(t))}')
            params = loadTask(task_file)
            print('TASK:\n=====')
            print(params.task)
            print('TAGS:\n=====')
            print(' '.join(params.tags))
            print()
            if params.global_def:
                print('GLOBAL:\n=======')
                print(params.global_def)
                print()
            print('ENVIRONMENT:\n============')
            job_vars = params.sos_dict
            for k in sorted(job_vars.keys()):
                v = job_vars[k]
                print(
                    f'{k:22}{short_repr(v) if verbosity == 3 else pprint.pformat(v)}')
            print()
            print('EXECUTION STATS:\n================')
            print(summarizeExecution(t, status=s))
            if verbosity == 4:
                # if there are other files such as job file, print them.
                files = glob.glob(os.path.join(
                    os.path.expanduser('~'), '.sos', 'tasks', t + '.*'))
                for f in sorted([x for x in files if os.path.splitext(x)[-1] not in ('.res',
                                                                                     '.task', '.pulse', '.status', '.def')]):
                    print(
                        f'{os.path.basename(f)}:\n{"="*(len(os.path.basename(f))+1)}')
                    try:
                        with open(f) as fc:
                            print(fc.read())
                    except Exception:
                        print('Binary file')
    else:
        # HTML output
        from .utils import PrettyRelativeTime, isPrimitive
        from .monitor import summarizeExecution
        import pprint
        print('<table width="100%" class="resource_table">')

        def row(th=None, td=None):
            if td is None:
                print(
                    f'<tr><th align="right" width="30%">{th}</th><td></td></tr>')
            elif th is None:
                print(
                    f'<tr><td colspan="2" align="left"  width="30%">{td}</td></tr>')
            else:
                print(
                    f'<tr><th align="right"  width="30%">{th}</th><td align="left"><div class="one_liner">{td}</div></td></tr>')
        for s, (t, d) in zip(obtained_status, all_tasks):
            if d is not None and time.time() - d > 30 * 24 * 60 * 60:
                to_be_removed.append(t)
                continue
            row('ID', t)
            row('Status', s)
            task_file = os.path.join(os.path.expanduser(
                '~'), '.sos', 'tasks', t + '.task')
            if not os.path.isfile(task_file):
                print('</table>')
                continue
            if d is not None:
                row('Start', f'{PrettyRelativeTime(time.time() - d):>15} ago')
            if s not in ('pending', 'submitted', 'running'):
                row('Duration', f'{PrettyRelativeTime(taskDuration(t)):>15}')
            task_file = os.path.join(os.path.expanduser(
                '~'), '.sos', 'tasks', t + '.task')
            if not os.path.isfile(task_file):
                continue
            params = loadTask(task_file)
            row('Task')
            row(td=f'<pre style="text-align:left">{params.task}</pre>')
            row('Tags')
            row(td=f'<pre style="text-align:left">{" ".join(params.tags)}</pre>')
            if params.global_def:
                row('Global')
                row(
                    td=f'<pre style="text-align:left">{params.global_def}</pre>')
            # row('Environment')
            job_vars = params.sos_dict
            for k in sorted(job_vars.keys()):
                v = job_vars[k]
                if not k.startswith('__') and not k == 'CONFIG':
                    if k == '_runtime':
                        for _k, _v in v.items():
                            if isPrimitive(_v) and _v not in (None, '', [], (), {}):
                                row(_k, _v)
                    elif isPrimitive(v) and v not in (None, '', [], (), {}):
                        row(k,
                            f'<pre style="text-align:left">{pprint.pformat(v)}</pre>')
            summary = summarizeExecution(t, status=s)
            if summary:
                # row('Execution')
                for line in summary.split('\n'):
                    fields = line.split(None, 1)
                    if fields[0] == 'task':
                        continue
                    row(fields[0], '' if fields[1] is None else fields[1])
                # this is a placeholder for the frontend to draw figure
                row(td=f'<div id="res_{t}"></div>')
            #
            files = glob.glob(os.path.join(
                os.path.expanduser('~'), '.sos', 'tasks', t + '.*'))
            for f in sorted([x for x in files if os.path.splitext(x)[-1] not in ('.def', '.res', '.task', '.pulse', '.status')]):
                numLines = linecount_of_file(f)
                row(os.path.splitext(f)[-1], '(empty)' if numLines ==
                    0 else f'{numLines} lines{"" if numLines < 200 else " (showing last 200)"}')
                try:
                    row(
                        td=f'<small><pre style="text-align:left">{tail_of_file(f, 200, ansi2html=True)}</pre></small>')
                except Exception:
                    row(td='<small><pre style="text-align:left">ignored.</pre><small>')
            print('</table>')
            #
            # supplement run time information
            pulse_file = os.path.join(os.path.expanduser(
                '~'), '.sos', 'tasks', t + '.pulse')
            if not os.path.isfile(pulse_file):
                return
            else:
                # A sample of 400 point should be enough to show the change of resources
                lines = sample_of_file(pulse_file, 400).splitlines()
                if len(lines) <= 2:
                    return
            # read the pulse file and plot it
            # time   proc_cpu        proc_mem        children        children_cpu    children_mem
            try:
                etime = []
                cpu = []
                mem = []
                for line in lines:
                    if line.startswith('#') or not line.strip():
                        continue
                    fields = line.split()
                    etime.append(float(fields[0]))
                    cpu.append(float(fields[1]) + float(fields[4]))
                    mem.append(float(fields[2]) / 1e6 + float(fields[5]) / 1e6)
                if not etime:
                    return
            except Exception:
                return
            #
            print('''
<script>
    function loadFiles(files, fn) {
        if (!files.length) {
            files = [];
        }
        var head = document.head || document.getElementsByTagName('head')[0];

        function loadFile(index) {
            if (files.length > index) {
                if (files[index].endsWith('.css')) {
                    var fileref = document.createElement('link');
                    fileref.setAttribute("rel", "stylesheet");
                    fileref.setAttribute("type", "text/css");
                    fileref.setAttribute("href", files[index]);
                } else {
                    var fileref = document.createElement('script');
                    fileref.setAttribute("type", "text/javascript");
                    fileref.setAttribute("src", files[index]);
                }
                console.log('Load ' + files[index]);
                head.appendChild(fileref);
                index = index + 1;
                // Used to call a callback function
                fileref.onload = function() {
                    loadFile(index);
                }
            } else if (fn) {
                fn();
            }
        }
        loadFile(0);
    }

function plotResourcePlot_''' + t + '''() {
    // get the item
    // parent element is a table cell, needs enlarge
    document.getElementById(
        "res_''' + t + '''").parentElement.setAttribute("height", "300px;");
    $("#res_''' + t + '''").css("height", "300px");
    $("#res_''' + t + '''").css("width", "100%");
    $("#res_''' + t + '''").css("min-height", "300px");

    var cpu = [''' + ','.join([f'[{x*1000},{y}]' for x, y in zip(etime, cpu)]) + '''];
    var mem = [''' + ','.join([f'[{x*1000},{y}]' for x, y in zip(etime, mem)]) + '''];

    $.plot('#res_''' + t + '''', [{
                data: cpu,
                label: "CPU (%)"
            },
            {
                data: mem,
                label: "mem (M)",
                yaxis: 2
            }
        ], {
            xaxes: [{
                mode: "time"
            }],
            yaxes: [{
                min: 0
            }, {
                position: "right",
                tickFormatter: function(v, axis) {
                    return v.toFixed(1) + 'M';
                }
            }],
            legend: {
                position: "nw"
            }
        });
}

var dt = 100;
// the frontend might be notified before the table is inserted as results.
function showResourceFigure_''' + t + '''() {
    if ( $("#res_''' + t + '''").length === 0) {
          dt = dt * 1.5; // slow-down checks for datatable as time goes on;
          setTimeout(showResourceFigure_''' + t + ''', dt);
          return;
    } else {
        $("#res_''' + t + '''").css('width', "100%").css('height', "300px");
        loadFiles(["http://www.flotcharts.org/flot/jquery.flot.js",
             "http://www.flotcharts.org/flot/jquery.flot.time.js"
            ], plotResourcePlot_''' + t + ''');
    }
}
showResourceFigure_''' + t + '''()
</script>
''')
    if has_changes:
        with fasteners.InterProcessLock(cache_file + '_'):
            with open(cache_file, 'wb') as cache:
                pickle.dump(status_cache, cache)
    else:
        env.logger.debug('No new status detected')
    # remove jobs that are older than 1 month
    if to_be_removed:
        purge_tasks(to_be_removed, verbosity=0)


def kill_tasks(tasks, tags=None):
    #
    import glob
    from multiprocessing.pool import ThreadPool as Pool
    if not tasks:
        tasks = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*.task'))
        all_tasks = [os.path.basename(x)[:-5] for x in tasks]
    else:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(os.path.join(os.path.expanduser('~'),
                                             '.sos', 'tasks', f'{t}*.task'))
            matched = [os.path.basename(x)[:-5] for x in matched]
            if not matched:
                env.logger.warning(f'{t} does not match any existing task')
            else:
                all_tasks.extend(matched)
    if tags:
        all_tasks = [x for x in all_tasks if any(
            x in tags for x in taskTags(x).split(' '))]

    if not all_tasks:
        env.logger.warning('No task to kill')
        return
    all_tasks = sorted(list(set(all_tasks)))
    # at most 20 threads
    p = Pool(min(20, len(all_tasks)))
    killed = p.map(kill_task, all_tasks)
    for s, t in zip(killed, all_tasks):
        print(f'{t}\t{s}')


def kill_task(task):
    status = check_task(task)['status']
    if status == 'pending':
        # the task engine will remove it from lists if killed through a task engine
        # but it can also be killed through command line externally, so we will need
        # to mark the task as killed
        pulse_file = os.path.join(os.path.expanduser(
            '~'), '.sos', 'tasks', task + '.pulse')
        os.open(pulse_file, flags=os.O_CREAT | os.O_RDONLY)
        return 'aborted'
    # remove job file as well
    job_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.sh')
    if os.path.isfile(job_file):
        try:
            os.remove(job_file)
        except Exception:
            pass
    if status != 'running':
        return status
    # job is running
    pulse_file = os.path.join(os.path.expanduser(
        '~'), '.sos', 'tasks', task + '.pulse')
    os.chmod(pulse_file, S_IREAD | S_IRGRP | S_IROTH)
    return 'killed'


def purge_tasks(tasks, purge_all=False, age=None, status=None, tags=None, verbosity=2):
    # verbose is ignored for now
    import glob
    if tasks:
        all_tasks = []
        for t in tasks:
            matched = glob.glob(os.path.join(os.path.expanduser('~'),
                                             '.sos', 'tasks', f'{t}*.task'))
            matched = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                       for x in matched]
            all_tasks.extend(matched)
    else:
        tasks = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*.task'))
        all_tasks = [(os.path.basename(x)[:-5], os.path.getmtime(x))
                     for x in tasks]
    #
    if age is not None:
        age = expand_time(age, default_unit='d')
        if age > 0:
            all_tasks = [x for x in all_tasks if time.time() - x[1] >= age]
        else:
            all_tasks = [x for x in all_tasks if time.time() - x[1] <= -age]

    if status:
        # at most 20 threads
        from multiprocessing.pool import ThreadPool as Pool
        p = Pool(min(20, len(all_tasks)))
        task_status = p.map(check_task, [x[0] for x in all_tasks])
        all_tasks = [x for x, s in zip(
            all_tasks, task_status) if s['status'] in status]

    if tags:
        all_tasks = [x for x in all_tasks if any(
            x in tags for x in taskTags(x[0]).split(' '))]
    #
    # remoe all task files
    all_tasks = set([x[0] for x in all_tasks])
    if all_tasks:
        #
        # find all related files, including those in nested directories
        from collections import defaultdict
        to_be_removed = defaultdict(list)
        for dirname, _, filelist in os.walk(os.path.join(os.path.expanduser('~'), '.sos', 'tasks')):
            for f in filelist:
                ID = os.path.basename(f).split('.', 1)[0]
                if ID in all_tasks:
                    to_be_removed[ID].append(os.path.join(dirname, f))
        #
        cache_file: str = os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', 'status_cache.pickle')

        if os.path.isfile(cache_file):
            with fasteners.InterProcessLock(cache_file + '_'):
                with open(cache_file, 'rb') as cache:
                    status_cache = pickle.load(cache)
        else:
            status_cache = {}
        for task in all_tasks:
            removed = True
            for f in to_be_removed[task]:
                try:
                    if verbosity > 3:
                        env.logger.trace(f'Remove {f}')
                    os.remove(f)
                except Exception as e:
                    removed = False
                    if verbosity > 0:
                        env.logger.warning(f'Failed to purge task {task[0]}')
            status_cache.pop(task, None)
            if removed and verbosity > 1:
                env.logger.info(f'Task ``{task}`` removed.')
        with fasteners.InterProcessLock(cache_file + '_'):
            with open(cache_file, 'wb') as cache:
                pickle.dump(status_cache, cache)
    elif verbosity > 1:
        env.logger.info('No matching tasks')
    if purge_all:
        matched = glob.glob(os.path.join(
            os.path.expanduser('~'), '.sos', 'tasks', '*'))
        count = 0
        for f in matched:
            if os.path.isdir(f):
                import shutil
                try:
                    shutil.rmtree(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f'Failed to remove {f}')
            else:
                try:
                    os.remove(f)
                    count += 1
                except Exception as e:
                    if verbosity > 0:
                        env.logger.warning(f'Failed to remove {e}')
        if count > 0 and verbosity > 1:
            env.logger.info(
                f'{count} other files and directories are removed.')
    return ''
